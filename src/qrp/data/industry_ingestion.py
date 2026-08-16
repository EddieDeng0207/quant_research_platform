"""Resumable Shenwan classification and historical-membership ingestion."""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .ingestion import (
    DEFAULT_REQUESTS_PER_MINUTE,
    RateLimiter,
    RunRecorder,
    _atomic_json_write,
)
from .providers.base import FetchResult, ProviderError
from .storage import ParquetLake

INDUSTRY_TAXONOMIES = ("SW2014", "SW2021")
INDUSTRY_INGESTION_POLICY_VERSION = "p08_shenwan_l1_history_v1_rpm250"


@dataclass(frozen=True)
class IndustryBackfillConfig:
    taxonomies: Tuple[str, ...] = INDUSTRY_TAXONOMIES
    industry_level: str = "L1"
    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE
    max_attempts: int = 5
    retry_base_seconds: float = 2.0
    job_name: str = "p08_industry_membership_backfill"
    ingestion_policy_version: str = INDUSTRY_INGESTION_POLICY_VERSION

    def validate(self) -> "IndustryBackfillConfig":
        if not self.taxonomies or len(set(self.taxonomies)) != len(self.taxonomies):
            raise ValueError("industry taxonomies must be non-empty and unique")
        unknown = sorted(set(self.taxonomies) - set(INDUSTRY_TAXONOMIES))
        if unknown:
            raise ValueError(f"unsupported industry taxonomies: {unknown}")
        if self.industry_level != "L1":
            raise ValueError("formal industry neutralization currently supports L1 only")
        if self.requests_per_minute <= 0 or self.max_attempts <= 0:
            raise ValueError("rate and retry limits must be positive")
        if self.retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must be non-negative")
        return self


class IndustryIngestionRunner:
    """Freeze taxonomies, then checkpoint every taxonomy-industry membership task."""

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

    def run(self, config: IndustryBackfillConfig) -> Path:
        frozen = config.validate()
        config_hash = _hash_payload(asdict(frozen))
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
            "failed_tasks": [],
        }
        recorder.event("run_started", state_path=str(state_path.resolve()))
        try:
            categories = self._freeze_categories(
                frozen, limiter, state, state_path, recorder, summary
            )
            for category in categories:
                task_id = (
                    f"industry_membership:{category['taxonomy']}:"
                    f"{category['source_index_code']}"
                )
                self._execute_task(
                    task_id,
                    lambda category=category: self.provider.fetch_industry_members(
                        taxonomy=category["taxonomy"],
                        source_index_code=category["source_index_code"],
                        industry_code=category["industry_code"],
                        industry_name=category["industry_name"],
                        industry_level=frozen.industry_level,
                    ),
                    frozen,
                    limiter,
                    state,
                    state_path,
                    recorder,
                    summary,
                )
            summary["categories"] = len(categories)
            summary["expected_membership_tasks"] = len(categories)
            summary["checkpoint"] = str(state_path.resolve())
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

    def _freeze_categories(
        self,
        config: IndustryBackfillConfig,
        limiter: RateLimiter,
        state: Dict[str, Any],
        state_path: Path,
        recorder: RunRecorder,
        summary: Dict[str, Any],
    ) -> list[Dict[str, str]]:
        if state.get("categories"):
            return list(state["categories"])
        categories = []
        for taxonomy in config.taxonomies:
            task_id = f"industry_classification:{taxonomy}:{config.industry_level}"
            result, path = self._fetch_with_retry(
                task_id,
                lambda taxonomy=taxonomy: self.provider.fetch_industry_classification(
                    taxonomy, config.industry_level
                ),
                config,
                limiter,
                recorder,
            )
            state["completed"][task_id] = str(path.resolve())
            self._record_success(task_id, result, path, recorder, summary)
            selected = result.frame[
                ["taxonomy", "source_index_code", "industry_code", "industry_name"]
            ].drop_duplicates()
            categories.extend(selected.astype("string").to_dict("records"))
            _atomic_json_write(state_path, state)
        categories = sorted(
            categories,
            key=lambda item: (item["taxonomy"], item["source_index_code"]),
        )
        if not categories:
            raise RuntimeError("industry classifications produced no categories")
        state["categories"] = categories
        _atomic_json_write(state_path, state)
        return categories

    def _execute_task(
        self,
        task_id: str,
        call: Callable[[], FetchResult],
        config: IndustryBackfillConfig,
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
        result, path = self._fetch_with_retry(task_id, call, config, limiter, recorder)
        state["completed"][task_id] = str(path.resolve())
        _atomic_json_write(state_path, state)
        self._record_success(task_id, result, path, recorder, summary)

    def _fetch_with_retry(
        self,
        task_id: str,
        call: Callable[[], FetchResult],
        config: IndustryBackfillConfig,
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
        recorder.event(
            "task_completed",
            task_id=task_id,
            dataset=result.dataset,
            rows=len(result.frame),
            path=str(path.resolve()),
        )


def _load_or_initialize_state(path: Path, data_job_hash: str) -> Dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("data_job_hash") != data_job_hash:
            raise RuntimeError(f"checkpoint data identity mismatch: {path}")
        return state
    state: Dict[str, Any] = {
        "version": 1,
        "data_job_hash": data_job_hash,
        "completed": {},
        "categories": [],
    }
    _atomic_json_write(path, state)
    return state


def _data_job_hash(config: IndustryBackfillConfig) -> str:
    return _hash_payload(
        {
            "taxonomies": config.taxonomies,
            "industry_level": config.industry_level,
            "job_name": config.job_name,
            "ingestion_policy_version": config.ingestion_policy_version,
        }
    )


def _hash_payload(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
