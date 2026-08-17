"""Resumable full-universe corporate-action ingestion."""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

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

CORPORATE_ACTION_INGESTION_POLICY_VERSION = (
    "p063_tushare_per_symbol_dividend_v1_rpm400_p05_universe"
)


@dataclass(frozen=True)
class CorporateActionBackfillConfig:
    """Freeze one complete security-level dividend query universe."""

    start_date: str
    end_date: str
    symbols: tuple[str, ...]
    universe_artifact_id: str
    universe_manifest_sha256: str
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE
    max_attempts: int = 3
    retry_base_seconds: float = 2.0
    workers: int = 4
    job_name: str = "p063_corporate_actions_backfill"
    ingestion_policy_version: str = CORPORATE_ACTION_INGESTION_POLICY_VERSION

    def validate(self) -> "CorporateActionBackfillConfig":
        if pd.Timestamp(self.start_date) > pd.Timestamp(self.end_date):
            raise ValueError("start_date must be on or before end_date")
        normalized = tuple(sorted(normalize_cn_symbol(item) for item in self.symbols))
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("corporate-action symbols must be non-empty and unique")
        if normalized != self.symbols:
            raise ValueError("corporate-action symbols must be normalized and sorted")
        if not self.universe_artifact_id or not self.universe_manifest_sha256:
            raise ValueError("the frozen P0.5 universe identity is required")
        if min(self.requests_per_minute, self.max_attempts, self.workers) <= 0:
            raise ValueError("rate, attempts, and workers must be positive")
        if self.retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must be non-negative")
        return self


class CorporateActionIngestionRunner:
    """Fetch every frozen security with durable checkpoints, including empty results."""

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

    def run(self, config: CorporateActionBackfillConfig) -> Path:
        frozen = config.validate()
        config_hash = _fingerprint(asdict(frozen))
        data_job_hash = _data_job_hash(frozen)
        recorder = RunRecorder(self.artifact_root, frozen, config_hash)
        limiter = self.limiter or RateLimiter(frozen.requests_per_minute)
        install_limiter = getattr(self.provider, "set_request_limiter", None)
        if callable(install_limiter):
            install_limiter(limiter.wait)
            self._provider_request_limiter_installed = True
        state_path = self.state_root / f"{frozen.job_name}_{data_job_hash[:16]}.json"
        state = _load_or_initialize_state(state_path, data_job_hash, frozen.symbols)
        summary: Dict[str, Any] = {
            "expected_symbol_tasks": len(frozen.symbols),
            "completed_this_run": 0,
            "skipped_from_checkpoint": 0,
            "rows_written": 0,
            "files_written": 0,
            "empty_snapshots": 0,
            "failed_tasks": [],
            "lake_root": str(self.lake.root.resolve()),
            "checkpoint": str(state_path.resolve()),
        }
        recorder.event("run_started", state_path=str(state_path.resolve()))
        tasks = []
        for symbol in frozen.symbols:
            task_id = f"corporate_actions:{symbol}"
            if task_id in state["completed"]:
                summary["skipped_from_checkpoint"] += 1
                recorder.event("task_skipped", task_id=task_id, reason="checkpoint")
                continue
            tasks.append(
                (
                    task_id,
                    partial(
                        self.provider.fetch_corporate_actions,
                        symbol,
                        frozen.start_date,
                        frozen.end_date,
                    ),
                )
            )
        try:
            if frozen.workers == 1:
                for task_id, call in tasks:
                    result, path = self._fetch_with_retry(
                        task_id, call, frozen, limiter, recorder
                    )
                    _record_completion(
                        task_id, result, path, state, state_path, recorder, summary
                    )
            else:
                with ThreadPoolExecutor(max_workers=frozen.workers) as executor:
                    pending = {
                        executor.submit(
                            self._fetch_with_retry,
                            task_id,
                            call,
                            frozen,
                            limiter,
                            recorder,
                        ): task_id
                        for task_id, call in tasks
                    }
                    try:
                        for future in as_completed(pending):
                            task_id = pending[future]
                            result, path = future.result()
                            _record_completion(
                                task_id,
                                result,
                                path,
                                state,
                                state_path,
                                recorder,
                                summary,
                            )
                    except Exception:
                        for future in pending:
                            future.cancel()
                        raise
            summary["completed_total"] = len(state["completed"])
            summary["query_coverage"] = len(state["completed"]) / len(frozen.symbols)
            recorder.event("run_completed", **summary)
            recorder.close("completed", summary)
            return recorder.path
        except Exception as exc:
            summary["failed_tasks"].append(str(exc))
            recorder.event(
                "run_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            recorder.close("failed", summary)
            raise

    def _fetch_with_retry(
        self,
        task_id: str,
        call: Callable[[], FetchResult],
        config: CorporateActionBackfillConfig,
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


def frozen_symbols_from_tradability(artifact: Path) -> tuple[tuple[str, ...], Dict[str, str]]:
    """Return the exact canonical security universe from one promoted P0.5 artifact."""
    root = Path(artifact)
    manifest_path = root / "manifest.json"
    parquet_path = root / "tradability.parquet"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("quality", {}).get("promotion_passed", False):
        raise ValueError("P0.5 artifact was not promoted")
    if _sha256(parquet_path) != manifest.get("output", {}).get("sha256"):
        raise ValueError("P0.5 tradability hash mismatch")
    frame = pd.read_parquet(parquet_path, columns=["symbol"])
    symbols = tuple(sorted(frame["symbol"].dropna().map(normalize_cn_symbol).unique()))
    if not symbols:
        raise ValueError("P0.5 artifact contains no corporate-action query symbols")
    return symbols, {
        "artifact_id": str(manifest["artifact_id"]),
        "manifest_sha256": _sha256(manifest_path),
    }


def load_completed_corporate_action_run(
    run_path: Path,
) -> tuple[list[Path], tuple[str, ...], Dict[str, Any]]:
    """Verify a completed run and return every queried snapshot, including empties."""
    root = Path(run_path)
    manifest_path = root / "run_manifest.json"
    config_path = root / "config_snapshot.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "completed":
        raise ValueError("corporate-action ingestion run is not complete")
    state_path = Path(manifest["summary"]["checkpoint"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    symbols = tuple(config["symbols"])
    expected = {f"corporate_actions:{symbol}" for symbol in symbols}
    missing = sorted(expected - set(state["completed"]))
    if missing:
        raise ValueError(f"corporate-action run has incomplete query cells: {missing[:10]}")
    paths = [Path(state["completed"][key]) for key in sorted(expected)]
    for path in paths:
        if not path.exists():
            raise ValueError(f"corporate-action raw snapshot is missing: {path}")
    identity = {
        "run_id": manifest["run_id"],
        "run_manifest_sha256": _sha256(manifest_path),
        "config_sha256": _sha256(config_path),
        "checkpoint_sha256": _sha256(state_path),
        "universe_artifact_id": config["universe_artifact_id"],
        "universe_manifest_sha256": config["universe_manifest_sha256"],
        "query_start_date": config["start_date"],
        "query_end_date": config["end_date"],
        "query_symbols": len(symbols),
    }
    return paths, symbols, identity


def _record_completion(
    task_id: str,
    result: FetchResult,
    path: Path,
    state: Dict[str, Any],
    state_path: Path,
    recorder: RunRecorder,
    summary: Dict[str, Any],
) -> None:
    state["completed"][task_id] = str(path.resolve())
    _atomic_json_write(state_path, state)
    summary["completed_this_run"] += 1
    summary["rows_written"] += len(result.frame)
    summary["files_written"] += 1
    summary["empty_snapshots"] += int(result.frame.empty)
    recorder.event(
        "task_completed",
        task_id=task_id,
        dataset=result.dataset,
        rows=len(result.frame),
        path=str(path.resolve()),
    )


def _load_or_initialize_state(
    path: Path, data_job_hash: str, symbols: Sequence[str]
) -> Dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("data_job_hash") != data_job_hash or tuple(state["symbols"]) != tuple(
            symbols
        ):
            raise RuntimeError("corporate-action checkpoint identity mismatch")
        return state
    state = {
        "version": 1,
        "data_job_hash": data_job_hash,
        "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "symbols": list(symbols),
        "completed": {},
    }
    _atomic_json_write(path, state)
    return state


def _data_job_hash(config: CorporateActionBackfillConfig) -> str:
    payload = {
        "start_date": config.start_date,
        "end_date": config.end_date,
        "symbols": config.symbols,
        "universe_artifact_id": config.universe_artifact_id,
        "universe_manifest_sha256": config.universe_manifest_sha256,
        "job_name": config.job_name,
        "ingestion_policy_version": config.ingestion_policy_version,
    }
    return _fingerprint(payload)


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
