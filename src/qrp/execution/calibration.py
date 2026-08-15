"""Broker-fill calibration for daily execution assumptions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from .daily import ExecutionError

REQUIRED_FILL_COLUMNS = {
    "order_id",
    "trade_date",
    "instrument_id",
    "side",
    "arrival_price",
    "filled_price",
    "filled_quantity",
    "commission",
    "tax",
    "transfer_fee",
    "adv20_amount_lag1",
}


def calibrate_broker_fills(
    fills: pd.DataFrame, *, minimum_group_samples: int = 30
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Estimate robust slippage/fee observations without silently overfitting."""
    missing = sorted(REQUIRED_FILL_COLUMNS - set(fills.columns))
    if missing:
        raise ExecutionError(f"broker fills missing columns: {missing}")
    work = fills.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"]).dt.normalize()
    numeric = [
        "arrival_price",
        "filled_price",
        "filled_quantity",
        "commission",
        "tax",
        "transfer_fee",
        "adv20_amount_lag1",
    ]
    work[numeric] = work[numeric].apply(pd.to_numeric, errors="coerce")
    if work[numeric].isna().any().any() or (
        work[["arrival_price", "filled_price", "filled_quantity", "adv20_amount_lag1"]] <= 0
    ).any().any():
        raise ExecutionError("broker fills contain invalid prices, quantities or liquidity")
    if not work["side"].isin(["buy", "sell"]).all():
        raise ExecutionError("broker fill side must be buy or sell")
    direction = work["side"].map({"buy": 1.0, "sell": -1.0})
    work["notional"] = work["filled_price"] * work["filled_quantity"]
    work["signed_slippage_bps"] = (
        direction
        * (work["filled_price"] / work["arrival_price"] - 1.0)
        * 10_000.0
    )
    work["all_in_fee_bps"] = (
        (work["commission"] + work["tax"] + work["transfer_fee"])
        / work["notional"]
        * 10_000.0
    )
    work["amount_participation"] = work["notional"] / work["adv20_amount_lag1"]
    work["participation_bucket"] = pd.cut(
        work["amount_participation"],
        bins=[-np.inf, 0.001, 0.005, 0.01, 0.02, 0.05, np.inf],
        labels=["<=0.1%", "0.1-0.5%", "0.5-1%", "1-2%", "2-5%", ">5%"],
    ).astype(str)
    summary = (
        work.groupby(["side", "participation_bucket"], observed=True)
        .agg(
            samples=("order_id", "count"),
            median_slippage_bps=("signed_slippage_bps", "median"),
            p75_slippage_bps=("signed_slippage_bps", lambda values: values.quantile(0.75)),
            p90_slippage_bps=("signed_slippage_bps", lambda values: values.quantile(0.90)),
            median_fee_bps=("all_in_fee_bps", "median"),
            turnover_notional=("notional", "sum"),
        )
        .reset_index()
    )
    summary["calibration_ready"] = summary["samples"] >= minimum_group_samples
    diagnostics = {
        "rows": len(work),
        "start_date": str(work["trade_date"].min().date()),
        "end_date": str(work["trade_date"].max().date()),
        "minimum_group_samples": minimum_group_samples,
        "ready_groups": int(summary["calibration_ready"].sum()),
        "unready_groups": int((~summary["calibration_ready"]).sum()),
        "recommended_base_slippage_bps": (
            float(summary.loc[summary["calibration_ready"], "p75_slippage_bps"].median())
            if summary["calibration_ready"].any()
            else None
        ),
        "implementation_ready": bool(summary["calibration_ready"].any()),
    }
    return summary, diagnostics


def build_calibration_artifact(
    fills_path: Path, output_root: Path, *, minimum_group_samples: int = 30
) -> Path:
    """Freeze broker-fill calibration inputs and robust grouped estimates."""
    fills_path = Path(fills_path)
    fills = pd.read_parquet(fills_path) if fills_path.suffix.lower() in {".pq", ".parquet"} else pd.read_csv(fills_path)
    summary, diagnostics = calibrate_broker_fills(
        fills, minimum_group_samples=minimum_group_samples
    )
    identity = {
        "fills_sha256": _sha256(fills_path),
        "minimum_group_samples": minimum_group_samples,
        "implementation_sha256": _sha256(Path(__file__)),
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / "execution_calibration" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    summary_path = destination / "calibration_summary.parquet"
    manifest_path = destination / "manifest.json"
    _write_immutable_parquet(summary, summary_path)
    manifest = {
        "artifact_id": artifact_id,
        "identity": identity,
        "diagnostics": diagnostics,
        "outputs": {"summary": str(summary_path)},
        "guardrail": "parameters remain uncalibrated until implementation_ready=true",
    }
    _write_immutable_json(manifest, manifest_path)
    return destination


def _write_immutable_parquet(frame: pd.DataFrame, path: Path) -> None:
    if path.exists():
        if not frame.equals(pd.read_parquet(path)):
            raise ExecutionError(f"immutable calibration conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, path)


def _write_immutable_json(payload: Dict[str, Any], path: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise ExecutionError(f"immutable calibration conflict: {path}")
    if not path.exists():
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
