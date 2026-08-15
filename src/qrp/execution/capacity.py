"""Point-in-time liquidity features and institutional capacity constraints."""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Sequence

import numpy as np
import pandas as pd

CAPACITY_FIELDS: tuple[str, ...] = (
    "adv20_shares_lag1",
    "adv20_amount_lag1",
    "adv60_amount_lag1",
    "median_amount20_lag1",
    "free_float_market_cap_lag1",
    "volatility20_daily_lag1",
)


def build_lagged_capacity_panel(
    bars: pd.DataFrame,
    daily_indicators: pd.DataFrame,
    adjustment_factors: pd.DataFrame,
    *,
    key_columns: Sequence[str] = ("symbol",),
    min_periods_20: int = 20,
    min_periods_60: int = 60,
) -> pd.DataFrame:
    """Create causal capacity inputs using data known before the execution day.

    Every rolling statistic is shifted by one security session.  The current
    day's final volume, amount and market value therefore never influence an
    order intended for that day's open.
    """
    required_bars = {*key_columns, "trade_date", "close", "volume", "amount"}
    required_indicators = {*key_columns, "trade_date", "circ_mv"}
    required_adjustments = {*key_columns, "trade_date", "adj_factor"}
    missing_bars = sorted(required_bars - set(bars.columns))
    missing_indicators = sorted(required_indicators - set(daily_indicators.columns))
    missing_adjustments = sorted(required_adjustments - set(adjustment_factors.columns))
    if missing_bars:
        raise ValueError(f"bars missing capacity columns: {missing_bars}")
    if missing_indicators:
        raise ValueError(f"daily_indicators missing capacity columns: {missing_indicators}")
    if missing_adjustments:
        raise ValueError(
            f"adjustment_factors missing volatility columns: {missing_adjustments}"
        )
    if not 1 <= min_periods_20 <= 20 or not 1 <= min_periods_60 <= 60:
        raise ValueError("rolling minimum periods must be within their windows")

    keys = list(key_columns)
    work = bars[[*keys, "trade_date", "close", "volume", "amount"]].copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"]).dt.normalize()
    if work.duplicated([*keys, "trade_date"]).any():
        raise ValueError("bars contain duplicate security-date rows")
    for column in ("close", "volume", "amount"):
        work[column] = pd.to_numeric(work[column], errors="coerce")
    if work[["close", "volume", "amount"]].isna().any().any():
        raise ValueError("bars contain invalid close, volume or amount")
    if (work[["close", "volume", "amount"]] <= 0).any().any():
        raise ValueError("bars require positive close, volume and amount")
    work = work.sort_values([*keys, "trade_date"]).reset_index(drop=True)

    adjustments = adjustment_factors[
        [*keys, "trade_date", "adj_factor"]
    ].copy()
    adjustments["trade_date"] = pd.to_datetime(
        adjustments["trade_date"]
    ).dt.normalize()
    if adjustments.duplicated([*keys, "trade_date"]).any():
        raise ValueError("adjustment_factors contain duplicate security-date rows")
    adjustments["adj_factor"] = pd.to_numeric(
        adjustments["adj_factor"], errors="coerce"
    )
    if adjustments["adj_factor"].isna().any() or (
        adjustments["adj_factor"] <= 0
    ).any():
        raise ValueError("adjustment_factors require positive adj_factor")
    work = work.merge(
        adjustments,
        on=[*keys, "trade_date"],
        how="left",
        validate="one_to_one",
    )
    if work["adj_factor"].isna().any():
        raise ValueError("adjustment factors do not fully cover capacity bars")
    work["corporate_action_neutral_close"] = work["close"] * work["adj_factor"]
    work["neutral_log_return"] = work.groupby(
        keys, sort=False, observed=True
    )["corporate_action_neutral_close"].transform(
        lambda values: np.log(values / values.shift(1))
    )

    grouped = work.groupby(keys, sort=False, observed=True)
    work["adv20_shares_lag1"] = grouped["volume"].transform(
        lambda values: values.rolling(20, min_periods=min_periods_20).mean().shift(1)
    )
    work["adv20_amount_lag1"] = grouped["amount"].transform(
        lambda values: values.rolling(20, min_periods=min_periods_20).mean().shift(1)
    )
    work["adv60_amount_lag1"] = grouped["amount"].transform(
        lambda values: values.rolling(60, min_periods=min_periods_60).mean().shift(1)
    )
    work["median_amount20_lag1"] = grouped["amount"].transform(
        lambda values: values.rolling(20, min_periods=min_periods_20).median().shift(1)
    )
    work["volatility20_daily_lag1"] = grouped["neutral_log_return"].transform(
        lambda values: values.rolling(
            20, min_periods=min_periods_20
        ).std(ddof=1).shift(1)
    )
    work["volatility20_observations_lag1"] = grouped[
        "neutral_log_return"
    ].transform(
        lambda values: values.rolling(
            20, min_periods=min_periods_20
        ).count().shift(1)
    )

    indicators = daily_indicators[[*keys, "trade_date", "circ_mv"]].copy()
    indicators["trade_date"] = pd.to_datetime(indicators["trade_date"]).dt.normalize()
    if indicators.duplicated([*keys, "trade_date"]).any():
        raise ValueError("daily_indicators contain duplicate security-date rows")
    indicators["circ_mv"] = pd.to_numeric(indicators["circ_mv"], errors="coerce")
    indicators = indicators.sort_values([*keys, "trade_date"])
    indicators["free_float_market_cap_lag1"] = indicators.groupby(
        keys, sort=False, observed=True
    )["circ_mv"].shift(1)

    panel = work.merge(
        indicators[[*keys, "trade_date", "free_float_market_cap_lag1"]],
        on=[*keys, "trade_date"],
        how="left",
        validate="one_to_one",
    )
    panel["capacity_inputs_complete"] = panel[list(CAPACITY_FIELDS)].apply(
        lambda column: pd.to_numeric(column, errors="coerce")
    ).gt(0).all(axis=1)
    panel["capacity_available_at"] = (
        panel["trade_date"] + pd.Timedelta(hours=9, minutes=20)
    ).dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
    panel["capacity_policy"] = "lagged_liquidity_volatility_v3"
    panel["volatility_policy"] = "adjusted_close_log_return_std20_lag1_v1"
    return panel[
        [
            *keys,
            "trade_date",
            *CAPACITY_FIELDS,
            "volatility20_observations_lag1",
            "capacity_inputs_complete",
            "capacity_available_at",
            "capacity_policy",
            "volatility_policy",
        ]
    ]


def assess_order_capacity(
    order: Mapping[str, Any],
    *,
    reference_price: float,
    side: str,
    current_position: int,
    submitted_quantity: int,
    lot_increment: int,
    minimum_lot: int,
    spec: Any,
) -> Dict[str, Any]:
    """Return the tightest causal share capacity and auditable diagnostics."""
    values = {field: _positive_float(order.get(field)) for field in CAPACITY_FIELDS}
    missing = [field for field, value in values.items() if not np.isfinite(value)]
    if missing and spec.require_institutional_capacity_inputs:
        return {"capacity_quantity": 0, "block_reason": f"missing_capacity_inputs:{','.join(missing)}"}

    legacy_adv = _positive_float(order.get("adv_shares_lag1"))
    if not np.isfinite(values["adv20_shares_lag1"]):
        values["adv20_shares_lag1"] = legacy_adv
    if not np.isfinite(values["adv20_shares_lag1"]):
        if spec.require_lagged_liquidity:
            return {"capacity_quantity": 0, "block_reason": "missing_adv20_shares_lag1"}
        values["adv20_shares_lag1"] = math.inf

    haircut = spec.liquidity_haircut
    constraints: Dict[str, float] = {
        "adv20_shares": values["adv20_shares_lag1"] * haircut * spec.max_participation_rate
    }
    amount_values = [
        values["adv20_amount_lag1"],
        values["adv60_amount_lag1"],
        values["median_amount20_lag1"],
    ]
    finite_amounts = [value for value in amount_values if np.isfinite(value)]
    reference_amount = min(finite_amounts) * haircut if finite_amounts else math.inf
    if np.isfinite(reference_amount):
        constraints["lagged_amount"] = (
            reference_amount * spec.max_participation_rate / reference_price
        )

    volatility = values["volatility20_daily_lag1"]
    impact_participation_limit = math.nan
    if np.isfinite(volatility):
        volatility_bps = volatility * 10_000.0
        impact_scale = spec.impact_y * volatility_bps
        if impact_scale > 0:
            impact_participation_limit = (
                spec.max_executable_impact_bps / impact_scale
            ) ** (1.0 / spec.impact_exponent)
            impact_participation_limit = min(1.0, impact_participation_limit)
            if np.isfinite(reference_amount):
                constraints["impact_tolerance"] = (
                    reference_amount
                    * impact_participation_limit
                    / reference_price
                )

    free_float = values["free_float_market_cap_lag1"]
    if side == "buy" and np.isfinite(free_float):
        max_position_shares = free_float * spec.max_position_free_float_fraction / reference_price
        constraints["free_float_position"] = max(0.0, max_position_shares - current_position)
    if side == "buy" and np.isfinite(reference_amount):
        max_stress_position_shares = (
            reference_amount
            * spec.stress_exit_participation_rate
            * spec.max_stress_exit_days
            / reference_price
        )
        constraints["stress_exit_days"] = max(0.0, max_stress_position_shares - current_position)

    binding = min(constraints, key=constraints.get)
    raw_capacity = min(float(submitted_quantity), constraints[binding])
    if raw_capacity >= submitted_quantity:
        capacity = submitted_quantity
    else:
        capacity = int(math.floor(max(0.0, raw_capacity) / lot_increment) * lot_increment)
    if capacity < minimum_lot:
        capacity = 0

    projected_position = (
        current_position + capacity if side == "buy" else max(0, current_position - capacity)
    )
    position_value = projected_position * reference_price
    normal_exit_days = _safe_ratio(
        position_value, reference_amount * spec.normal_exit_participation_rate
    )
    stress_exit_days = _safe_ratio(
        position_value, reference_amount * spec.stress_exit_participation_rate
    )
    order_amount = capacity * reference_price
    return {
        "capacity_quantity": capacity,
        "block_reason": "",
        "capacity_binding_constraint": binding,
        "capacity_adv20_shares": _finite_or_nan(constraints.get("adv20_shares")),
        "capacity_lagged_amount_shares": _finite_or_nan(constraints.get("lagged_amount")),
        "capacity_free_float_shares": _finite_or_nan(constraints.get("free_float_position")),
        "capacity_stress_exit_shares": _finite_or_nan(constraints.get("stress_exit_days")),
        "capacity_impact_shares": _finite_or_nan(
            constraints.get("impact_tolerance")
        ),
        "impact_participation_limit": _finite_or_nan(
            impact_participation_limit
        ),
        "liquidity_reference_amount_lag1": _finite_or_nan(reference_amount),
        "order_amount_participation_rate": _safe_ratio(order_amount, reference_amount),
        "projected_free_float_fraction": _safe_ratio(position_value, free_float),
        "normal_exit_days": normal_exit_days,
        "stress_exit_days": stress_exit_days,
    }


def _positive_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if np.isfinite(number) and number > 0 else math.nan


def _safe_ratio(numerator: float, denominator: float) -> float:
    if not np.isfinite(denominator) or denominator <= 0:
        return math.nan
    return float(numerator / denominator)


def _finite_or_nan(value: Any) -> float:
    return float(value) if value is not None and np.isfinite(value) else math.nan
