"""Chronological, fail-closed portfolio backtest orchestration."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from qrp.execution.capacity import CAPACITY_FIELDS
from qrp.execution.daily import (
    DailyExecutionEngine,
    ExecutionError,
    ExecutionSpec,
    FeePolicy,
    PortfolioLedger,
)
from qrp.execution.portfolio import generate_target_weight_orders
from qrp.execution.scenarios import DEFAULT_SCENARIOS, ExecutionScenario


@dataclass(frozen=True)
class BacktestSpec:
    cash_buffer_fraction: float = 0.02
    unfilled_order_policy: str = "cancel_and_rebuild_from_active_target"
    max_stale_valuation_sessions: int = 20
    target_weight_tolerance: float = 1e-6
    version: str = "a_share_daily_portfolio_backtest_v2_p063"

    def validate(self) -> "BacktestSpec":
        if not 0 <= self.cash_buffer_fraction < 1:
            raise ValueError("cash_buffer_fraction must be in [0, 1)")
        if self.unfilled_order_policy != "cancel_and_rebuild_from_active_target":
            raise ValueError("unsupported unfilled order policy")
        if self.max_stale_valuation_sessions < 1:
            raise ValueError("max_stale_valuation_sessions must be positive")
        if self.target_weight_tolerance < 0:
            raise ValueError("target_weight_tolerance must be non-negative")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(asdict(self))


@dataclass
class BacktestResult:
    daily_nav: pd.DataFrame
    daily_positions: pd.DataFrame
    target_weights: pd.DataFrame
    orders: pd.DataFrame
    suppressed_orders: pd.DataFrame
    executions: pd.DataFrame
    corporate_action_ledger: pd.DataFrame
    capacity_history: pd.DataFrame
    scenario_summary: pd.DataFrame


def run_portfolio_backtest(
    targets: pd.DataFrame,
    tradability: pd.DataFrame,
    capacity_panel: pd.DataFrame,
    *,
    initial_cash: float,
    corporate_actions: Optional[pd.DataFrame] = None,
    initial_positions: Optional[pd.DataFrame] = None,
    backtest_spec: Optional[BacktestSpec] = None,
    execution_spec: Optional[ExecutionSpec] = None,
    fees: Optional[FeePolicy] = None,
    scenarios: Sequence[ExecutionScenario] = DEFAULT_SCENARIOS,
) -> BacktestResult:
    """Run independent chronological ledgers for every frozen execution scenario."""
    bt_spec = (backtest_spec or BacktestSpec()).validate()
    exec_spec = (execution_spec or ExecutionSpec()).validate()
    fee_policy = fees or FeePolicy()
    relevant_ids = _relevant_instrument_ids(
        targets, corporate_actions, initial_positions
    )
    market = _prepare_market(tradability, capacity_panel, relevant_ids)
    calendar = pd.DatetimeIndex(market["trade_date"].unique()).sort_values()
    frozen_targets = _prepare_targets(targets, market, calendar, bt_spec)
    actions = _prepare_corporate_actions(corporate_actions, market, calendar)
    seeded = _prepare_initial_positions(initial_positions)
    if not set(seeded["instrument_id"]).issubset(set(market["instrument_id"])):
        raise ExecutionError("initial position is outside the backtest market")
    if not scenarios:
        raise ExecutionError("at least one execution scenario is required")
    scenario_names = [scenario.name for scenario in scenarios]
    if len(scenario_names) != len(set(scenario_names)):
        raise ExecutionError("execution scenario names must be unique")

    all_nav = []
    all_positions = []
    all_targets = []
    all_orders = []
    all_suppressed = []
    all_executions = []
    all_actions = []
    all_capacity = []
    summaries = []
    for scenario in scenarios:
        result = _run_scenario(
            scenario,
            frozen_targets,
            market,
            calendar,
            actions,
            seeded,
            initial_cash,
            bt_spec,
            exec_spec,
            fee_policy,
        )
        all_nav.append(result["daily_nav"])
        all_positions.append(result["daily_positions"])
        all_targets.append(result["target_weights"])
        all_orders.append(result["orders"])
        all_suppressed.append(result["suppressed_orders"])
        all_executions.append(result["executions"])
        all_actions.append(result["corporate_action_ledger"])
        all_capacity.append(result["capacity_history"])
        summaries.append(result["scenario_summary"])
    return BacktestResult(
        daily_nav=_concat(all_nav),
        daily_positions=_concat(all_positions),
        target_weights=_concat(all_targets),
        orders=_concat(all_orders),
        suppressed_orders=_concat(all_suppressed),
        executions=_concat(all_executions),
        corporate_action_ledger=_concat(all_actions),
        capacity_history=_concat(all_capacity),
        scenario_summary=pd.DataFrame(summaries),
    )


def _run_scenario(
    scenario: ExecutionScenario,
    targets: pd.DataFrame,
    market: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    actions: pd.DataFrame,
    initial_positions: pd.DataFrame,
    initial_cash: float,
    bt_spec: BacktestSpec,
    base_exec_spec: ExecutionSpec,
    fees: FeePolicy,
) -> Dict[str, Any]:
    scenario_spec = _scenario_execution_spec(base_exec_spec, scenario)
    activation = _target_activation_schedule(targets, calendar, scenario.session_delay)
    ledger = PortfolioLedger(initial_cash)
    for row in initial_positions.to_dict("records"):
        ledger.seed_position(
            row["instrument_id"],
            row["total_quantity"],
            row["sellable_quantity"],
            row["average_cost"],
        )
    engine = DailyExecutionEngine(ledger, spec=scenario_spec, fees=fees)
    market_index = market.set_index(["instrument_id", "trade_date"], drop=False)
    active_target = pd.DataFrame()
    pending_instruments: Optional[set[str]] = None
    last_prices: Dict[str, float] = {
        str(row["instrument_id"]): float(row["last_price"])
        for row in initial_positions.to_dict("records")
    }
    stale_sessions: Dict[str, int] = {
        str(row["instrument_id"]): 0
        for row in initial_positions.to_dict("records")
    }
    dividend_entitlements: Dict[str, int] = {}
    dividend_receivables: Dict[str, float] = {}
    nav_rows = []
    position_rows = []
    target_rows = []
    order_rows = []
    suppressed_rows = []
    execution_rows = []
    action_rows = []
    capacity_rows = []
    initial_nav = float(initial_cash) + _seed_market_value(initial_positions)
    previous_nav = initial_nav
    cumulative_fees = 0.0
    cumulative_slippage = 0.0
    cumulative_turnover = 0.0

    for trade_date in calendar:
        ledger.advance(trade_date)
        day_market = market.loc[market["trade_date"] == trade_date].copy()
        _refresh_preopen_prices(day_market, last_prices)
        day_actions = actions.loc[actions["processing_date"] == trade_date]
        open_actions = day_actions.loc[
            day_actions["processing_stage"].isin(["pre_open", "ex_open", "pay_open"])
        ].copy()
        open_actions["stage_sequence"] = open_actions["processing_stage"].map(
            {"pre_open": 0, "ex_open": 1, "pay_open": 2}
        )
        for action in open_actions.sort_values(
            ["stage_sequence", "action_id"]
        ).to_dict("records"):
            applied = dict(action)
            applied["effective_date"] = trade_date
            if action["action_type"] == "cash_dividend":
                entitlement_key = str(action["action_id"])
                if entitlement_key not in dividend_entitlements:
                    raise ExecutionError(
                        f"cash dividend lacks record-date entitlement: {entitlement_key}"
                    )
                entitled = dividend_entitlements[entitlement_key]
                applied["entitled_quantity"] = entitled
                net_cash = (
                    entitled
                    * float(action["cash_per_share"])
                    * (1.0 - float(action["withholding_tax_rate"]))
                )
                if action["processing_stage"] == "ex_open":
                    dividend_receivables[entitlement_key] = net_cash
                    event = {
                        "instrument_id": action["instrument_id"],
                        "action_type": action["action_type"],
                        "effective_date": trade_date,
                        "quantity_before": ledger.position(
                            str(action["instrument_id"])
                        ).total_quantity,
                        "quantity_after": ledger.position(
                            str(action["instrument_id"])
                        ).total_quantity,
                        "cash_before": ledger.cash,
                        "cash_after": ledger.cash,
                        "receivable_after": net_cash,
                    }
                else:
                    receivable = dividend_receivables.get(entitlement_key)
                    if receivable is None:
                        raise ExecutionError(
                            f"cash dividend lacks ex-date receivable: {entitlement_key}"
                        )
                    event = ledger.apply_corporate_action(applied)
                    dividend_receivables[entitlement_key] = max(
                        0.0, receivable - net_cash
                    )
                    event["receivable_after"] = dividend_receivables[entitlement_key]
            else:
                event = ledger.apply_corporate_action(applied)
            action_rows.append(
                {
                    "scenario": scenario.name,
                    "action_id": action["action_id"],
                    "processing_stage": action["processing_stage"],
                    **event,
                }
            )

        if trade_date in activation:
            active_target = activation[trade_date].copy()
            pending_instruments = None
        if not active_target.empty:
            target_for_day = _target_for_day(active_target, day_market, trade_date)
            target_for_day.insert(0, "scenario", scenario.name)
            target_rows.extend(target_for_day.to_dict("records"))
            sizing_prices = _sizing_price_frame(
                day_market, ledger, last_prices, trade_date
            )
            sizing_nav = _preopen_nav(
                ledger,
                sizing_prices,
                noncash_assets=float(sum(dividend_receivables.values())),
            )
            orders, suppressed = generate_target_weight_orders(
                target_for_day.drop(columns="scenario"),
                ledger.snapshot(),
                sizing_prices,
                portfolio_nav=sizing_nav,
                cash_buffer_fraction=bt_spec.cash_buffer_fraction,
                liquidate_missing=True,
                capacity_panel=day_market,
                minimum_routine_trade_notional_cny=(
                    scenario_spec.minimum_routine_trade_notional_cny
                ),
                return_suppressed=True,
            )
            if pending_instruments is not None:
                if not orders.empty:
                    orders = orders.loc[
                        orders["instrument_id"].astype(str).isin(
                            pending_instruments
                        )
                    ].reset_index(drop=True)
                if not suppressed.empty:
                    suppressed = suppressed.loc[
                        suppressed["instrument_id"].astype(str).isin(
                            pending_instruments
                        )
                    ].reset_index(drop=True)
            if not suppressed.empty:
                suppressed["scenario"] = scenario.name
                suppressed["target_decision_at"] = target_for_day[
                    "decision_at"
                ].max()
                suppressed_rows.extend(suppressed.to_dict("records"))
            attempt_results = []
            if not orders.empty:
                orders["scenario"] = scenario.name
                orders["target_decision_at"] = target_for_day["decision_at"].max()
                orders["unfilled_order_policy"] = bt_spec.unfilled_order_policy
                order_rows.extend(orders.to_dict("records"))
                for order in orders.to_dict("records"):
                    key = (str(order["instrument_id"]), trade_date)
                    if key not in market_index.index:
                        raise ExecutionError(f"no market row for generated order {key}")
                    execution = engine.execute(order, market_index.loc[key].to_dict())
                    execution["scenario"] = scenario.name
                    execution_rows.append(execution)
                    attempt_results.append(execution)
            capacity_rows.append(
                _portfolio_capacity_row(
                    scenario.name,
                    trade_date,
                    sizing_nav,
                    target_for_day,
                    orders,
                    day_market,
                    scenario_spec,
                )
            )
            if orders.empty or all(
                execution["status"] == "filled" for execution in attempt_results
            ):
                active_target = pd.DataFrame()
                pending_instruments = None
            else:
                pending_instruments = {
                    str(execution["instrument_id"])
                    for execution in attempt_results
                    if execution["status"] != "filled"
                }

        _refresh_close_prices(
            day_market,
            ledger,
            last_prices,
            stale_sessions,
        )
        details, account = ledger.mark_to_market(last_prices, trade_date)
        account["dividend_receivable"] = float(sum(dividend_receivables.values()))
        account["nav"] += account["dividend_receivable"]
        details.insert(0, "scenario", scenario.name)
        details["valuation_method"] = details["instrument_id"].map(
            lambda item: "observed_close"
            if stale_sessions.get(item, 0) == 0
            else "carry_forward_prior_close"
        )
        details["stale_sessions"] = details["instrument_id"].map(stale_sessions).fillna(0)
        position_rows.extend(details.to_dict("records"))

        day_exec = [row for row in execution_rows if row["trade_date"] == trade_date]
        day_fees = float(sum(row["total_fees"] for row in day_exec))
        day_slippage = float(sum(row["slippage_cost"] for row in day_exec))
        day_turnover = float(sum(row["notional"] for row in day_exec))
        cumulative_fees += day_fees
        cumulative_slippage += day_slippage
        cumulative_turnover += day_turnover
        nav = float(account["nav"])
        daily_return = nav / previous_nav - 1.0 if previous_nav > 0 else np.nan
        nav_rows.append(
            {
                "scenario": scenario.name,
                "trade_date": trade_date,
                **account,
                "daily_return": daily_return,
                "day_fees": day_fees,
                "day_slippage_cost": day_slippage,
                "day_turnover_notional": day_turnover,
                "cumulative_fees": cumulative_fees,
                "cumulative_slippage_cost": cumulative_slippage,
                "cumulative_turnover_notional": cumulative_turnover,
                "positions": int((details["total_quantity"] > 0).sum()),
                "cash_fraction": account["cash"] / nav if nav > 0 else np.nan,
            }
        )
        previous_nav = nav

        for action in day_actions.loc[
            day_actions["processing_stage"] == "record_close"
        ].to_dict("records"):
            quantity = ledger.position(str(action["instrument_id"])).total_quantity
            dividend_entitlements[str(action["action_id"])] = quantity
            action_rows.append(
                {
                    "scenario": scenario.name,
                    "action_id": action["action_id"],
                    "instrument_id": action["instrument_id"],
                    "action_type": action["action_type"],
                    "effective_date": trade_date,
                    "processing_stage": "record_close",
                    "entitled_quantity": quantity,
                    "cash_before": ledger.cash,
                    "cash_after": ledger.cash,
                }
            )

    nav = pd.DataFrame(nav_rows)
    executions = pd.DataFrame(execution_rows)
    summary = _scenario_summary(
        scenario,
        nav,
        executions,
        pd.DataFrame(suppressed_rows),
        pd.DataFrame(capacity_rows),
        initial_nav,
        fees,
    )
    return {
        "daily_nav": nav,
        "daily_positions": pd.DataFrame(position_rows),
        "target_weights": pd.DataFrame(target_rows),
        "orders": pd.DataFrame(order_rows),
        "suppressed_orders": pd.DataFrame(suppressed_rows),
        "executions": executions,
        "corporate_action_ledger": pd.DataFrame(action_rows),
        "capacity_history": pd.DataFrame(capacity_rows),
        "scenario_summary": summary,
    }


def _prepare_market(
    tradability: pd.DataFrame,
    capacity_panel: pd.DataFrame,
    relevant_instrument_ids: set[str],
) -> pd.DataFrame:
    required_market = {
        "trade_date",
        "instrument_id",
        "symbol",
        "open",
        "close",
        "data_complete",
        "has_bar",
        "can_mark_to_market",
        "execution_event_at",
    }
    missing = sorted(required_market - set(tradability.columns))
    if missing:
        raise ExecutionError(f"tradability missing backtest columns: {missing}")
    market = tradability.copy()
    market = market.loc[
        market["instrument_id"].astype(str).isin(relevant_instrument_ids)
    ].copy()
    if market.empty:
        raise ExecutionError("no P0.5 rows cover the requested backtest instruments")
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    if market.duplicated(["instrument_id", "trade_date"]).any():
        raise ExecutionError("tradability has duplicate instrument-date rows")
    panel = capacity_panel.copy()
    panel["trade_date"] = pd.to_datetime(panel["trade_date"]).dt.normalize()
    join_keys = (
        ["instrument_id", "trade_date"]
        if "instrument_id" in panel.columns
        else ["symbol", "trade_date"]
    )
    required_capacity = {*join_keys, *CAPACITY_FIELDS, "capacity_available_at"}
    missing_capacity = sorted(required_capacity - set(panel.columns))
    if missing_capacity:
        raise ExecutionError(f"capacity panel missing columns: {missing_capacity}")
    if panel.duplicated(join_keys).any():
        raise ExecutionError("capacity panel has duplicate join keys")
    result = market.merge(
        panel[[*join_keys, *CAPACITY_FIELDS, "capacity_available_at"]],
        on=join_keys,
        how="left",
        validate="one_to_one",
    )
    result["adv_shares_lag1"] = result["adv20_shares_lag1"]
    available = pd.to_datetime(result["capacity_available_at"], utc=True, errors="coerce")
    execution = pd.to_datetime(result["execution_event_at"], utc=True, errors="coerce")
    invalid = available.notna() & execution.notna() & (available > execution)
    if invalid.any():
        raise ExecutionError("capacity inputs become available after execution")
    return result.sort_values(["trade_date", "instrument_id"]).reset_index(drop=True)


def _relevant_instrument_ids(
    targets: pd.DataFrame,
    corporate_actions: Optional[pd.DataFrame],
    initial_positions: Optional[pd.DataFrame],
) -> set[str]:
    if "instrument_id" not in targets:
        raise ExecutionError("targets missing columns: ['instrument_id']")
    relevant = set(targets["instrument_id"].dropna().astype(str))
    for frame in (corporate_actions, initial_positions):
        if frame is not None and not frame.empty:
            if "instrument_id" not in frame:
                raise ExecutionError("optional backtest input is missing instrument_id")
            relevant.update(frame["instrument_id"].dropna().astype(str))
    if not relevant:
        raise ExecutionError("backtest has no requested instruments")
    return relevant


def _prepare_targets(
    targets: pd.DataFrame,
    market: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    spec: BacktestSpec,
) -> pd.DataFrame:
    required = {"trade_date", "instrument_id", "target_weight", "decision_at"}
    missing = sorted(required - set(targets.columns))
    if missing:
        raise ExecutionError(f"targets missing columns: {missing}")
    work = targets.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"]).dt.normalize()
    work["decision_at"] = pd.to_datetime(work["decision_at"], utc=True, errors="coerce")
    work["target_weight"] = pd.to_numeric(work["target_weight"], errors="coerce")
    if work[["decision_at", "target_weight"]].isna().any().any():
        raise ExecutionError("targets contain invalid decision time or weight")
    if (work["target_weight"] < 0).any():
        raise ExecutionError("P0.6.3 supports long-only targets")
    if work.duplicated(["trade_date", "instrument_id"]).any():
        raise ExecutionError("targets contain duplicate security-date rows")
    if not set(work["trade_date"]).issubset(set(calendar)):
        raise ExecutionError("target trade dates must be tradability sessions")
    if not set(work["instrument_id"]).issubset(set(market["instrument_id"])):
        raise ExecutionError("target instrument is outside the backtest market")
    sums = work.groupby("trade_date")["target_weight"].sum()
    if (sums > 1.0 - spec.cash_buffer_fraction + spec.target_weight_tolerance).any():
        raise ExecutionError("targets breach the cash-buffer policy")
    execution_times = market[["trade_date", "execution_event_at"]].drop_duplicates()
    if execution_times.duplicated("trade_date").any():
        raise ExecutionError("execution event time differs inside a trading session")
    work = work.merge(execution_times, on="trade_date", how="left", validate="many_to_one")
    event_at = pd.to_datetime(work["execution_event_at"], utc=True, errors="coerce")
    if (work["decision_at"] >= event_at).any():
        raise ExecutionError("target decision_at must be before the modeled execution event")
    return work.drop(columns="execution_event_at").sort_values(
        ["trade_date", "instrument_id"]
    )


def _prepare_initial_positions(frame: Optional[pd.DataFrame]) -> pd.DataFrame:
    columns = ["instrument_id", "total_quantity", "sellable_quantity", "average_cost", "last_price"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    missing = sorted({"instrument_id", "total_quantity", "average_cost", "last_price"} - set(frame.columns))
    if missing:
        raise ExecutionError(f"initial positions missing columns: {missing}")
    result = frame.copy()
    if "sellable_quantity" not in result:
        result["sellable_quantity"] = result["total_quantity"]
    for column in ["total_quantity", "sellable_quantity"]:
        result[column] = pd.to_numeric(result[column], errors="raise").astype(int)
    for column in ["average_cost", "last_price"]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    invalid_prices = (
        result[["average_cost", "last_price"]].isna().any(axis=1)
        | (result["average_cost"] < 0)
        | (result["last_price"] <= 0)
    )
    invalid_quantities = (
        (result[["total_quantity", "sellable_quantity"]] < 0).any(axis=1)
        | (result["sellable_quantity"] > result["total_quantity"])
    )
    if (
        result.duplicated("instrument_id").any()
        or invalid_quantities.any()
        or invalid_prices.any()
    ):
        raise ExecutionError("invalid initial positions")
    return result[columns]


def _prepare_corporate_actions(
    frame: Optional[pd.DataFrame], market: pd.DataFrame, calendar: pd.DatetimeIndex
) -> pd.DataFrame:
    columns = [
        "action_id",
        "instrument_id",
        "symbol",
        "action_type",
        "announcement_at",
        "available_at",
        "ex_date",
        "record_date",
        "pay_date",
        "share_ratio",
        "cash_per_share",
        "withholding_tax_rate",
        "settlement_price",
        "processing_date",
        "processing_stage",
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    required = {
        "action_id",
        "instrument_id",
        "symbol",
        "action_type",
        "announcement_at",
        "available_at",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ExecutionError(f"corporate actions missing columns: {missing}")
    work = frame.copy()
    if work["action_id"].astype(str).duplicated().any():
        raise ExecutionError("corporate action_id must be unique")
    for column in ["announcement_at", "available_at"]:
        work[column] = pd.to_datetime(work[column], utc=True, errors="coerce")
    if work[["announcement_at", "available_at"]].isna().any().any():
        raise ExecutionError("corporate actions contain invalid knowledge timestamps")
    if (work["announcement_at"] > work["available_at"]).any():
        raise ExecutionError("corporate action available_at precedes announcement_at")
    for column in ["ex_date", "record_date", "pay_date"]:
        if column not in work:
            work[column] = pd.NaT
        work[column] = pd.to_datetime(work[column], errors="coerce").dt.normalize()
    for column, default in [
        ("share_ratio", np.nan),
        ("cash_per_share", np.nan),
        ("withholding_tax_rate", 0.0),
        ("settlement_price", np.nan),
    ]:
        if column not in work:
            work[column] = default
        work[column] = pd.to_numeric(work[column], errors="coerce")
    valid_types = {"cash_dividend", "split", "bonus", "delisting_cash_settlement"}
    if not work["action_type"].isin(valid_types).all():
        raise ExecutionError("corporate actions contain unsupported action types")
    market_ids = set(market["instrument_id"])
    if not set(work["instrument_id"]).issubset(market_ids):
        raise ExecutionError("corporate action instrument is outside the backtest market")
    rows = []
    for action in work.to_dict("records"):
        if action["action_type"] == "cash_dividend":
            if (
                pd.isna(action["record_date"])
                or pd.isna(action["ex_date"])
                or pd.isna(action["pay_date"])
            ):
                raise ExecutionError(
                    "cash dividend requires record_date, ex_date, and pay_date"
                )
            if not (
                action["record_date"] <= action["ex_date"] <= action["pay_date"]
            ):
                raise ExecutionError(
                    "cash-dividend dates must satisfy record_date <= ex_date <= pay_date"
                )
            record_session = _covered_session(action["record_date"], calendar)
            ex_session = _covered_session(action["ex_date"], calendar)
            pay_session = _covered_session(action["pay_date"], calendar)
            if record_session is None or ex_session is None:
                raise ExecutionError(
                    "cash-dividend record and ex dates must be covered by the backtest"
                )
            rows.append({**action, "processing_date": record_session, "processing_stage": "record_close"})
            rows.append({**action, "processing_date": ex_session, "processing_stage": "ex_open"})
            if pay_session is not None:
                rows.append({**action, "processing_date": pay_session, "processing_stage": "pay_open"})
        else:
            if pd.isna(action["ex_date"]):
                raise ExecutionError(f"{action['action_type']} requires ex_date")
            if action["action_type"] in {"split", "bonus"} and (
                pd.isna(action["share_ratio"]) or float(action["share_ratio"]) <= 0
            ):
                raise ExecutionError(f"{action['action_type']} requires positive share_ratio")
            if action["action_type"] == "delisting_cash_settlement" and (
                pd.isna(action["settlement_price"])
                or float(action["settlement_price"]) < 0
            ):
                raise ExecutionError("delisting settlement requires settlement_price")
            rows.append({**action, "processing_date": _next_session(action["ex_date"], calendar, include=True), "processing_stage": "pre_open"})
    result = pd.DataFrame(rows)
    processing_deadline = result["processing_date"] + result["processing_stage"].map(
        {
            "pre_open": pd.Timedelta(hours=9, minutes=30),
            "ex_open": pd.Timedelta(hours=9, minutes=30),
            "pay_open": pd.Timedelta(hours=9, minutes=30),
            "record_close": pd.Timedelta(hours=15),
        }
    )
    processing_deadline = processing_deadline.dt.tz_localize(
        "Asia/Shanghai"
    ).dt.tz_convert("UTC")
    if (result["available_at"] > processing_deadline).any():
        raise ExecutionError("corporate action was unavailable before its processing session")
    return result[columns].sort_values(["processing_date", "processing_stage", "action_id"])


def _target_activation_schedule(
    targets: pd.DataFrame, calendar: pd.DatetimeIndex, delay: int
) -> Dict[pd.Timestamp, pd.DataFrame]:
    scheduled: Dict[pd.Timestamp, list[pd.DataFrame]] = {}
    for trade_date, group in targets.groupby("trade_date", sort=True):
        position = calendar.get_indexer([pd.Timestamp(trade_date)])[0]
        activation_position = position + delay
        if activation_position >= len(calendar):
            continue
        activation_date = calendar[activation_position]
        shifted = group.copy()
        shifted["signal_trade_date"] = shifted["trade_date"]
        shifted["trade_date"] = activation_date
        scheduled.setdefault(activation_date, []).append(shifted)
    return {
        date: pd.concat(groups, ignore_index=True)
        for date, groups in scheduled.items()
    }


def _target_for_day(
    active: pd.DataFrame, day_market: pd.DataFrame, trade_date: pd.Timestamp
) -> pd.DataFrame:
    symbols = day_market[["instrument_id", "symbol"]]
    result = active.drop(columns="symbol", errors="ignore").merge(
        symbols, on="instrument_id", how="left", validate="one_to_one"
    )
    if result["symbol"].isna().any():
        raise ExecutionError("active target instrument is absent from the daily market")
    result["trade_date"] = trade_date
    return result


def _sizing_price_frame(
    day_market: pd.DataFrame,
    ledger: PortfolioLedger,
    last_prices: Mapping[str, float],
    trade_date: pd.Timestamp,
) -> pd.DataFrame:
    rows = []
    for row in day_market.to_dict("records"):
        pre_close = row.get("limit_pre_close")
        if pre_close is None or pd.isna(pre_close) or float(pre_close) <= 0:
            pre_close = last_prices.get(str(row["instrument_id"]), np.nan)
        rows.append(
            {
                "trade_date": trade_date,
                "instrument_id": row["instrument_id"],
                "symbol": row["symbol"],
                "reference_price": pre_close,
            }
        )
    prices = pd.DataFrame(rows)
    required = set(ledger.snapshot().loc[lambda frame: frame["total_quantity"] > 0, "instrument_id"])
    invalid = prices.loc[prices["instrument_id"].isin(required), "reference_price"].isna()
    if invalid.any():
        raise ExecutionError("held position lacks a causal pre-open sizing price")
    return prices


def _preopen_nav(
    ledger: PortfolioLedger,
    prices: pd.DataFrame,
    *,
    noncash_assets: float = 0.0,
) -> float:
    price_map = prices.set_index("instrument_id")["reference_price"].to_dict()
    if not np.isfinite(noncash_assets) or noncash_assets < 0:
        raise ExecutionError("noncash assets must be finite and non-negative")
    value = ledger.cash + noncash_assets
    for instrument_id, position in ledger.positions.items():
        if position.total_quantity:
            price = float(price_map.get(instrument_id, np.nan))
            if not np.isfinite(price) or price <= 0:
                raise ExecutionError(f"missing pre-open price for {instrument_id}")
            value += position.total_quantity * price
    return float(value)


def _refresh_preopen_prices(day_market: pd.DataFrame, last_prices: Dict[str, float]) -> None:
    for row in day_market.to_dict("records"):
        value = row.get("limit_pre_close")
        if value is not None and pd.notna(value) and float(value) > 0:
            last_prices[str(row["instrument_id"])] = float(value)


def _refresh_close_prices(
    day_market: pd.DataFrame,
    ledger: PortfolioLedger,
    last_prices: Dict[str, float],
    stale_sessions: Dict[str, int],
) -> None:
    rows = day_market.set_index("instrument_id").to_dict("index")
    for instrument_id, position in ledger.positions.items():
        if position.total_quantity <= 0:
            stale_sessions[instrument_id] = 0
            continue
        market = rows.get(instrument_id)
        observed = (
            market is not None
            and bool(market.get("can_mark_to_market", False))
            and pd.notna(market.get("close"))
            and float(market["close"]) > 0
        )
        if observed:
            last_prices[instrument_id] = float(market["close"])
            stale_sessions[instrument_id] = 0
        else:
            stale_sessions[instrument_id] = stale_sessions.get(instrument_id, 0) + 1
            if instrument_id not in last_prices:
                raise ExecutionError(f"no carry-forward valuation price for {instrument_id}")
            # Continue with the last observable close so the full economic
            # impact stays measurable. Exceeding the frozen stale limit is evaluated
            # from the persisted position ledger as a hard promotion failure;
            # it is not hidden behind an opaque runtime abort.


def _portfolio_capacity_row(
    scenario: str,
    trade_date: pd.Timestamp,
    nav: float,
    targets: pd.DataFrame,
    orders: pd.DataFrame,
    day_market: pd.DataFrame,
    execution_spec: ExecutionSpec,
) -> Dict[str, Any]:
    order_capacity_aum = math.inf
    if not orders.empty:
        for row in orders.to_dict("records"):
            requested_amount = row["quantity"] * row["reference_price_for_sizing"]
            liquidity_inputs = [
                float(row["adv20_amount_lag1"]),
                float(row["adv60_amount_lag1"]),
                float(row["median_amount20_lag1"]),
            ]
            if not all(
                np.isfinite(value) and value > 0 for value in liquidity_inputs
            ):
                order_capacity_aum = 0.0
                continue
            liquidity = min(liquidity_inputs)
            capacity_amount = (
                liquidity
                * execution_spec.liquidity_haircut
                * _effective_impact_participation(
                    float(row["volatility20_daily_lag1"]),
                    execution_spec,
                )
            )
            if requested_amount > 0:
                order_capacity_aum = min(
                    order_capacity_aum, nav * capacity_amount / requested_amount
                )
    position_capacity_aum = math.inf
    indexed = day_market.set_index("instrument_id")
    for row in targets.to_dict("records"):
        weight = float(row["target_weight"])
        if weight <= 0:
            continue
        market = indexed.loc[row["instrument_id"]]
        values = [float(market[field]) for field in CAPACITY_FIELDS]
        if not all(np.isfinite(value) and value > 0 for value in values):
            position_capacity_aum = 0.0
            continue
        liquidity = min(values[1], values[2], values[3])
        position_amount = min(
            values[4] * execution_spec.max_position_free_float_fraction,
            liquidity
            * execution_spec.stress_exit_participation_rate
            * execution_spec.liquidity_haircut
            * execution_spec.max_stress_exit_days,
        )
        position_capacity_aum = min(position_capacity_aum, position_amount / weight)
    strategy_capacity = min(order_capacity_aum, position_capacity_aum)
    if not np.isfinite(strategy_capacity):
        binding_constraint = "none"
    elif order_capacity_aum <= position_capacity_aum:
        binding_constraint = "order"
    else:
        binding_constraint = "position"
    return {
        "scenario": scenario,
        "trade_date": trade_date,
        "nav_for_sizing": nav,
        "order_capacity_aum": _finite_or_nan(order_capacity_aum),
        "position_capacity_aum": _finite_or_nan(position_capacity_aum),
        "strategy_capacity_aum": _finite_or_nan(strategy_capacity),
        "binding_constraint": binding_constraint,
        "generated_orders": len(orders),
    }


def _scenario_summary(
    scenario: ExecutionScenario,
    nav: pd.DataFrame,
    executions: pd.DataFrame,
    suppressed: pd.DataFrame,
    capacity: pd.DataFrame,
    initial_nav: float,
    fees: FeePolicy,
) -> Dict[str, Any]:
    ending_nav = float(nav["nav"].iloc[-1])
    returns = nav["daily_return"].fillna(0.0)
    cumulative = nav["nav"] / initial_nav
    drawdown = cumulative / cumulative.cummax() - 1.0
    finite_capacity = capacity["strategy_capacity_aum"].dropna() if not capacity.empty else pd.Series(dtype=float)
    annualized_return = (
        (ending_nav / initial_nav) ** (252.0 / len(nav)) - 1.0
        if initial_nav > 0 and ending_nav > 0 and len(nav) > 0
        else np.nan
    )
    return_volatility = float(returns.std(ddof=0))
    turnover_notional = (
        float(executions["notional"].sum()) if not executions.empty else 0.0
    )
    filled = (
        executions.loc[
            executions["status"].isin(["filled", "partial"])
            & (executions["notional"] > 0)
        ]
        if not executions.empty
        else pd.DataFrame()
    )
    total_commission = (
        float(filled["commission"].sum()) if not filled.empty else 0.0
    )
    minimum_commission_orders = (
        int(
            (
                filled["commission"]
                <= fees.minimum_commission_cny + 1e-9
            ).sum()
        )
        if not filled.empty
        else 0
    )
    return {
        "scenario": scenario.name,
        "session_delay": scenario.session_delay,
        "start_date": nav["trade_date"].min(),
        "end_date": nav["trade_date"].max(),
        "trading_sessions": len(nav),
        "initial_nav": float(initial_nav),
        "ending_nav": ending_nav,
        "total_return": ending_nav / initial_nav - 1.0,
        "annualized_return": annualized_return,
        "annualized_volatility": return_volatility * math.sqrt(252),
        "sharpe_zero_risk_free": (
            float(returns.mean() / return_volatility * math.sqrt(252))
            if return_volatility > 0
            else np.nan
        ),
        "max_drawdown": float(drawdown.min()),
        "orders": len(executions),
        "filled_or_partial_orders": int(executions["status"].isin(["filled", "partial"]).sum()) if not executions.empty else 0,
        "rejected_orders": int((executions["status"] == "rejected").sum()) if not executions.empty else 0,
        "turnover_notional": turnover_notional,
        "turnover_multiple_of_average_nav": (
            turnover_notional / float(nav["nav"].mean())
            if float(nav["nav"].mean()) > 0
            else np.nan
        ),
        "total_fees": float(executions["total_fees"].sum()) if not executions.empty else 0.0,
        "total_commission": total_commission,
        "minimum_commission_orders": minimum_commission_orders,
        "minimum_commission_hit_rate": (
            minimum_commission_orders / len(filled)
            if len(filled) > 0
            else np.nan
        ),
        "effective_commission_bps": (
            total_commission / turnover_notional * 10_000.0
            if turnover_notional > 0
            else np.nan
        ),
        "all_in_fee_bps": (
            float(executions["total_fees"].sum())
            / turnover_notional
            * 10_000.0
            if not executions.empty and turnover_notional > 0
            else np.nan
        ),
        "median_filled_order_notional": (
            float(filled["notional"].median()) if not filled.empty else np.nan
        ),
        "p10_filled_order_notional": (
            float(filled["notional"].quantile(0.10))
            if not filled.empty
            else np.nan
        ),
        "suppressed_orders": len(suppressed),
        "suppressed_notional": (
            float(suppressed["estimated_notional"].sum())
            if not suppressed.empty
            else 0.0
        ),
        "suppressed_abs_delta_weight_median": (
            float(suppressed["delta_weight_for_sizing"].abs().median())
            if not suppressed.empty
            else np.nan
        ),
        "suppressed_abs_delta_weight_max": (
            float(suppressed["delta_weight_for_sizing"].abs().max())
            if not suppressed.empty
            else np.nan
        ),
        "total_slippage_cost": float(executions["slippage_cost"].sum()) if not executions.empty else 0.0,
        "total_cash_dividends": float(nav["cumulative_dividends"].iloc[-1]),
        "capacity_p10": float(finite_capacity.quantile(0.10)) if not finite_capacity.empty else np.nan,
        "capacity_p25": float(finite_capacity.quantile(0.25)) if not finite_capacity.empty else np.nan,
        "capacity_median": float(finite_capacity.median()) if not finite_capacity.empty else np.nan,
        "capacity_min": float(finite_capacity.min()) if not finite_capacity.empty else np.nan,
    }


def _scenario_execution_spec(
    base: ExecutionSpec, scenario: ExecutionScenario
) -> ExecutionSpec:
    return replace(
        base,
        max_participation_rate=scenario.max_participation_rate
        if scenario.max_participation_rate is not None
        else base.max_participation_rate,
        liquidity_haircut=scenario.liquidity_haircut
        if scenario.liquidity_haircut is not None
        else base.liquidity_haircut,
        base_slippage_bps=scenario.base_slippage_bps
        if scenario.base_slippage_bps is not None
        else base.base_slippage_bps,
        impact_y=scenario.impact_y
        if scenario.impact_y is not None
        else base.impact_y,
        max_executable_impact_bps=scenario.max_executable_impact_bps
        if scenario.max_executable_impact_bps is not None
        else base.max_executable_impact_bps,
        minimum_routine_trade_notional_cny=(
            scenario.minimum_routine_trade_notional_cny
            if scenario.minimum_routine_trade_notional_cny is not None
            else base.minimum_routine_trade_notional_cny
        ),
        version=f"{base.version}:{scenario.name}",
    ).validate()


def _effective_impact_participation(
    volatility_daily: float, spec: ExecutionSpec
) -> float:
    if not np.isfinite(volatility_daily) or volatility_daily <= 0:
        return 0.0
    impact_scale = spec.impact_y * volatility_daily * 10_000.0
    impact_limit = (
        spec.max_executable_impact_bps / impact_scale
    ) ** (1.0 / spec.impact_exponent)
    return min(spec.max_participation_rate, impact_limit)


def _next_session(
    date: Any, calendar: pd.DatetimeIndex, *, include: bool
) -> pd.Timestamp:
    normalized = pd.Timestamp(date).normalize()
    side = "left" if include else "right"
    position = calendar.searchsorted(normalized, side=side)
    if position >= len(calendar):
        raise ExecutionError(f"calendar does not cover event date {normalized.date()}")
    return calendar[position]


def _covered_session(date: Any, calendar: pd.DatetimeIndex) -> Optional[pd.Timestamp]:
    normalized = pd.Timestamp(date).normalize()
    if normalized < calendar.min() or normalized > calendar.max():
        return None
    return _next_session(normalized, calendar, include=True)


def _seed_market_value(initial_positions: pd.DataFrame) -> float:
    if initial_positions.empty:
        return 0.0
    return float((initial_positions["total_quantity"] * initial_positions["last_price"]).sum())


def _concat(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True) if nonempty else pd.DataFrame()


def _finite_or_nan(value: float) -> float:
    return float(value) if np.isfinite(value) else np.nan


def _fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
