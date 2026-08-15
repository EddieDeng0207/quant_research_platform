"""Reproducible execution sensitivity scenarios for daily/weekly strategies."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Sequence, Tuple

import pandas as pd

from .daily import ExecutionSpec, FeePolicy, simulate_orders


@dataclass(frozen=True)
class ExecutionScenario:
    name: str
    session_delay: int = 0
    max_participation_rate: Optional[float] = None
    liquidity_haircut: Optional[float] = None
    base_slippage_bps: Optional[float] = None
    impact_y: Optional[float] = None
    max_executable_impact_bps: Optional[float] = None
    minimum_routine_trade_notional_cny: Optional[float] = None


DEFAULT_SCENARIOS: tuple[ExecutionScenario, ...] = (
    ExecutionScenario(name="base_open"),
    ExecutionScenario(
        name="conservative_open",
        max_participation_rate=0.005,
        liquidity_haircut=0.50,
        base_slippage_bps=10.0,
        impact_y=1.0,
        max_executable_impact_bps=100.0,
    ),
    ExecutionScenario(
        name="commission_aware_open",
        minimum_routine_trade_notional_cny=16_666.67,
    ),
    ExecutionScenario(name="delay_one_session", session_delay=1),
)


def simulate_execution_scenarios(
    orders: pd.DataFrame,
    tradability: pd.DataFrame,
    *,
    initial_cash: float,
    spec: Optional[ExecutionSpec] = None,
    fees: Optional[FeePolicy] = None,
    scenarios: Sequence[ExecutionScenario] = DEFAULT_SCENARIOS,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Run independent ledgers under frozen base, stress and delay assumptions."""
    base_spec = (spec or ExecutionSpec()).validate()
    fee_policy = fees or FeePolicy()
    all_results = []
    summaries = []
    for scenario in scenarios:
        scenario_orders, dropped = _shift_orders(
            orders, tradability, scenario.session_delay
        )
        scenario_spec = replace(
            base_spec,
            max_participation_rate=(
                scenario.max_participation_rate
                if scenario.max_participation_rate is not None
                else base_spec.max_participation_rate
            ),
            liquidity_haircut=(
                scenario.liquidity_haircut
                if scenario.liquidity_haircut is not None
                else base_spec.liquidity_haircut
            ),
            base_slippage_bps=(
                scenario.base_slippage_bps
                if scenario.base_slippage_bps is not None
                else base_spec.base_slippage_bps
            ),
            impact_y=(
                scenario.impact_y
                if scenario.impact_y is not None
                else base_spec.impact_y
            ),
            max_executable_impact_bps=(
                scenario.max_executable_impact_bps
                if scenario.max_executable_impact_bps is not None
                else base_spec.max_executable_impact_bps
            ),
            minimum_routine_trade_notional_cny=(
                scenario.minimum_routine_trade_notional_cny
                if scenario.minimum_routine_trade_notional_cny is not None
                else base_spec.minimum_routine_trade_notional_cny
            ),
            version=f"{base_spec.version}:{scenario.name}",
        ).validate()
        if scenario_orders.empty:
            executions = pd.DataFrame()
            ending_cash = initial_cash
        else:
            executions, ledger = simulate_orders(
                scenario_orders,
                tradability,
                initial_cash=initial_cash,
                spec=scenario_spec,
                fees=fee_policy,
            )
            executions.insert(0, "scenario", scenario.name)
            all_results.append(executions)
            ending_cash = ledger.cash
        summaries.append(
            {
                "scenario": scenario.name,
                "session_delay": scenario.session_delay,
                "input_orders": len(orders),
                "covered_orders": len(scenario_orders),
                "uncovered_orders": dropped,
                "filled_orders": int(
                    executions["status"].isin(["filled", "partial"]).sum()
                )
                if not executions.empty
                else 0,
                "filled_shares": int(executions["filled_quantity"].sum())
                if not executions.empty
                else 0,
                "turnover_notional": float(executions["notional"].sum())
                if not executions.empty
                else 0.0,
                "total_fees": float(executions["total_fees"].sum())
                if not executions.empty
                else 0.0,
                "total_slippage_cost": float(executions["slippage_cost"].sum())
                if not executions.empty
                else 0.0,
                "ending_cash": float(ending_cash),
                "execution_spec_sha256": scenario_spec.fingerprint,
            }
        )
    combined = pd.concat(all_results, ignore_index=True) if all_results else pd.DataFrame()
    return combined, pd.DataFrame(summaries)


def _shift_orders(
    orders: pd.DataFrame, tradability: pd.DataFrame, delay: int
) -> Tuple[pd.DataFrame, int]:
    work = orders.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"]).dt.normalize()
    if delay == 0:
        return work, 0
    available = tradability[["instrument_id", "trade_date", "symbol"]].copy()
    available["trade_date"] = pd.to_datetime(available["trade_date"]).dt.normalize()
    calendars = {
        instrument_id: group.sort_values("trade_date").reset_index(drop=True)
        for instrument_id, group in available.groupby("instrument_id", observed=True)
    }
    rows = []
    dropped = 0
    for row in work.to_dict("records"):
        calendar = calendars.get(str(row["instrument_id"]))
        if calendar is None:
            dropped += 1
            continue
        later = calendar.loc[calendar["trade_date"] > row["trade_date"]]
        if len(later) < delay:
            dropped += 1
            continue
        destination = later.iloc[delay - 1]
        row["signal_trade_date"] = row["trade_date"]
        row["trade_date"] = destination["trade_date"]
        row["symbol"] = destination["symbol"]
        row["order_id"] = f"{row['order_id']}__delay{delay}"
        rows.append(row)
    return pd.DataFrame(rows, columns=[*work.columns, "signal_trade_date"]), dropped
