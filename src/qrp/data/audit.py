"""Reproducibility and integrity audits for the append-only data lake."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pandas as pd

from .contracts import DataContractError, validate_dataset


@dataclass
class LakeAuditReport:
    manifest_entries: int = 0
    files_checked: int = 0
    rows_checked: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["passed"] = self.passed
        return payload


def audit_lake(root: Path) -> LakeAuditReport:
    root = Path(root)
    report = LakeAuditReport()
    manifest = root / "manifest.jsonl"
    if not manifest.exists():
        report.errors.append({"type": "missing_manifest", "path": str(manifest)})
        return report
    entries = []
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            report.errors.append(
                {"type": "invalid_manifest_json", "line": line_number, "error": str(exc)}
            )
    report.manifest_entries = len(entries)
    latest_paths: Dict[str, str] = {}
    for entry in sorted(entries, key=lambda item: pd.Timestamp(item["written_at"])):
        latest_paths[_logical_partition_key(entry)] = entry.get("path", "")
    query_fingerprints: Dict[str, List[str]] = {}
    for entry in entries:
        relative = entry.get("path", "")
        path = root / relative
        if not path.exists():
            report.errors.append({"type": "missing_file", "path": relative})
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry.get("sha256"):
            report.errors.append({"type": "hash_mismatch", "path": relative})
            continue
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:
            report.errors.append(
                {"type": "parquet_read_error", "path": relative, "error": str(exc)}
            )
            continue
        report.files_checked += 1
        report.rows_checked += len(frame)
        if len(frame) != entry.get("rows"):
            report.errors.append(
                {
                    "type": "row_count_mismatch",
                    "path": relative,
                    "manifest_rows": entry.get("rows"),
                    "actual_rows": len(frame),
                }
            )
        try:
            validate_dataset(entry["dataset"], frame)
        except (DataContractError, KeyError) as exc:
            issue = {
                "type": "contract_failure",
                "path": relative,
                "error": str(exc),
            }
            if latest_paths.get(_logical_partition_key(entry)) != relative:
                issue["type"] = "superseded_contract_failure"
                report.warnings.append(issue)
            else:
                report.errors.append(issue)
        fingerprint = _query_fingerprint(entry)
        query_fingerprints.setdefault(fingerprint, []).append(relative)
    for paths in query_fingerprints.values():
        if len(paths) > 1:
            report.warnings.append({"type": "duplicate_logical_query", "paths": paths})
    return report


def compare_daily_bars(
    left: pd.DataFrame,
    right: pd.DataFrame,
    absolute_tolerances: Mapping[str, float] = None,
) -> Dict[str, Any]:
    tolerances = dict(
        absolute_tolerances
        or {"open": 0.01, "high": 0.01, "low": 0.01, "close": 0.01, "volume": 100.0, "amount": 1.0}
    )
    keys = ["symbol", "trade_date"]
    columns = list(tolerances)
    merged = left[keys + columns].merge(
        right[keys + columns], on=keys, how="outer", suffixes=("_left", "_right"), indicator=True
    )
    result: Dict[str, Any] = {
        "left_rows": len(left),
        "right_rows": len(right),
        "overlap_rows": int((merged["_merge"] == "both").sum()),
        "left_only_rows": int((merged["_merge"] == "left_only").sum()),
        "right_only_rows": int((merged["_merge"] == "right_only").sum()),
        "columns": {},
    }
    overlap = merged[merged["_merge"] == "both"]
    for column, tolerance in tolerances.items():
        difference = (overlap[f"{column}_left"] - overlap[f"{column}_right"]).abs()
        result["columns"][column] = {
            "tolerance": tolerance,
            "max_absolute_difference": None if difference.empty else float(difference.max()),
            "violations": int((difference > tolerance).sum()),
        }
    result["passed"] = (
        result["left_only_rows"] == 0
        and result["right_only_rows"] == 0
        and all(value["violations"] == 0 for value in result["columns"].values())
    )
    return result


def write_audit_report(report: LakeAuditReport, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _query_fingerprint(entry: Mapping[str, Any]) -> str:
    payload = {
        "provider": entry.get("provider"),
        "dataset": entry.get("dataset"),
        "query": entry.get("query"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _logical_partition_key(entry: Mapping[str, Any]) -> str:
    partition = entry.get("partition_values") or {}
    identity = {
        "provider": entry.get("provider"),
        "dataset": entry.get("dataset"),
        "selector": partition if partition else entry.get("query", {}),
    }
    return json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
