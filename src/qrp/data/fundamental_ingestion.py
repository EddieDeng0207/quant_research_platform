"""Resumable full-universe financial-statement ingestion."""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import pandas as pd

from .contracts import normalize_cn_symbol
from .ingestion import (
    DEFAULT_REQUESTS_PER_MINUTE,
    RateLimiter,
    RunRecorder,
    _atomic_json_write,
)
from .providers.base import FetchResult, ProviderError
from .storage import ParquetLake

FUNDAMENTAL_STATEMENTS = (
    "income",
    "balance_sheet",
    "cashflow",
    "financial_indicators",
)
FUNDAMENTAL_INGESTION_POLICY_VERSION = (
    "p08_tushare_per_symbol_statement_v1_rpm250"
)


@dataclass(frozen=True)
class FundamentalBackfillConfig:
    """Freeze one complete financial-history request grid."""

    start_date: str
    end_date: str
    statements: Tuple[str, ...] = FUNDAMENTAL_STATEMENTS
    symbols: Tuple[str, ...] = ()
    instrument_statuses: Tuple[str, ...] = ("L", "D", "P", "G")
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE
    max_attempts: int = 3
    retry_base_seconds: float = 2.0
    job_name: str = "p08_fundamentals_backfill"
    ingestion_policy_version: str = FUNDAMENTAL_INGESTION_POLICY_VERSION

    def validate(self) -> "FundamentalBackfillConfig":
        start = pd.Timestamp(self.start_date).normalize()
        end = pd.Timestamp(self.end_date).normalize()
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        if not self.statements:
            raise ValueError("at least one fundamental statement is required")
        unknown = sorted(set(self.statements) - set(FUNDAMENTAL_STATEMENTS))
        if unknown:
            raise ValueError(f"unsupported fundamental statements: {unknown}")
        if len(set(self.statements)) != len(self.statements):
            raise ValueError("fundamental statements must be unique")
        if self.symbols:
            normalized = tuple(normalize_cn_symbol(symbol) for symbol in self.symbols)
            if len(set(normalized)) != len(normalized):
                raise ValueError("fundamental symbols must be unique")
        if not self.instrument_statuses and not self.symbols:
            raise ValueError("instrument_statuses are required when symbols are not explicit")
        if self.requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must be non-negative")
        if not self.ingestion_policy_version:
            raise ValueError("ingestion_policy_version must not be empty")
        return self


class FundamentalIngestionRunner:
    """Fetch every statement-symbol cell with durable, resumable checkpoints."""

    def __init__(
        self,
        provider: Any,
        lake: ParquetLake,
        artifact_root: Path,
        state_root: Path,
        limiter: Optional[RateLimiter] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.lake = lake
        self.artifact_root = Path(artifact_root)
        self.state_root = Path(state_root)
        self.limiter = limiter
        self.sleeper = sleeper
        self._provider_request_limiter_installed = False

    def run(self, config: FundamentalBackfillConfig) -> Path:
        frozen = config.validate()
        config_hash = _config_hash(frozen)
        data_job_hash = _data_job_hash(frozen)
        recorder = RunRecorder(self.artifact_root, frozen, config_hash)
        limiter = self.limiter or RateLimiter(frozen.requests_per_minute)
        install_limiter = getattr(self.provider, "set_request_limiter", None)
        if callable(install_limiter):
            install_limiter(limiter.wait)
            self._provider_request_limiter_installed = True
        state_path = self.state_root / f"{frozen.job_name}_{data_job_hash[:16]}.json"
        state = _load_or_initialize_state(state_path, data_job_hash)
        summary: Dict[str, Any] = {
            "completed_this_run": 0,
            "skipped_from_checkpoint": 0,
            "rows_written": 0,
            "files_written": 0,
            "empty_snapshots": 0,
            "failed_tasks": [],
            "lake_root": str(self.lake.root.resolve()),
        }
        recorder.event("run_started", state_path=str(state_path.resolve()))
        try:
            symbols = self._resolve_symbols(
                frozen, limiter, state, state_path, recorder, summary
            )
            summary["symbols"] = len(symbols)
            summary["statements"] = len(frozen.statements)
            summary["expected_statement_tasks"] = len(symbols) * len(
                frozen.statements
            )
            for statement in frozen.statements:
                for symbol in symbols:
                    task_id = f"fundamentals_{statement}:{symbol}"
                    self._execute_task(
                        task_id,
                        lambda statement=statement, symbol=symbol: (
                            self.provider.fetch_fundamentals(
                                statement,
                                symbol,
                                frozen.start_date,
                                frozen.end_date,
                            )
                        ),
                        frozen,
                        limiter,
                        state,
                        state_path,
                        recorder,
                        summary,
                    )
            summary["checkpoint"] = str(state_path.resolve())
            summary["excluded_legacy_instruments"] = len(
                state.get("excluded_legacy_instruments", [])
            )
            recorder.event("run_completed", **summary)
            recorder.close("completed", summary)
            return recorder.path
        except Exception as exc:
            summary["failed_tasks"].append(str(exc))
            summary["checkpoint"] = str(state_path.resolve())
            recorder.event(
                "run_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            recorder.close("failed", summary)
            raise

    def _resolve_symbols(
        self,
        config: FundamentalBackfillConfig,
        limiter: RateLimiter,
        state: Dict[str, Any],
        state_path: Path,
        recorder: RunRecorder,
        summary: Dict[str, Any],
    ) -> Tuple[str, ...]:
        if config.symbols:
            symbols = tuple(sorted(normalize_cn_symbol(item) for item in config.symbols))
            if state.get("symbols") and tuple(state["symbols"]) != symbols:
                raise RuntimeError("checkpoint symbol universe differs from configuration")
            state["symbols"] = list(symbols)
            _atomic_json_write(state_path, state)
            return symbols
        if state.get("symbols"):
            summary["skipped_from_checkpoint"] += 1
            recorder.event("task_skipped", task_id="instruments", reason="checkpoint")
            return tuple(state["symbols"])

        result, path = self._fetch_with_retry(
            "instruments",
            lambda: self.provider.fetch_instruments(config.instrument_statuses),
            config,
            limiter,
            recorder,
        )
        frame = result.frame.copy()
        kind = frame.get(
            "instrument_kind", pd.Series("stock", index=frame.index, dtype="string")
        )
        legacy = sorted(frame.loc[kind.eq("legacy_stock"), "symbol"].astype(str).unique())
        list_date = pd.to_datetime(frame.get("list_date"), errors="coerce")
        eligible = kind.eq("stock") & (list_date.isna() | (list_date <= pd.Timestamp(config.end_date)))
        symbols = tuple(sorted(frame.loc[eligible, "symbol"].map(normalize_cn_symbol).unique()))
        if not symbols:
            raise RuntimeError("instrument snapshot produced no eligible fundamental symbols")
        state["symbols"] = list(symbols)
        state["excluded_legacy_instruments"] = legacy
        state["completed"]["instruments"] = str(path.resolve())
        _atomic_json_write(state_path, state)
        self._record_success("instruments", result, path, recorder, summary)
        return symbols

    def _execute_task(
        self,
        task_id: str,
        call: Callable[[], FetchResult],
        config: FundamentalBackfillConfig,
        limiter: RateLimiter,
        state: Dict[str, Any],
        state_path: Path,
        recorder: RunRecorder,
        summary: Dict[str, Any],
    ) -> None:
        if task_id in state["completed"]:
            summary["skipped_from_checkpoint"] += 1
            recorder.event("task_skipped", task_id=task_id, reason="checkpoint")
            return
        result, path = self._fetch_with_retry(
            task_id, call, config, limiter, recorder
        )
        state["completed"][task_id] = str(path.resolve())
        _atomic_json_write(state_path, state)
        self._record_success(task_id, result, path, recorder, summary)

    def _fetch_with_retry(
        self,
        task_id: str,
        call: Callable[[], FetchResult],
        config: FundamentalBackfillConfig,
        limiter: RateLimiter,
        recorder: RunRecorder,
    ) -> tuple[FetchResult, Path]:
        for attempt in range(1, config.max_attempts + 1):
            if not self._provider_request_limiter_installed:
                limiter.wait()
            recorder.event("request_started", task_id=task_id, attempt=attempt)
            try:
                result = call()
                return result, self.lake.write(result)
            except ProviderError as exc:
                recorder.event(
                    "request_failed", task_id=task_id, attempt=attempt, error=str(exc)
                )
                if attempt == config.max_attempts:
                    raise
                self.sleeper(config.retry_base_seconds * (2 ** (attempt - 1)))
        raise AssertionError("unreachable")

    @staticmethod
    def _record_success(
        task_id: str,
        result: FetchResult,
        path: Path,
        recorder: RunRecorder,
        summary: Dict[str, Any],
    ) -> None:
        summary["completed_this_run"] += 1
        summary["rows_written"] += len(result.frame)
        summary["files_written"] += 1
        if result.frame.empty:
            summary["empty_snapshots"] += 1
        recorder.event(
            "task_completed",
            task_id=task_id,
            dataset=result.dataset,
            rows=len(result.frame),
            path=str(path.resolve()),
        )


def _config_hash(config: FundamentalBackfillConfig) -> str:
    payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _data_job_hash(config: FundamentalBackfillConfig) -> str:
    payload = {
        "start_date": config.start_date,
        "end_date": config.end_date,
        "statements": config.statements,
        "symbols": config.symbols,
        "instrument_statuses": config.instrument_statuses,
        "job_name": config.job_name,
        "ingestion_policy_version": config.ingestion_policy_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_or_initialize_state(path: Path, data_job_hash: str) -> Dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("data_job_hash") != data_job_hash:
            raise RuntimeError(f"checkpoint data identity mismatch: {path}")
        return state
    state: Dict[str, Any] = {
        "version": 1,
        "data_job_hash": data_job_hash,
        "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "completed": {},
        "symbols": [],
        "excluded_legacy_instruments": [],
    }
    _atomic_json_write(path, state)
    return state
