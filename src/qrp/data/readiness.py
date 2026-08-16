"""Research-readiness coverage gates for multi-year A-share inputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pandas as pd

DEFAULT_REQUIRED_DAILY_DATASETS = (
    "daily_bars",
    "adjustment_factors",
    "daily_indicators",
    "historical_instruments",
    "daily_limits",
    "daily_suspensions",
    "stock_status",
)


@dataclass
class ResearchReadinessReport:
    start_date: str
    end_date: str
    expected_open_sessions: int
    estimated_weekly_periods: int
    minimum_weekly_periods: int
    datasets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    fundamentals: Dict[str, Any] = field(default_factory=dict)
    industry_membership: Dict[str, Any] = field(default_factory=dict)
    hard_failures: Dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(value == 0 for value in self.hard_failures.values())

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["passed"] = self.passed
        return payload


def audit_research_readiness(
    lake_root: Path,
    calendar_path: Path,
    start_date: str,
    end_date: str,
    *,
    fundamental_artifact: Optional[Path] = None,
    industry_membership_path: Optional[Path] = None,
    required_daily_datasets: Sequence[str] = DEFAULT_REQUIRED_DAILY_DATASETS,
    minimum_weekly_periods: int = 104,
) -> ResearchReadinessReport:
    """Prove complete daily partitions and required PIT research inputs."""
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    if minimum_weekly_periods < 1:
        raise ValueError("minimum_weekly_periods must be positive")
    calendar_file = Path(calendar_path)
    calendar = pd.read_parquet(calendar_file)
    missing_calendar = sorted({"calendar_date", "is_open"} - set(calendar.columns))
    if missing_calendar:
        raise ValueError(f"calendar missing columns: {missing_calendar}")
    dates = pd.to_datetime(calendar["calendar_date"], errors="coerce").dt.normalize()
    open_dates = pd.DatetimeIndex(
        dates.loc[calendar["is_open"].astype(bool) & dates.between(start, end)]
    ).unique().sort_values()
    if open_dates.empty:
        raise ValueError("calendar contains no open sessions in the requested range")
    weekly_periods = int(
        pd.Series(open_dates).dt.to_period("W-FRI").nunique()
    )
    report = ResearchReadinessReport(
        start_date=str(start.date()),
        end_date=str(end.date()),
        expected_open_sessions=len(open_dates),
        estimated_weekly_periods=weekly_periods,
        minimum_weekly_periods=minimum_weekly_periods,
    )
    manifest_path = Path(lake_root) / "manifest.jsonl"
    entries = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    missing_partition_total = 0
    expected = {str(value.date()) for value in open_dates}
    for dataset in required_daily_datasets:
        eligible = []
        for entry in entries:
            if entry.get("provider") != "tushare" or entry.get("dataset") != dataset:
                continue
            trade_date = entry.get("partition_values", {}).get("trade_date")
            if not trade_date:
                continue
            normalized = str(pd.Timestamp(trade_date).date())
            if normalized in expected:
                eligible.append((normalized, pd.Timestamp(entry["written_at"]), entry))
        latest: Dict[str, Dict[str, Any]] = {}
        for trade_date, _written_at, entry in sorted(eligible, key=lambda item: item[1]):
            latest[trade_date] = entry
        covered = set(latest)
        missing = sorted(expected - covered)
        missing_partition_total += len(missing)
        report.datasets[dataset] = {
            "covered_partitions": len(covered),
            "expected_partitions": len(expected),
            "missing_partitions": len(missing),
            "missing_sample": missing[:10],
            "first_partition": min(covered) if covered else None,
            "last_partition": max(covered) if covered else None,
            "latest_rows": sum(int(entry.get("rows", 0)) for entry in latest.values()),
            "superseded_snapshots": len(eligible) - len(latest),
            "complete": not missing,
        }

    report.fundamentals = _audit_fundamental_artifact(fundamental_artifact)
    report.industry_membership = _audit_industry_membership(
        industry_membership_path, start, end
    )
    report.hard_failures = {
        "insufficient_weekly_periods": int(weekly_periods < minimum_weekly_periods),
        "missing_daily_partitions": missing_partition_total,
        "fundamental_artifact_missing_or_failed": int(
            not report.fundamentals.get("ready", False)
        ),
        "historical_industry_membership_missing_or_failed": int(
            not report.industry_membership.get("ready", False)
        ),
    }
    return report


def write_research_readiness_report(
    report: ResearchReadinessReport, path: Path
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, default=str
    ) + "\n"
    temporary = target.parent / f".{target.name}.tmp"
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)
    return target


def _audit_fundamental_artifact(path: Optional[Path]) -> Dict[str, Any]:
    if path is None:
        return {"ready": False, "reason": "not_provided"}
    root = Path(path)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return {"ready": False, "reason": "missing_manifest", "path": str(root)}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_failures = []
    for name, metadata in manifest.get("outputs", {}).items():
        output = root / metadata["path"]
        if not output.exists() or _sha256(output) != metadata["sha256"]:
            output_failures.append(name)
    promoted = bool(manifest.get("quality", {}).get("promotion_passed", False))
    return {
        "ready": promoted and not output_failures,
        "artifact_id": manifest.get("artifact_id"),
        "schema_version": manifest.get("schema_version"),
        "research_as_of_at": manifest.get("identity", {}).get("research_as_of_at"),
        "output_hash_failures": output_failures,
        "promotion_passed": promoted,
    }


def _audit_industry_membership(
    path: Optional[Path], start: pd.Timestamp, end: pd.Timestamp
) -> Dict[str, Any]:
    if path is None:
        return {"ready": False, "reason": "not_provided"}
    source = Path(path)
    if not source.exists():
        return {"ready": False, "reason": "missing_file", "path": str(source)}
    frame = pd.read_parquet(source)
    base_required = {"instrument_id", "industry_code"}
    missing = sorted(base_required - set(frame.columns))
    if missing:
        return {"ready": False, "reason": "missing_columns", "missing": missing}
    if "decision_at" in frame:
        decision = pd.to_datetime(frame["decision_at"], utc=True, errors="coerce")
        local = decision.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
        covered_start = local.min()
        covered_end = local.max()
        temporal_contract = "decision_snapshot"
    elif {"membership_start", "membership_end"}.issubset(frame.columns):
        membership_start = pd.to_datetime(frame["membership_start"], errors="coerce")
        membership_end = pd.to_datetime(frame["membership_end"], errors="coerce")
        covered_start = membership_start.min()
        covered_end = membership_end.max()
        if pd.isna(covered_end):
            covered_end = end
        temporal_contract = "effective_interval"
    else:
        return {
            "ready": False,
            "reason": "missing_historical_time_contract",
            "required": ["decision_at", "or membership_start/membership_end"],
        }
    valid_dates = pd.notna(covered_start) and pd.notna(covered_end)
    covers = bool(valid_dates and covered_start <= start and covered_end >= end)
    return {
        "ready": covers,
        "path": str(source),
        "sha256": _sha256(source),
        "rows": len(frame),
        "instruments": int(frame["instrument_id"].nunique()),
        "temporal_contract": temporal_contract,
        "covered_start": str(covered_start.date()) if pd.notna(covered_start) else None,
        "covered_end": str(covered_end.date()) if pd.notna(covered_end) else None,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
