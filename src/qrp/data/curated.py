"""Versioned curated price artifacts derived from frozen raw inputs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from .adjustments import (
    AdjustmentSpec,
    adjustment_quality_summary,
    build_adjusted_price_view,
    build_causal_return_panel,
)
from .catalog import load_partitioned_snapshot


def build_curated_price_artifact(
    lake_root: Path,
    output_root: Path,
    start_date: str,
    end_date: str,
    mode: str = "causal_returns",
    as_of_date: Optional[str] = None,
    base_date: Optional[str] = None,
    as_of_ingested_at: Optional[str] = None,
) -> Path:
    bars = load_partitioned_snapshot(
        lake_root, "tushare", "daily_bars", start_date, end_date, as_of_ingested_at
    )
    factors = load_partitioned_snapshot(
        lake_root,
        "tushare",
        "adjustment_factors",
        start_date,
        end_date,
        as_of_ingested_at,
    )
    bar_dates = {
        entry["partition_values"]["trade_date"] for entry in bars.manifest_entries
    }
    factor_dates = {
        entry["partition_values"]["trade_date"] for entry in factors.manifest_entries
    }
    if bar_dates != factor_dates:
        raise ValueError(
            "Price/factor partition coverage differs: "
            f"bars_only={sorted(bar_dates - factor_dates)}, "
            f"factors_only={sorted(factor_dates - bar_dates)}"
        )
    if mode == "causal_returns":
        frame = build_causal_return_panel(bars.frame, factors.frame)
        policy: Dict[str, Any] = {
            "mode": mode,
            "version": "close_times_adj_factor_ratio_v1",
        }
    else:
        spec = AdjustmentSpec(mode=mode, as_of_date=as_of_date, base_date=base_date)
        frame = build_adjusted_price_view(bars.frame, factors.frame, spec)
        policy = {
            "mode": mode,
            "spec": spec.fingerprint,
            "as_of_date": as_of_date,
            "base_date": base_date,
        }
    identity = {
        "start_date": start_date,
        "end_date": end_date,
        "as_of_ingested_at": as_of_ingested_at,
        "bars_fingerprint": bars.fingerprint,
        "factors_fingerprint": factors.fingerprint,
        "policy": policy,
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / "prices" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    parquet_path = destination / "prices.parquet"
    expected_frame_sha = _frame_fingerprint(frame)
    if not parquet_path.exists():
        temporary = destination / f".prices.{os.getpid()}.tmp.parquet"
        frame.to_parquet(temporary, index=False, engine="pyarrow")
        os.replace(temporary, parquet_path)
    else:
        existing = _frame_fingerprint(pd.read_parquet(parquet_path))
        if existing != expected_frame_sha:
            raise IOError(f"Existing artifact content mismatch: {parquet_path}")
    output_sha = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    manifest = {
        "artifact_id": artifact_id,
        "identity": identity,
        "rows": len(frame),
        "columns": list(frame.columns),
        "logical_frame_sha256": expected_frame_sha,
        "quality": adjustment_quality_summary(frame),
        "output": {"path": str(parquet_path), "sha256": output_sha},
        "inputs": {
            "bars": [
                {"path": entry["path"], "sha256": entry["sha256"]}
                for entry in bars.manifest_entries
            ],
            "adjustment_factors": [
                {"path": entry["path"], "sha256": entry["sha256"]}
                for entry in factors.manifest_entries
            ],
        },
        "guardrails": {
            "raw_prices_overwritten": False,
            "volume_adjusted": False,
            "future_qfq_anchor_allowed": False,
        },
    }
    manifest_path = destination / "manifest.json"
    manifest_text = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str
    ) + "\n"
    if manifest_path.exists():
        if manifest_path.read_text(encoding="utf-8") != manifest_text:
            raise IOError(f"Existing artifact manifest mismatch: {manifest_path}")
    else:
        temporary_manifest = destination / f".manifest.{os.getpid()}.tmp"
        temporary_manifest.write_text(manifest_text, encoding="utf-8")
        os.replace(temporary_manifest, manifest_path)
    return destination


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    normalized = frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    hashed = pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    return hashlib.sha256(hashed).hexdigest()
