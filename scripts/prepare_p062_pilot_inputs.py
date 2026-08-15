#!/usr/bin/env python3
"""Create content-addressed, mechanical P0.6.3 one-year pilot inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Sequence

import pandas as pd

from qrp.execution import build_lagged_capacity_panel


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lake-root", default="data/lake")
    parser.add_argument("--tradability-artifact", required=True)
    parser.add_argument("--raw-start", required=True)
    parser.add_argument("--raw-end", required=True)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--cash-buffer", type=float, default=0.02)
    parser.add_argument("--output-root", default="artifacts/pilots/p062_one_year")
    args = parser.parse_args()

    symbols = sorted(set(args.symbol))
    if len(symbols) < 2:
        raise ValueError("pilot requires at least two frozen symbols")
    if not 0 <= args.cash_buffer < 1:
        raise ValueError("cash buffer must be in [0, 1)")
    lake_root = Path(args.lake_root)
    p05_root = Path(args.tradability_artifact)
    p05_manifest_path = p05_root / "manifest.json"
    p05_path = p05_root / "tradability.parquet"
    p05_manifest = json.loads(p05_manifest_path.read_text(encoding="utf-8"))
    if not p05_manifest.get("quality", {}).get("promotion_passed", False):
        raise ValueError("P0.5 artifact was not promoted")
    if _sha256(p05_path) != p05_manifest["output"]["sha256"]:
        raise ValueError("P0.5 artifact hash mismatch")

    entries = _select_symbol_snapshots(
        lake_root,
        symbols,
        args.raw_start,
        args.raw_end,
        datasets=("daily_bars", "daily_indicators", "adjustment_factors"),
    )
    frames: Dict[str, list[pd.DataFrame]] = {
        "daily_bars": [],
        "daily_indicators": [],
        "adjustment_factors": [],
    }
    input_refs = []
    for entry in entries:
        path = lake_root / entry["path"]
        digest = _sha256(path)
        if digest != entry["sha256"]:
            raise ValueError(f"raw snapshot hash mismatch: {path}")
        frames[entry["dataset"]].append(pd.read_parquet(path))
        input_refs.append(
            {
                "dataset": entry["dataset"],
                "symbol": entry["query"]["symbol"],
                "path": str(path),
                "sha256": digest,
            }
        )
    bars = pd.concat(frames["daily_bars"], ignore_index=True)
    indicators = pd.concat(frames["daily_indicators"], ignore_index=True)
    adjustments = pd.concat(frames["adjustment_factors"], ignore_index=True)
    capacity = build_lagged_capacity_panel(bars, indicators, adjustments)

    market = pd.read_parquet(p05_path)
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    market = market.loc[market["symbol"].isin(symbols)].copy()
    if set(market["symbol"].unique()) != set(symbols):
        missing = sorted(set(symbols) - set(market["symbol"].unique()))
        raise ValueError(f"frozen symbols absent from P0.5: {missing}")
    identity = market[["symbol", "trade_date", "instrument_id"]]
    capacity["trade_date"] = pd.to_datetime(capacity["trade_date"]).dt.normalize()
    capacity = identity.merge(
        capacity,
        on=["symbol", "trade_date"],
        how="left",
        validate="one_to_one",
    ).sort_values(["trade_date", "symbol"])
    required_capacity = [
        "adv20_shares_lag1",
        "adv20_amount_lag1",
        "adv60_amount_lag1",
        "median_amount20_lag1",
        "free_float_market_cap_lag1",
        "volatility20_daily_lag1",
        "capacity_available_at",
    ]
    coverage = capacity.groupby("trade_date", observed=True)[required_capacity].apply(
        lambda group: group.notna().all().all() and len(group) == len(symbols)
    )
    complete_dates = pd.DatetimeIndex(coverage.loc[coverage].index).sort_values()
    p05_dates = pd.DatetimeIndex(market["trade_date"].unique()).sort_values()
    first_sessions = (
        pd.Series(p05_dates, index=p05_dates)
        .groupby(p05_dates.to_period("M"))
        .first()
        .tolist()
    )
    if not set(first_sessions).issubset(set(complete_dates)):
        missing_dates = sorted(set(first_sessions) - set(complete_dates))
        raise ValueError(
            "capacity warm-up does not cover every rebalance session; "
            f"missing dates={missing_dates}"
        )
    raw_dates = pd.DatetimeIndex(pd.to_datetime(bars["trade_date"]).unique()).sort_values()
    target_rows = []
    target_weight = (1.0 - args.cash_buffer) / len(symbols)
    for trade_date in first_sessions:
        prior = raw_dates[raw_dates < trade_date]
        if prior.empty:
            raise ValueError(f"no prior session for target decision on {trade_date.date()}")
        decision_at = _local_close(prior[-1])
        daily_identity = identity.loc[identity["trade_date"] == trade_date]
        if len(daily_identity) != len(symbols):
            raise ValueError(f"identity is incomplete on {trade_date.date()}")
        for row in daily_identity.sort_values("symbol").to_dict("records"):
            target_rows.append(
                {
                    "trade_date": trade_date,
                    "instrument_id": row["instrument_id"],
                    "symbol": row["symbol"],
                    "target_weight": target_weight,
                    "decision_at": decision_at,
                    "target_policy": "frozen_ten_name_equal_weight_monthly_v1",
                }
            )
    targets = pd.DataFrame(target_rows)

    config = {
        "symbols": symbols,
        "raw_start": args.raw_start,
        "raw_end": args.raw_end,
        "cash_buffer": args.cash_buffer,
        "target_policy": "frozen_ten_name_equal_weight_monthly_v1",
        "selection_policy": "fixed_before_backtest_systems_pilot_not_alpha_evidence",
    }
    identity_payload = {
        "p05_artifact_id": p05_manifest["artifact_id"],
        "p05_sha256": _sha256(p05_path),
        "raw_inputs": input_refs,
        "config": config,
        "implementation_sha256": _sha256(Path(__file__)),
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    destination = Path(args.output_root) / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    capacity_path = destination / "capacity.parquet"
    targets_path = destination / "targets.parquet"
    _write_immutable_frame(capacity, capacity_path, ["trade_date", "symbol"])
    _write_immutable_frame(targets, targets_path, ["trade_date", "symbol"])
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": "p063_one_year_pilot_inputs_v1",
        "identity": identity_payload,
        "outputs": {
            "capacity": {
                "path": str(capacity_path),
                "rows": len(capacity),
                "sha256": _sha256(capacity_path),
            },
            "targets": {
                "path": str(targets_path),
                "rows": len(targets),
                "sha256": _sha256(targets_path),
            },
        },
        "quality": {
            "p05_sessions": len(p05_dates),
            "capacity_complete_sessions": len(complete_dates.intersection(p05_dates)),
            "rebalance_sessions": len(first_sessions),
            "symbols": len(symbols),
            "target_weight_sum": float(targets.groupby("trade_date")["target_weight"].sum().max()),
            "promotion_passed": True,
        },
        "limitations": [
            "Fixed names are suitable for a systems pilot, not an unbiased alpha claim.",
            "The mechanical target deliberately contains no optimized research signal.",
        ],
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    print(destination)
    return 0


def _select_symbol_snapshots(
    lake_root: Path,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    *,
    datasets: Sequence[str],
) -> list[Dict[str, Any]]:
    entries = [
        json.loads(line)
        for line in (lake_root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    selected = []
    for dataset in datasets:
        for symbol in symbols:
            eligible = [
                entry
                for entry in entries
                if entry.get("provider") == "tushare"
                and entry.get("dataset") == dataset
                and entry.get("query", {}).get("symbol") == symbol
                and entry.get("query", {}).get("start_date") <= start_date
                and entry.get("query", {}).get("end_date") >= end_date
            ]
            if not eligible:
                raise FileNotFoundError(
                    f"no covering raw {dataset} snapshot for {symbol} "
                    f"over {start_date}..{end_date}"
                )
            selected.append(
                max(eligible, key=lambda entry: pd.Timestamp(entry["written_at"]))
            )
    return selected


def _local_close(date: Any) -> pd.Timestamp:
    return (
        (pd.Timestamp(date).normalize() + pd.Timedelta(hours=16))
        .tz_localize("Asia/Shanghai")
        .tz_convert("UTC")
    )


def _frame_fingerprint(frame: pd.DataFrame, sort_columns: Sequence[str]) -> str:
    normalized = frame.sort_values(list(sort_columns)).reset_index(drop=True)
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def _write_immutable_frame(
    frame: pd.DataFrame, path: Path, sort_columns: Sequence[str]
) -> None:
    logical_sha = _frame_fingerprint(frame, sort_columns)
    if path.exists():
        if _frame_fingerprint(pd.read_parquet(path), sort_columns) != logical_sha:
            raise ValueError(f"immutable pilot input conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, path)


def _write_immutable_json(payload: Any, path: Path) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != text:
        raise ValueError(f"immutable pilot manifest conflict: {path}")
    if not path.exists():
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
