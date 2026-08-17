"""Target-weight order construction and account-level accounting helpers."""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Mapping, Optional, Tuple, Union

import numpy as np
import pandas as pd

from .capacity import CAPACITY_FIELDS
from .daily import ExecutionError


def net_orders(orders: pd.DataFrame) -> pd.DataFrame:
    """Net same-day same-security intents before fees and market impact are applied."""
    required = {"trade_date", "instrument_id", "symbol", "side", "quantity"}
    missing = sorted(required - set(orders.columns))
    if missing:
        raise ExecutionError(f"orders missing netting columns: {missing}")
    work = orders.copy()
    normalized_side = work["side"].astype(str).str.lower()
    if not normalized_side.isin(["buy", "sell"]).all():
        raise ExecutionError("order side must be buy or sell before netting")
    work["side"] = normalized_side
    work["trade_date"] = pd.to_datetime(work["trade_date"]).dt.normalize()
    work["signed_quantity"] = np.where(
        work["side"].astype(str).str.lower() == "buy",
        work["quantity"],
        -pd.to_numeric(work["quantity"], errors="raise"),
    )
    group_columns = ["trade_date", "instrument_id", "symbol"]
    passthrough = [
        column
        for column in ["limit_price", "signal_at", *CAPACITY_FIELDS, "adv_shares_lag1"]
        if column in work
    ]
    rows = []
    for key, group in work.groupby(group_columns, sort=True, observed=True):
        net = int(group["signed_quantity"].sum())
        if net == 0:
            continue
        row: Dict[str, Any] = dict(zip(group_columns, key))
        row["side"] = "buy" if net > 0 else "sell"
        row["quantity"] = abs(net)
        row["source_order_count"] = len(group)
        for column in passthrough:
            nonnull = group[column].dropna()
            if not nonnull.empty and nonnull.nunique(dropna=False) > 1:
                raise ExecutionError(
                    f"cannot net conflicting {column} for {row['instrument_id']} on {row['trade_date']}"
                )
            row[column] = nonnull.iloc[0] if not nonnull.empty else np.nan
        row["order_id"] = _order_id(row)
        rows.append(row)
    return pd.DataFrame(rows)


def generate_target_weight_orders(
    targets: pd.DataFrame,
    positions: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    portfolio_nav: float,
    cash_buffer_fraction: float = 0.02,
    liquidate_missing: bool = True,
    capacity_panel: Optional[pd.DataFrame] = None,
    minimum_routine_trade_notional_cny: float = 0.0,
    return_suppressed: bool = False,
) -> Union[pd.DataFrame, Tuple[pd.DataFrame, pd.DataFrame]]:
    """Convert a frozen target portfolio into deterministic, netted share orders.

    Targets are weights of total NAV.  The cash buffer is a hard ceiling on
    aggregate invested target weight, not an additional haircut applied twice.
    """
    if not np.isfinite(portfolio_nav) or portfolio_nav <= 0:
        raise ValueError("portfolio_nav must be finite and positive")
    if not 0 <= cash_buffer_fraction < 1:
        raise ValueError("cash_buffer_fraction must be in [0, 1)")
    if minimum_routine_trade_notional_cny < 0:
        raise ValueError("minimum_routine_trade_notional_cny must be non-negative")
    required_targets = {"trade_date", "instrument_id", "symbol", "target_weight"}
    required_positions = {"instrument_id", "total_quantity"}
    required_prices = {"trade_date", "instrument_id", "symbol", "reference_price"}
    for label, frame, required in (
        ("targets", targets, required_targets),
        ("positions", positions, required_positions),
        ("prices", prices, required_prices),
    ):
        missing = sorted(required - set(frame.columns))
        if missing:
            raise ExecutionError(f"{label} missing columns: {missing}")

    target = targets.copy()
    target["trade_date"] = pd.to_datetime(target["trade_date"]).dt.normalize()
    target["target_weight"] = pd.to_numeric(target["target_weight"], errors="coerce")
    if target["target_weight"].isna().any() or (target["target_weight"] < 0).any():
        raise ExecutionError("long-only target weights must be finite and non-negative")
    weight_sums = target.groupby("trade_date")["target_weight"].sum()
    if (weight_sums > 1.0 - cash_buffer_fraction + 1e-9).any():
        raise ExecutionError("target weights breach the required cash buffer")
    if target.duplicated(["trade_date", "instrument_id"]).any():
        raise ExecutionError("targets contain duplicate security-date rows")

    price = prices.copy()
    price["trade_date"] = pd.to_datetime(price["trade_date"]).dt.normalize()
    price["reference_price"] = pd.to_numeric(price["reference_price"], errors="coerce")
    if price.duplicated(["trade_date", "instrument_id"]).any():
        raise ExecutionError("prices contain duplicate security-date rows")
    current = positions[["instrument_id", "total_quantity"]].copy()
    current["total_quantity"] = pd.to_numeric(
        current["total_quantity"], errors="raise"
    ).astype(int)

    rebalance_dates = pd.DatetimeIndex(target["trade_date"].unique()).sort_values()
    if len(rebalance_dates) != 1:
        raise ExecutionError("one target-weight build handles exactly one rebalance date")
    trade_date = rebalance_dates[0]
    priced = price.loc[price["trade_date"] == trade_date]
    missing_target_prices = set(target["instrument_id"]) - set(priced["instrument_id"])
    missing_position_prices = (
        set(current.loc[current["total_quantity"] != 0, "instrument_id"])
        - set(priced["instrument_id"])
    )
    if missing_target_prices or (liquidate_missing and missing_position_prices):
        raise ExecutionError(
            "raw execution prices do not cover target/current positions: "
            f"targets={sorted(missing_target_prices)}, positions={sorted(missing_position_prices)}"
        )
    universe = priced.merge(
        current, on="instrument_id", how="left", validate="one_to_one"
    )
    universe["total_quantity"] = universe["total_quantity"].fillna(0).astype(int)
    universe = universe.merge(
        target[["instrument_id", "target_weight"]],
        on="instrument_id",
        how="left",
        validate="one_to_one",
    )
    universe["target_weight"] = universe["target_weight"].fillna(
        0.0 if liquidate_missing else np.nan
    )
    universe = universe.loc[universe["target_weight"].notna()].copy()
    # The daily P0.5 grid also contains securities that are neither held nor
    # targeted (including pre-listing and suspended rows without a price).
    # They cannot create an order and therefore must not contaminate the price
    # validation applied to genuine target or liquidation intents.
    requires_positioning = universe["target_weight"].gt(0) | universe[
        "total_quantity"
    ].ne(0)
    universe = universe.loc[requires_positioning].copy()
    invalid_prices = ~np.isfinite(universe["reference_price"]) | (
        universe["reference_price"] <= 0
    )
    if invalid_prices.any():
        raise ExecutionError("target order construction requires positive raw prices")

    universe["current_value"] = universe["total_quantity"] * universe["reference_price"]
    universe["target_value"] = universe["target_weight"] * portfolio_nav
    universe["delta_value"] = universe["target_value"] - universe["current_value"]
    universe["raw_delta_quantity"] = universe["delta_value"] / universe["reference_price"]
    rows = []
    suppressed_rows = []
    for record in universe.to_dict("records"):
        side = "buy" if record["raw_delta_quantity"] > 0 else "sell"
        requested = abs(int(np.trunc(record["raw_delta_quantity"])))
        exchange = str(record["symbol"]).rsplit(".", 1)[-1]
        increment = 1 if exchange == "BJ" else 100
        minimum = 100
        if side == "buy":
            quantity = requested // increment * increment
        elif requested >= record["total_quantity"]:
            quantity = record["total_quantity"]
        else:
            quantity = requested // increment * increment
        if quantity < minimum and not (
            side == "sell" and quantity == record["total_quantity"] and quantity > 0
        ):
            continue
        row = {
            "trade_date": trade_date,
            "instrument_id": record["instrument_id"],
            "symbol": record["symbol"],
            "side": side,
            "quantity": int(quantity),
            "target_weight": record["target_weight"],
            "target_value": record["target_value"],
            "current_value": record["current_value"],
            "reference_price_for_sizing": record["reference_price"],
            "delta_weight_for_sizing": (
                record["delta_value"] / portfolio_nav
            ),
        }
        full_exit = (
            side == "sell"
            and quantity == record["total_quantity"]
            and record["total_quantity"] > 0
        )
        row["order_reason"] = (
            "full_exit"
            if full_exit
            else "routine_rebalance"
        )
        row["order_id"] = _order_id(row)
        estimated_notional = quantity * record["reference_price"]
        if (
            row["order_reason"] == "routine_rebalance"
            and minimum_routine_trade_notional_cny > 0
            and estimated_notional < minimum_routine_trade_notional_cny
        ):
            suppressed_rows.append(
                {
                    **row,
                    "estimated_notional": estimated_notional,
                    "minimum_trade_notional": (
                        minimum_routine_trade_notional_cny
                    ),
                    "suppression_reason": "below_minimum_routine_trade_notional",
                }
            )
            continue
        rows.append(row)
    orders = pd.DataFrame(rows)
    suppressed = pd.DataFrame(suppressed_rows)
    if orders.empty:
        return (orders, suppressed) if return_suppressed else orders
    if capacity_panel is not None:
        panel = capacity_panel.copy()
        panel["trade_date"] = pd.to_datetime(panel["trade_date"]).dt.normalize()
        join_keys = ["instrument_id", "trade_date"] if "instrument_id" in panel else ["symbol", "trade_date"]
        columns = [*join_keys, *CAPACITY_FIELDS]
        orders = orders.merge(panel[columns], on=join_keys, how="left", validate="many_to_one")
        orders["adv_shares_lag1"] = orders["adv20_shares_lag1"]
    orders = orders.sort_values(
        ["trade_date", "side", "instrument_id"],
        key=lambda values: values.map({"sell": 0, "buy": 1}).fillna(values)
        if values.name == "side"
        else values,
    ).reset_index(drop=True)
    return (orders, suppressed) if return_suppressed else orders


def _order_id(row: Mapping[str, Any]) -> str:
    payload = "|".join(
        str(row.get(field, ""))
        for field in ("trade_date", "instrument_id", "side", "quantity")
    )
    return "ord_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
