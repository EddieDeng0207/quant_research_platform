"""Select immutable raw snapshots by logical partition and ingestion cutoff."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd


@dataclass
class DatasetSnapshot:
    provider: str
    dataset: str
    frame: pd.DataFrame
    manifest_entries: List[Dict[str, Any]]
    fingerprint: str


def load_partitioned_snapshot(
    lake_root: Path,
    provider: str,
    dataset: str,
    start_date: str,
    end_date: str,
    as_of_ingested_at: Optional[str] = None,
    columns: Optional[Sequence[str]] = None,
) -> DatasetSnapshot:
    """Load the latest eligible file per trade-date partition without mixing vintages."""
    root = Path(lake_root)
    entries = [
        json.loads(line)
        for line in (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    ingestion_cutoff = (
        pd.Timestamp(as_of_ingested_at).tz_convert("UTC")
        if as_of_ingested_at and pd.Timestamp(as_of_ingested_at).tzinfo
        else pd.Timestamp(as_of_ingested_at, tz="UTC")
        if as_of_ingested_at
        else None
    )
    eligible = []
    for entry in entries:
        if entry.get("provider") != provider or entry.get("dataset") != dataset:
            continue
        trade_date_value = entry.get("partition_values", {}).get("trade_date")
        if not trade_date_value:
            continue
        trade_date = pd.Timestamp(trade_date_value).normalize()
        written_at = pd.Timestamp(entry["written_at"])
        if start <= trade_date <= end and (
            ingestion_cutoff is None or written_at <= ingestion_cutoff
        ):
            eligible.append((trade_date, written_at, entry))
    latest: Dict[pd.Timestamp, Any] = {}
    for trade_date, written_at, entry in sorted(eligible, key=lambda item: item[1]):
        latest[trade_date] = (written_at, entry)
    selected = [latest[date][1] for date in sorted(latest)]
    if not selected:
        raise FileNotFoundError(
            f"No partitioned {provider}/{dataset} data for {start_date}..{end_date}"
        )
    frames = []
    for entry in selected:
        path = root / entry["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise IOError(f"Input hash mismatch: {path}")
        frames.append(pd.read_parquet(path, columns=list(columns) if columns else None))
    fingerprint_payload = [{"path": entry["path"], "sha256": entry["sha256"]} for entry in selected]
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    nonempty_frames = [frame for frame in frames if not frame.empty]
    combined = (
        pd.concat(nonempty_frames, ignore_index=True)
        if nonempty_frames
        else frames[0].iloc[0:0].copy()
    )
    return DatasetSnapshot(
        provider=provider,
        dataset=dataset,
        frame=combined,
        manifest_entries=selected,
        fingerprint=fingerprint,
    )


def load_latest_snapshot(
    lake_root: Path,
    provider: str,
    dataset: str,
    as_of_ingested_at: Optional[str] = None,
) -> DatasetSnapshot:
    """Load one latest immutable snapshot for a non-date-partitioned dataset."""
    root = Path(lake_root)
    manifest_path = root / "manifest.jsonl"
    entries = [
        json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line
    ]
    ingestion_cutoff = _utc_cutoff(as_of_ingested_at)
    eligible = [
        entry
        for entry in entries
        if entry.get("provider") == provider
        and entry.get("dataset") == dataset
        and (ingestion_cutoff is None or pd.Timestamp(entry["written_at"]) <= ingestion_cutoff)
    ]
    if not eligible:
        raise FileNotFoundError(f"No eligible {provider}/{dataset} snapshot")
    selected = max(eligible, key=lambda entry: pd.Timestamp(entry["written_at"]))
    path = root / selected["path"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != selected["sha256"]:
        raise IOError(f"Input hash mismatch: {path}")
    fingerprint = hashlib.sha256(
        json.dumps(
            {"path": selected["path"], "sha256": selected["sha256"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return DatasetSnapshot(
        provider=provider,
        dataset=dataset,
        frame=pd.read_parquet(path),
        manifest_entries=[selected],
        fingerprint=fingerprint,
    )


def _utc_cutoff(value: Optional[str]) -> Optional[pd.Timestamp]:
    if not value:
        return None
    timestamp = pd.Timestamp(value)
    return timestamp.tz_convert("UTC") if timestamp.tzinfo else timestamp.tz_localize("UTC")
