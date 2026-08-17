"""Resumable, rate-limited P0 ingestion orchestration."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import pandas as pd

from .providers.base import FetchResult, ProviderError
from .storage import ParquetLake

DEFAULT_REQUESTS_PER_MINUTE = 400
INGESTION_POLICY_VERSION = (
    "p0_tushare_max_page_v3_rpm400_vendor_sentinels_master_interval_fallback"
)

P0_DATASET_METHODS: Mapping[str, str] = {
    "daily_bars": "fetch_daily_bars_by_date",
    "adjustment_factors": "fetch_adjustment_factors_by_date",
    "daily_indicators": "fetch_daily_indicators_by_date",
    "stock_status": "fetch_stock_status_by_date",
    "daily_limits": "fetch_daily_limits_by_date",
    "daily_suspensions": "fetch_daily_suspensions_by_date",
    "historical_instruments": "fetch_historical_instruments_by_date",
}


@dataclass(frozen=True)
class P0BackfillConfig:
    start_date: str
    end_date: str
    datasets: Tuple[str, ...] = (
        "daily_bars",
        "adjustment_factors",
        "daily_indicators",
    )
    exchange: str = "SSE"
    include_instruments: bool = True
    include_security_code_mappings: bool = False
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE
    max_attempts: int = 3
    retry_base_seconds: float = 2.0
    job_name: str = "p0_backfill"
    ingestion_policy_version: str = INGESTION_POLICY_VERSION
    workers: int = 1

    def validate(self) -> "P0BackfillConfig":
        start = pd.Timestamp(self.start_date)
        end = pd.Timestamp(self.end_date)
        if start > end:
            raise ValueError("start_date must be on or before end_date")
        unknown = sorted(set(self.datasets) - set(P0_DATASET_METHODS))
        if unknown:
            raise ValueError(f"Unsupported P0 datasets: {unknown}")
        if not self.datasets:
            raise ValueError("At least one P0 dataset is required")
        if self.requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.workers <= 0:
            raise ValueError("workers must be positive")
        if not self.ingestion_policy_version:
            raise ValueError("ingestion_policy_version must not be empty")
        return self


class RateLimiter:
    """Simple monotonic interval limiter; one runner owns one limiter."""

    def __init__(
        self,
        requests_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.minimum_interval = 60.0 / requests_per_minute
        self.clock = clock
        self.sleeper = sleeper
        self.last_request_at: Optional[float] = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            if self.last_request_at is not None:
                remaining = self.minimum_interval - (now - self.last_request_at)
                if remaining > 0:
                    self.sleeper(remaining)
            self.last_request_at = self.clock()


class RunRecorder:
    """Persist config, runtime, source identity, events, and terminal status."""

    def __init__(self, artifact_root: Path, config: Any, config_hash: str) -> None:
        timestamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{timestamp}_{config_hash[:12]}_{uuid.uuid4().hex[:6]}"
        self.path = Path(artifact_root) / "ingestion_runs" / self.run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.events_path = self.path / "events.jsonl"
        self._event_lock = threading.Lock()
        self.started_at = pd.Timestamp.now(tz="UTC")
        source_identity = _snapshot_source(self.path / "source_snapshot")
        _atomic_json_write(self.path / "config_snapshot.json", asdict(config))
        _atomic_json_write(
            self.path / "run_manifest.json",
            {
                "run_id": self.run_id,
                "status": "running",
                "started_at": self.started_at.isoformat(),
                "config_hash": config_hash,
                "source_tree_sha256": source_identity["tree_sha256"],
                "source_manifest": "source_snapshot/source_manifest.json",
                "runtime": _runtime_identity(),
                "credentials": {
                    "TUSHARE_TOKEN": "set" if os.environ.get("TUSHARE_TOKEN") else "missing",
                    "FRED_API_KEY": "set" if os.environ.get("FRED_API_KEY") else "missing",
                },
            },
        )

    def event(self, event_type: str, **payload: Any) -> None:
        entry = {
            "at": pd.Timestamp.now(tz="UTC").isoformat(),
            "event": event_type,
            **payload,
        }
        with self._event_lock:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(entry, ensure_ascii=False, default=str, sort_keys=True)
                    + "\n"
                )

    def close(self, status: str, summary: Mapping[str, Any]) -> None:
        finished_at = pd.Timestamp.now(tz="UTC")
        manifest = json.loads((self.path / "run_manifest.json").read_text(encoding="utf-8"))
        manifest.update(
            {
                "status": status,
                "finished_at": finished_at.isoformat(),
                "elapsed_seconds": (finished_at - self.started_at).total_seconds(),
                "summary": dict(summary),
            }
        )
        _atomic_json_write(self.path / "run_manifest.json", manifest)
        _atomic_json_write(self.path / "summary.json", dict(summary))


class P0IngestionRunner:
    """Run or resume a deterministic set of full-market daily ingestion tasks."""

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

    def run(self, config: P0BackfillConfig) -> Path:
        config.validate()
        config_hash = _config_hash(config)
        data_job_hash = _data_job_hash(config)
        recorder = RunRecorder(self.artifact_root, config, config_hash)
        limiter = self.limiter or RateLimiter(config.requests_per_minute)
        install_limiter = getattr(self.provider, "set_request_limiter", None)
        if callable(install_limiter):
            install_limiter(limiter.wait)
            self._provider_request_limiter_installed = True
        state_path = self.state_root / f"{config.job_name}_{data_job_hash[:16]}.json"
        state = _load_or_initialize_state(state_path, data_job_hash)
        summary: Dict[str, Any] = {
            "completed_this_run": 0,
            "skipped_from_checkpoint": 0,
            "rows_written": 0,
            "files_written": 0,
            "failed_tasks": [],
        }
        recorder.event("run_started", state_path=str(state_path))
        try:
            if config.include_instruments:
                self._execute_task(
                    "instruments",
                    self.provider.fetch_instruments,
                    config,
                    limiter,
                    state,
                    state_path,
                    recorder,
                    summary,
                )

            if config.include_security_code_mappings:
                self._execute_task(
                    "security_code_mappings",
                    self.provider.fetch_security_code_mappings,
                    config,
                    limiter,
                    state,
                    state_path,
                    recorder,
                    summary,
                )

            if "calendar" not in state["completed"]:
                calendar_result, calendar_path = self._fetch_and_write(
                    "calendar",
                    lambda: self.provider.fetch_calendar(
                        config.start_date, config.end_date, config.exchange
                    ),
                    config,
                    limiter,
                    recorder,
                )
                open_dates = [
                    value.strftime("%Y-%m-%d")
                    for value in calendar_result.frame.loc[
                        calendar_result.frame["is_open"], "calendar_date"
                    ].sort_values()
                ]
                state["open_dates"] = open_dates
                state["completed"]["calendar"] = str(calendar_path)
                _atomic_json_write(state_path, state)
                _record_success(
                    recorder, summary, "calendar", calendar_result, calendar_path
                )
            else:
                open_dates = list(state.get("open_dates", []))
                summary["skipped_from_checkpoint"] += 1
                recorder.event("task_skipped", task_id="calendar", reason="checkpoint")
            if not open_dates:
                raise RuntimeError("The requested range contains no open trading dates")

            tasks = []
            for trade_date in open_dates:
                for dataset in config.datasets:
                    task_id = f"{dataset}:{trade_date}"
                    if task_id in state["completed"]:
                        summary["skipped_from_checkpoint"] += 1
                        recorder.event(
                            "task_skipped", task_id=task_id, reason="checkpoint"
                        )
                        continue
                    method = getattr(self.provider, P0_DATASET_METHODS[dataset])
                    tasks.append(
                        (
                            task_id,
                            lambda method=method, trade_date=trade_date: method(trade_date),
                        )
                    )
            if config.workers == 1:
                for task_id, call in tasks:
                    self._execute_task(
                        task_id,
                        call,
                        config,
                        limiter,
                        state,
                        state_path,
                        recorder,
                        summary,
                    )
            else:
                self._execute_tasks_concurrently(
                    tasks,
                    config,
                    limiter,
                    state,
                    state_path,
                    recorder,
                    summary,
                )
            summary["open_dates"] = len(open_dates)
            summary["checkpoint"] = str(state_path)
            recorder.event("run_completed", **summary)
            recorder.close("completed", summary)
            return recorder.path
        except Exception as exc:
            summary["failed_tasks"].append(str(exc))
            summary["checkpoint"] = str(state_path)
            recorder.event(
                "run_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            recorder.close("failed", summary)
            raise

    def _execute_task(
        self,
        task_id: str,
        call: Callable[[], FetchResult],
        config: P0BackfillConfig,
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
        result, path = self._fetch_and_write(
            task_id, call, config, limiter, recorder
        )
        state["completed"][task_id] = str(path)
        _atomic_json_write(state_path, state)
        _record_success(recorder, summary, task_id, result, path)

    def _execute_tasks_concurrently(
        self,
        tasks: list[tuple[str, Callable[[], FetchResult]]],
        config: P0BackfillConfig,
        limiter: RateLimiter,
        state: Dict[str, Any],
        state_path: Path,
        recorder: RunRecorder,
        summary: Dict[str, Any],
    ) -> None:
        with ThreadPoolExecutor(max_workers=config.workers) as executor:
            pending = {
                executor.submit(
                    self._fetch_and_write,
                    task_id,
                    call,
                    config,
                    limiter,
                    recorder,
                ): task_id
                for task_id, call in tasks
            }
            try:
                for future in as_completed(pending):
                    task_id = pending[future]
                    result, path = future.result()
                    state["completed"][task_id] = str(path)
                    _atomic_json_write(state_path, state)
                    _record_success(recorder, summary, task_id, result, path)
            except Exception:
                for future in pending:
                    future.cancel()
                raise

    def _fetch_and_write(
        self,
        task_id: str,
        call: Callable[[], FetchResult],
        config: P0BackfillConfig,
        limiter: RateLimiter,
        recorder: RunRecorder,
    ) -> Tuple[FetchResult, Path]:
        for attempt in range(1, config.max_attempts + 1):
            if not self._provider_request_limiter_installed:
                limiter.wait()
            recorder.event("request_started", task_id=task_id, attempt=attempt)
            try:
                result = call()
                path = self.lake.write(result)
                return result, path
            except ProviderError as exc:
                recorder.event(
                    "request_failed", task_id=task_id, attempt=attempt, error=str(exc)
                )
                if attempt == config.max_attempts:
                    raise
                self.sleeper(config.retry_base_seconds * (2 ** (attempt - 1)))
        raise AssertionError("unreachable")


def _record_success(
    recorder: RunRecorder,
    summary: Dict[str, Any],
    task_id: str,
    result: FetchResult,
    path: Path,
) -> None:
    summary["completed_this_run"] += 1
    summary["rows_written"] += len(result.frame)
    summary["files_written"] += 1
    recorder.event(
        "task_completed",
        task_id=task_id,
        dataset=result.dataset,
        rows=len(result.frame),
        path=str(path),
    )


def _config_hash(config: P0BackfillConfig) -> str:
    payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _data_job_hash(config: P0BackfillConfig) -> str:
    payload = {
        "start_date": config.start_date,
        "end_date": config.end_date,
        "datasets": config.datasets,
        "exchange": config.exchange,
        "include_instruments": config.include_instruments,
        "include_security_code_mappings": config.include_security_code_mappings,
        "job_name": config.job_name,
        "ingestion_policy_version": config.ingestion_policy_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_or_initialize_state(path: Path, data_job_hash: str) -> Dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("data_job_hash") != data_job_hash:
            raise RuntimeError(f"Checkpoint data identity mismatch: {path}")
        return state
    state: Dict[str, Any] = {
        "version": 1,
        "data_job_hash": data_job_hash,
        "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "completed": {},
        "open_dates": [],
    }
    _atomic_json_write(path, state)
    return state


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _snapshot_source(destination: Path) -> Dict[str, Any]:
    """Freeze executable source and non-secret project specifications for a run."""
    project_root = Path(__file__).resolve().parents[3]
    candidates = [project_root / "pyproject.toml", project_root / "README.md"]
    for pattern in ["src/qrp/**/*.py", "configs/*.json", "docs/*.md"]:
        candidates.extend(project_root.glob(pattern))
    files = sorted({path for path in candidates if path.is_file()})
    entries = []
    tree_digest = hashlib.sha256()
    for source in files:
        relative = source.relative_to(project_root)
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entry = {"path": str(relative), "sha256": digest, "bytes": len(data)}
        entries.append(entry)
        tree_digest.update(str(relative).encode("utf-8"))
        tree_digest.update(digest.encode("ascii"))
    identity = {"tree_sha256": tree_digest.hexdigest(), "files": entries}
    _atomic_json_write(destination / "source_manifest.json", identity)
    return identity


def _runtime_identity() -> Dict[str, Any]:
    packages: Dict[str, str] = {}
    for name in ["quant-research-platform", "pandas", "pyarrow", "tushare", "akshare"]:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "not-installed"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "packages": packages,
    }
