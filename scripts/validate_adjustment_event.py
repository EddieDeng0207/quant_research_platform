"""Validate adjustment math against a real vendor history without storing credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from qrp.config import load_env_file
from qrp.data.adjustments import (
    AdjustmentSpec,
    build_adjusted_price_view,
    build_causal_return_panel,
)
from qrp.data.providers.tushare import TushareProvider


def _fingerprint(frame: pd.DataFrame) -> str:
    normalized = frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="600000.SH")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument(
        "--output", default="artifacts/audits/real_adjustment_event_validation.json"
    )
    args = parser.parse_args()

    load_env_file(Path(".env"))
    provider = TushareProvider()
    bars = provider.fetch_daily_bars(args.symbol, args.start, args.end).frame
    factors = provider.fetch_adjustment_factors(args.symbol, args.start, args.end).frame
    causal = build_causal_return_panel(bars, factors)
    qfq = build_adjusted_price_view(
        bars,
        factors,
        AdjustmentSpec(mode="qfq_asof", as_of_date=args.end),
    )
    joined = causal.merge(
        qfq[["symbol", "trade_date", "adj_close"]],
        on=["symbol", "trade_date"],
        how="left",
        validate="one_to_one",
    )
    joined["raw_close_return_1d"] = joined.groupby("symbol", observed=True)[
        "close"
    ].pct_change(fill_method=None)
    events = joined.loc[joined["factor_change"].abs() > 1e-4].copy()
    if events.empty:
        raise RuntimeError("No material adjustment-factor event found in requested range")
    fields = [
        "symbol",
        "trade_date",
        "close",
        "adj_factor",
        "raw_close_return_1d",
        "factor_change",
        "total_return_1d",
        "adj_close",
        "available_at",
    ]
    payload = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": "tushare",
        "query": {"symbol": args.symbol, "start": args.start, "end": args.end},
        "input_rows": {"bars": len(bars), "adjustment_factors": len(factors)},
        "input_logical_sha256": {
            "bars": _fingerprint(bars),
            "adjustment_factors": _fingerprint(factors),
        },
        "formula": "(close_t * factor_t) / (close_t_minus_1 * factor_t_minus_1) - 1",
        "material_factor_change_threshold": 1e-4,
        "events": events[fields].to_dict("records"),
        "credential_values_recorded": False,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"validated {len(events)} material event(s) -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
