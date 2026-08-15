"""Causal, provider-compatible A-share price adjustment logic."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd

from .temporal import attach_available_at


class AdjustmentError(ValueError):
    """Raised when prices cannot be adjusted without ambiguity or leakage."""


PRICE_COLUMNS: Sequence[str] = ("open", "high", "low", "close")


@dataclass(frozen=True)
class AdjustmentSpec:
    mode: str
    as_of_date: Optional[str] = None
    base_date: Optional[str] = None
    version: str = "a_share_multiplicative_v2_knowledge_time"

    def validate(self) -> "AdjustmentSpec":
        if self.mode not in {"qfq_asof", "hfq", "total_return_index"}:
            raise AdjustmentError(f"Unsupported adjustment mode: {self.mode}")
        if self.mode == "qfq_asof" and not self.as_of_date:
            raise AdjustmentError("qfq_asof requires an explicit as_of_date")
        if self.mode == "total_return_index" and not self.base_date:
            raise AdjustmentError("total_return_index requires an explicit base_date")
        return self

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_adjusted_price_view(
    bars: pd.DataFrame,
    factors: pd.DataFrame,
    spec: AdjustmentSpec,
) -> pd.DataFrame:
    """Build a stamped research view; never mutate or replace raw prices."""
    spec.validate()
    merged = _merge_inputs(bars, factors)
    merged = attach_available_at(merged, "daily_bars")
    if spec.as_of_date:
        cutoff = pd.Timestamp(spec.as_of_date).normalize()
        merged = merged.loc[merged["trade_date"] <= cutoff].copy()
        if merged.empty:
            raise AdjustmentError("No observations exist on or before as_of_date")

    if spec.mode == "qfq_asof":
        anchor_rows = merged.sort_values("trade_date").groupby(
            "symbol", observed=True, as_index=False
        ).tail(1)
        anchors = anchor_rows.set_index("symbol")["adj_factor"]
        anchor_dates = anchor_rows.set_index("symbol")["trade_date"]
        merged["price_scale"] = merged["adj_factor"] / merged["symbol"].map(anchors)
        merged["adjustment_anchor_date"] = merged["symbol"].map(anchor_dates)
        merged["requested_as_of_date"] = pd.Timestamp(spec.as_of_date).normalize()
        view_available_at = (
            pd.Timestamp(spec.as_of_date).normalize() + pd.Timedelta(hours=16)
        ).tz_localize("Asia/Shanghai").tz_convert("UTC")
        merged["available_at"] = view_available_at
        merged["knowledge_cutoff"] = view_available_at
    elif spec.mode == "hfq":
        merged["price_scale"] = merged["adj_factor"]
        merged["adjustment_anchor_date"] = pd.NaT
        merged["knowledge_cutoff"] = merged["available_at"]
    else:
        base_date = pd.Timestamp(spec.base_date).normalize()
        base_rows = merged.loc[merged["trade_date"] == base_date, ["symbol", "close", "adj_factor"]]
        if base_rows["symbol"].duplicated().any():
            raise AdjustmentError("Duplicate total-return base rows")
        base_values = pd.Series(
            (base_rows["close"] * base_rows["adj_factor"]).to_numpy(),
            index=base_rows["symbol"].to_numpy(),
        )
        missing_symbols = sorted(set(merged["symbol"]) - set(base_rows["symbol"]))
        if missing_symbols:
            raise AdjustmentError(
                f"Missing base_date={spec.base_date} for symbols: {missing_symbols[:5]}"
            )
        merged["price_scale"] = merged["adj_factor"] / merged["symbol"].map(base_values)
        merged["adjustment_anchor_date"] = base_date
        base_available_at = (
            base_date + pd.Timedelta(hours=16)
        ).tz_localize("Asia/Shanghai").tz_convert("UTC")
        merged["available_at"] = merged["available_at"].where(
            merged["available_at"] >= base_available_at, base_available_at
        )
        merged["knowledge_cutoff"] = base_available_at

    if spec.mode == "total_return_index":
        for column in PRICE_COLUMNS:
            merged[f"adj_{column}"] = (
                merged[column] * merged["adj_factor"] / merged["symbol"].map(base_values) * 100.0
            )
    else:
        for column in PRICE_COLUMNS:
            merged[f"adj_{column}"] = merged[column] * merged["price_scale"]

    adjusted_columns = [f"adj_{column}" for column in PRICE_COLUMNS]
    if (merged[adjusted_columns] <= 0).any().any():
        raise AdjustmentError("Adjusted OHLC must remain positive")
    if (merged["adj_high"] < merged["adj_low"]).any():
        raise AdjustmentError("Adjusted high is below adjusted low")
    merged["adjustment_mode"] = spec.mode
    merged["adjustment_version"] = spec.version
    merged["adjustment_spec_sha256"] = spec.fingerprint
    # Volume and amount are deliberately raw. Turnover/market-cap features must
    # use dedicated share and daily-indicator fields, not synthetic volume fixes.
    merged["volume_adjustment"] = "none_raw_shares"
    return merged.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def build_causal_return_panel(
    bars: pd.DataFrame,
    factors: pd.DataFrame,
) -> pd.DataFrame:
    """Persistable panel of event-time total returns with no future anchor."""
    merged = _merge_inputs(bars, factors)
    merged = attach_available_at(merged, "daily_bars")
    merged = merged.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    merged["total_return_value"] = merged["close"] * merged["adj_factor"]
    merged["total_return_1d"] = merged.groupby("symbol", observed=True)[
        "total_return_value"
    ].pct_change(fill_method=None)
    merged["previous_trade_date"] = merged.groupby("symbol", observed=True)[
        "trade_date"
    ].shift(1)
    merged["factor_change"] = merged.groupby("symbol", observed=True)["adj_factor"].pct_change(
        fill_method=None
    )
    merged["return_convention"] = "close_times_adj_factor_ratio_v1"
    return merged


def adjustment_quality_summary(frame: pd.DataFrame) -> Dict[str, Any]:
    """Return machine-readable checks used in reports and promotion gates."""
    adjusted = [column for column in frame if column.startswith("adj_")]
    return {
        "rows": len(frame),
        "symbols": int(frame["symbol"].nunique()),
        "start_date": str(frame["trade_date"].min().date()),
        "end_date": str(frame["trade_date"].max().date()),
        "factor_missing": int(frame["adj_factor"].isna().sum()),
        "factor_nonpositive": int((frame["adj_factor"] <= 0).sum()),
        "factor_jump_rows": int((frame.get("factor_change", pd.Series(dtype=float)).abs() > 1e-12).sum()),
        "adjusted_nonpositive": int((frame[adjusted] <= 0).sum().sum()) if adjusted else 0,
    }


def _merge_inputs(bars: pd.DataFrame, factors: pd.DataFrame) -> pd.DataFrame:
    keys = ["symbol", "trade_date"]
    required_bars = set(keys) | set(PRICE_COLUMNS) | {"volume", "amount"}
    required_factors = set(keys) | {"adj_factor"}
    missing_bars = sorted(required_bars - set(bars.columns))
    missing_factors = sorted(required_factors - set(factors.columns))
    if missing_bars or missing_factors:
        raise AdjustmentError(
            f"Missing adjustment inputs: bars={missing_bars}, factors={missing_factors}"
        )
    if bars.duplicated(keys).any() or factors.duplicated(keys).any():
        raise AdjustmentError("Price adjustment inputs contain duplicate symbol-date keys")
    clean_bars = bars.copy()
    clean_factors = factors[keys + ["adj_factor"]].copy()
    clean_bars["trade_date"] = pd.to_datetime(clean_bars["trade_date"]).dt.normalize()
    clean_factors["trade_date"] = pd.to_datetime(clean_factors["trade_date"]).dt.normalize()
    merged = clean_bars.merge(
        clean_factors, on=keys, how="left", validate="one_to_one", indicator=True
    )
    missing = merged["_merge"] != "both"
    if missing.any():
        sample = merged.loc[missing, keys].head(5).to_dict("records")
        raise AdjustmentError(f"Missing adjustment factor for {int(missing.sum())} bars: {sample}")
    merged = merged.drop(columns="_merge")
    merged["adj_factor"] = pd.to_numeric(merged["adj_factor"], errors="coerce")
    if (
        merged["adj_factor"].isna().any()
        or (~np.isfinite(merged["adj_factor"])).any()
        or (merged["adj_factor"] <= 0).any()
    ):
        raise AdjustmentError("Adjustment factors must be finite and positive")
    return merged
