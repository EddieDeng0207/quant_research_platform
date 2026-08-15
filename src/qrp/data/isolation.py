"""Physical research-split isolation and auditable access control."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import pandas as pd


class IsolationError(RuntimeError):
    """Raised when a temporal split or access rule would leak information."""


@dataclass(frozen=True)
class SplitWindow:
    name: str
    start_date: str
    end_date: str

    def bounds(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        return pd.Timestamp(self.start_date).normalize(), pd.Timestamp(self.end_date).normalize()


@dataclass(frozen=True)
class TemporalIsolationPlan:
    train: SplitWindow
    validation: SplitWindow
    holdout: SplitWindow
    label_horizon_sessions: int = 1
    embargo_sessions: int = 1
    version: str = "purged_walk_forward_v1"

    def validate(self, sessions: Sequence[pd.Timestamp]) -> Dict[str, pd.DatetimeIndex]:
        if self.label_horizon_sessions < 1 or self.embargo_sessions < 0:
            raise IsolationError("label horizon must be positive and embargo non-negative")
        calendar = pd.DatetimeIndex(pd.to_datetime(list(sessions))).normalize().unique().sort_values()
        if calendar.empty:
            raise IsolationError("Trading-session calendar is empty")
        if tuple(item.name for item in (self.train, self.validation, self.holdout)) != (
            "train",
            "validation",
            "holdout",
        ):
            raise IsolationError("Split windows must be named train, validation, holdout")
        selected: Dict[str, pd.DatetimeIndex] = {}
        prior_end_position: Optional[int] = None
        for window in (self.train, self.validation, self.holdout):
            start, end = window.bounds()
            if start > end:
                raise IsolationError(f"Invalid {window.name} window")
            dates = calendar[(calendar >= start) & (calendar <= end)]
            if dates.empty:
                raise IsolationError(f"{window.name} contains no trading sessions")
            start_position = int(calendar.get_loc(dates[0]))
            end_position = int(calendar.get_loc(dates[-1]))
            if prior_end_position is not None:
                gap = start_position - prior_end_position - 1
                if gap < self.embargo_sessions:
                    raise IsolationError(
                        f"{window.name} has {gap} embargo sessions; "
                        f"requires {self.embargo_sessions}"
                    )
            selected[window.name] = dates
            prior_end_position = end_position
        return selected


def apply_temporal_isolation(
    frame: pd.DataFrame,
    plan: TemporalIsolationPlan,
    sessions: Sequence[pd.Timestamp],
    time_column: str = "trade_date",
    label_end_column: Optional[str] = None,
) -> tuple[Dict[str, pd.DataFrame], Dict[str, Any]]:
    """Split a panel and purge rows whose forward label crosses a boundary."""
    if time_column not in frame:
        raise IsolationError(f"Missing split time column: {time_column}")
    windows = plan.validate(sessions)
    data = frame.copy()
    data[time_column] = pd.to_datetime(data[time_column]).dt.normalize()
    if label_end_column:
        if label_end_column not in data:
            raise IsolationError(f"Missing label end column: {label_end_column}")
        data[label_end_column] = pd.to_datetime(data[label_end_column]).dt.normalize()
    result: Dict[str, pd.DataFrame] = {}
    audit: Dict[str, Any] = {"version": plan.version, "splits": {}}
    for window in (plan.train, plan.validation, plan.holdout):
        dates = windows[window.name]
        candidate = data.loc[data[time_column].isin(dates)].copy()
        usable_count = max(0, len(dates) - plan.label_horizon_sessions)
        usable_dates = dates[:usable_count]
        keep = candidate[time_column].isin(usable_dates)
        if label_end_column:
            keep &= candidate[label_end_column].notna()
            keep &= candidate[label_end_column] <= dates[-1]
        isolated = candidate.loc[keep].copy()
        result[window.name] = isolated
        audit["splits"][window.name] = {
            "window_start": str(dates[0].date()),
            "window_end": str(dates[-1].date()),
            "usable_feature_end": str(usable_dates[-1].date()) if len(usable_dates) else None,
            "candidate_rows": len(candidate),
            "kept_rows": len(isolated),
            "purged_rows": len(candidate) - len(isolated),
        }
    assigned = pd.concat(result.values(), ignore_index=True) if result else pd.DataFrame()
    audit["input_rows"] = len(data)
    audit["kept_rows"] = len(assigned)
    audit["unassigned_or_purged_rows"] = len(data) - len(assigned)
    return result, audit


def seal_isolated_dataset(
    splits: Mapping[str, pd.DataFrame],
    destination_root: Path,
    dataset_id: str,
    plan: TemporalIsolationPlan,
    feature_columns: Sequence[str],
    label_columns: Sequence[str],
    key_columns: Sequence[str] = ("symbol", "trade_date"),
    audit: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Physically separate features and labels; holdout remains sealed by default."""
    expected = {"train", "validation", "holdout"}
    if set(splits) != expected:
        raise IsolationError(f"Expected split names {sorted(expected)}")
    overlap = set(feature_columns) & set(label_columns)
    if overlap:
        raise IsolationError(f"Features contain target columns: {sorted(overlap)}")
    root = Path(destination_root) / f"dataset_id={dataset_id}"
    files = []
    for split_name in ("train", "validation", "holdout"):
        frame = splits[split_name]
        required = set(key_columns) | set(feature_columns) | set(label_columns)
        missing = sorted(required - set(frame.columns))
        if missing:
            raise IsolationError(f"{split_name} missing columns: {missing}")
        if frame.duplicated(list(key_columns)).any():
            raise IsolationError(f"{split_name} contains duplicate keys")
        for kind, columns in (
            ("features", list(key_columns) + list(feature_columns)),
            ("labels", list(key_columns) + list(label_columns)),
        ):
            target = root / f"split={split_name}" / f"kind={kind}" / "data.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_once(frame.loc[:, columns], target)
            files.append(
                {
                    "split": split_name,
                    "kind": kind,
                    "path": str(target.relative_to(root)),
                    "rows": len(frame),
                    "columns": columns,
                    "sha256": _sha256(target),
                }
            )
    manifest = {
        "dataset_id": dataset_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan": asdict(plan),
        "feature_columns": list(feature_columns),
        "label_columns": list(label_columns),
        "key_columns": list(key_columns),
        "files": files,
        "isolation_audit": dict(audit or {}),
        "access_policy": "development_denies_holdout_v1",
    }
    manifest_path = root / "manifest.json"
    _write_json_once(manifest, manifest_path)
    return root


class ResearchDataGate:
    """Read sealed split files under a least-privilege, logged policy."""

    def __init__(self, dataset_root: Path, mode: str = "development") -> None:
        if mode not in {"development", "holdout_evaluation"}:
            raise IsolationError(f"Unsupported access mode: {mode}")
        self.root = Path(dataset_root)
        self.mode = mode
        self.manifest_path = self.root / "manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def read(
        self,
        split: str,
        kind: str,
        grant_path: Optional[Path] = None,
        purpose: str = "research",
    ) -> pd.DataFrame:
        if split not in {"train", "validation", "holdout"}:
            raise IsolationError(f"Unknown split: {split}")
        if kind not in {"features", "labels"}:
            raise IsolationError(f"Unknown dataset kind: {kind}")
        if split == "holdout":
            if self.mode != "holdout_evaluation":
                self._log_access(split, kind, purpose, "denied")
                raise IsolationError("Holdout access is denied in development mode")
            try:
                self._validate_grant(grant_path, purpose)
            except (IsolationError, FileNotFoundError, json.JSONDecodeError):
                self._log_access(split, kind, purpose, "denied")
                raise
        path = self.root / f"split={split}" / f"kind={kind}" / "data.parquet"
        record = next(
            item for item in self.manifest["files"]
            if item["split"] == split and item["kind"] == kind
        )
        if _sha256(path) != record["sha256"]:
            raise IsolationError(f"Sealed file hash mismatch: {path}")
        frame = pd.read_parquet(path)
        self._log_access(split, kind, purpose, "granted")
        return frame

    def _validate_grant(self, grant_path: Optional[Path], purpose: str) -> None:
        if grant_path is None:
            raise IsolationError("Holdout evaluation requires an explicit grant file")
        grant = json.loads(Path(grant_path).read_text(encoding="utf-8"))
        required = {"dataset_id", "manifest_sha256", "approved_by", "approved_at", "purpose"}
        if required - set(grant):
            raise IsolationError("Holdout grant is missing required fields")
        if grant["dataset_id"] != self.manifest["dataset_id"]:
            raise IsolationError("Holdout grant targets a different dataset")
        if grant["manifest_sha256"] != _sha256(self.manifest_path):
            raise IsolationError("Holdout grant does not match the sealed manifest")
        if grant["purpose"] != purpose:
            raise IsolationError("Holdout grant purpose does not match this access")

    def _log_access(self, split: str, kind: str, purpose: str, outcome: str) -> None:
        record = {
            "accessed_at": datetime.now(timezone.utc).isoformat(),
            "dataset_id": self.manifest["dataset_id"],
            "mode": self.mode,
            "split": split,
            "kind": kind,
            "purpose": purpose,
            "outcome": outcome,
        }
        _append_hash_chained_record(self.root / "access_log.jsonl", record)


def _write_once(frame: pd.DataFrame, path: Path) -> None:
    if path.exists():
        raise IsolationError(f"Sealed dataset path already exists: {path}")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    frame.to_parquet(temporary, index=False, engine="pyarrow")
    os.replace(temporary, path)


def _write_json_once(payload: Mapping[str, Any], path: Path) -> None:
    if path.exists():
        raise IsolationError(f"Sealed manifest already exists: {path}")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _append_hash_chained_record(path: Path, record: Mapping[str, Any]) -> None:
    """Serialize concurrent Agent access and make log edits detectable."""
    with path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            fcntl = None
        try:
            handle.seek(0)
            lines = [line for line in handle.read().splitlines() if line]
            previous = (
                json.loads(lines[-1])["entry_sha256"] if lines else "0" * 64
            )
            chained = {**record, "previous_entry_sha256": previous}
            encoded = json.dumps(chained, sort_keys=True, separators=(",", ":"))
            chained["entry_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(chained, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
