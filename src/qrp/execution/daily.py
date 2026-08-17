"""Fail-closed daily execution model for standard cash A-share research."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from .capacity import CAPACITY_FIELDS, assess_order_capacity


class ExecutionError(RuntimeError):
    """Raised when execution inputs are structurally invalid or non-causal."""


@dataclass(frozen=True)
class FeePolicy:
    """Versioned A-share cash-equity costs.

    Brokerage commission is deliberately configurable because it is a client
    contract, not a universal exchange rule.  It is treated as all-in brokerage
    commission excluding stamp duty and registration transfer fee.
    """

    commission_bps: float = 3.0
    minimum_commission_cny: float = 5.0
    stamp_duty_sell_bps_before_20230828: float = 10.0
    stamp_duty_sell_bps_from_20230828: float = 5.0
    transfer_fee_bps_before_20220429_sh_sz: float = 0.2
    transfer_fee_bps_before_20220429_bj: float = 0.25
    transfer_fee_bps_from_20220429: float = 0.1
    version: str = "a_share_cash_fee_v1_20230828"

    @property
    def fingerprint(self) -> str:
        return _fingerprint(asdict(self))

    def rates(self, trade_date: Any, exchange: str) -> Dict[str, float]:
        date = pd.Timestamp(trade_date).normalize()
        stamp = (
            self.stamp_duty_sell_bps_from_20230828
            if date >= pd.Timestamp("2023-08-28")
            else self.stamp_duty_sell_bps_before_20230828
        )
        if date >= pd.Timestamp("2022-04-29"):
            transfer = self.transfer_fee_bps_from_20220429
        elif exchange == "BJ":
            transfer = self.transfer_fee_bps_before_20220429_bj
        else:
            transfer = self.transfer_fee_bps_before_20220429_sh_sz
        return {
            "commission_bps": self.commission_bps,
            "stamp_duty_sell_bps": stamp,
            "transfer_fee_bps": transfer,
        }

    def calculate(
        self, notional: float, side: str, trade_date: Any, exchange: str
    ) -> Dict[str, float]:
        if notional <= 0:
            return {
                "commission": 0.0,
                "stamp_duty": 0.0,
                "transfer_fee": 0.0,
                "total_fees": 0.0,
            }
        rates = self.rates(trade_date, exchange)
        commission = max(
            self.minimum_commission_cny,
            notional * rates["commission_bps"] / 10_000.0,
        )
        stamp = (
            notional * rates["stamp_duty_sell_bps"] / 10_000.0
            if side == "sell"
            else 0.0
        )
        transfer = notional * rates["transfer_fee_bps"] / 10_000.0
        return {
            "commission": commission,
            "stamp_duty": stamp,
            "transfer_fee": transfer,
            "total_fees": commission + stamp + transfer,
        }


@dataclass(frozen=True)
class ExecutionSpec:
    max_participation_rate: float = 0.01
    liquidity_haircut: float = 1.0
    max_position_free_float_fraction: float = 0.001
    normal_exit_participation_rate: float = 0.05
    stress_exit_participation_rate: float = 0.02
    max_stress_exit_days: float = 3.0
    base_slippage_bps: float = 5.0
    impact_y: float = 0.50
    impact_exponent: float = 0.5
    max_executable_impact_bps: float = 100.0
    minimum_routine_trade_notional_cny: float = 0.0
    price_tick_cny: float = 0.01
    enforce_t_plus_one: bool = True
    allow_partial_fills: bool = True
    require_standard_research_eligible: bool = True
    require_lagged_liquidity: bool = True
    require_institutional_capacity_inputs: bool = True
    version: str = "a_share_daily_open_execution_v3_volatility_impact"

    def validate(self) -> "ExecutionSpec":
        if not 0 < self.max_participation_rate <= 1:
            raise ValueError("max_participation_rate must be in (0, 1]")
        if not 0 < self.liquidity_haircut <= 1:
            raise ValueError("liquidity_haircut must be in (0, 1]")
        if not 0 < self.max_position_free_float_fraction <= 1:
            raise ValueError("max_position_free_float_fraction must be in (0, 1]")
        if not 0 < self.stress_exit_participation_rate <= self.normal_exit_participation_rate <= 1:
            raise ValueError("exit participation rates are inconsistent")
        if self.max_stress_exit_days <= 0:
            raise ValueError("max_stress_exit_days must be positive")
        if min(
            self.base_slippage_bps,
            self.minimum_routine_trade_notional_cny,
        ) < 0:
            raise ValueError("cost parameters must be non-negative")
        if not 0 < self.impact_y <= 2:
            raise ValueError("impact_y must be in (0, 2]")
        if not 0 < self.impact_exponent <= 1:
            raise ValueError("impact_exponent must be in (0, 1]")
        if self.max_executable_impact_bps <= 0:
            raise ValueError("max_executable_impact_bps must be positive")
        if self.price_tick_cny <= 0:
            raise ValueError("price_tick_cny must be positive")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(asdict(self))


@dataclass
class Position:
    total_quantity: int = 0
    sellable_quantity: int = 0
    cost_basis_total: float = 0.0
    realized_pnl: float = 0.0
    cumulative_dividends: float = 0.0

    @property
    def average_cost(self) -> float:
        return self.cost_basis_total / self.total_quantity if self.total_quantity else 0.0


class PortfolioLedger:
    """Cash and share ledger with trading-day T+1 settlement state."""

    def __init__(self, initial_cash: float) -> None:
        if not np.isfinite(initial_cash) or initial_cash < 0:
            raise ValueError("initial_cash must be finite and non-negative")
        self.cash = float(initial_cash)
        self.positions: Dict[str, Position] = {}
        self.current_trade_date: Optional[pd.Timestamp] = None

    def advance(self, trade_date: Any) -> None:
        date = pd.Timestamp(trade_date).normalize()
        if self.current_trade_date is not None and date < self.current_trade_date:
            raise ExecutionError("Orders must be processed in non-decreasing trade-date order")
        if self.current_trade_date is None or date > self.current_trade_date:
            for position in self.positions.values():
                position.sellable_quantity = position.total_quantity
            self.current_trade_date = date

    def position(self, instrument_id: str) -> Position:
        return self.positions.setdefault(instrument_id, Position())

    def seed_position(
        self,
        instrument_id: str,
        total_quantity: int,
        sellable_quantity: Optional[int] = None,
        average_cost: float = 0.0,
    ) -> None:
        if self.current_trade_date is not None:
            raise ExecutionError("Initial positions must be seeded before execution starts")
        total = int(total_quantity)
        sellable = total if sellable_quantity is None else int(sellable_quantity)
        if total < 0 or sellable < 0 or sellable > total:
            raise ValueError("Seeded position quantities are inconsistent")
        if not np.isfinite(average_cost) or average_cost < 0:
            raise ValueError("average_cost must be finite and non-negative")
        self.positions[str(instrument_id)] = Position(
            total, sellable, total * float(average_cost)
        )

    def snapshot(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "instrument_id": instrument_id,
                    "total_quantity": position.total_quantity,
                    "sellable_quantity": position.sellable_quantity,
                    "average_cost": position.average_cost,
                    "cost_basis_total": position.cost_basis_total,
                    "realized_pnl": position.realized_pnl,
                    "cumulative_dividends": position.cumulative_dividends,
                }
                for instrument_id, position in sorted(self.positions.items())
            ],
            columns=[
                "instrument_id",
                "total_quantity",
                "sellable_quantity",
                "average_cost",
                "cost_basis_total",
                "realized_pnl",
                "cumulative_dividends",
            ],
        )

    def apply_buy(
        self, instrument_id: str, quantity: int, notional: float, fees: float
    ) -> None:
        cash_debit = notional + fees
        if cash_debit > self.cash + 1e-9:
            raise ExecutionError("Buy debit exceeds available cash")
        position = self.position(instrument_id)
        position.total_quantity += quantity
        position.cost_basis_total += cash_debit
        self.cash -= cash_debit

    def apply_sell(
        self, instrument_id: str, quantity: int, notional: float, fees: float
    ) -> None:
        position = self.position(instrument_id)
        if quantity > position.sellable_quantity or quantity > position.total_quantity:
            raise ExecutionError("Sell quantity exceeds sellable position")
        average_cost = position.average_cost
        cash_credit = notional - fees
        position.realized_pnl += cash_credit - average_cost * quantity
        position.cost_basis_total = max(
            0.0, position.cost_basis_total - average_cost * quantity
        )
        position.total_quantity -= quantity
        position.sellable_quantity -= quantity
        self.cash += cash_credit

    def apply_corporate_action(self, action: Mapping[str, Any]) -> Dict[str, Any]:
        """Apply a frozen, effective-date corporate action to cash and positions."""
        instrument_id = str(action["instrument_id"])
        action_type = str(action["action_type"]).lower()
        effective_date = pd.Timestamp(action["effective_date"]).normalize()
        self.advance(effective_date)
        position = self.position(instrument_id)
        before_quantity = position.total_quantity
        cash_before = self.cash
        fractional_total_discarded = 0.0
        fractional_sellable_discarded = 0.0
        if action_type in {"split", "bonus"}:
            ratio = float(action["share_ratio"])
            if not np.isfinite(ratio) or ratio <= 0:
                raise ExecutionError("corporate-action share_ratio must be positive")
            exact_total = position.total_quantity * ratio
            exact_sellable = position.sellable_quantity * ratio
            policy = str(action.get("fractional_share_policy", "fail_closed"))
            fractional = (
                not math.isclose(exact_total, round(exact_total), abs_tol=1e-9)
                or not math.isclose(exact_sellable, round(exact_sellable), abs_tol=1e-9)
            )
            if fractional and policy != "floor_zero_value_v1":
                raise ExecutionError(
                    "fractional corporate-action entitlement lacks a supported policy"
                )
            if fractional:
                new_total = int(math.floor(exact_total + 1e-12))
                new_sellable = int(math.floor(exact_sellable + 1e-12))
                fractional_total_discarded = exact_total - new_total
                fractional_sellable_discarded = exact_sellable - new_sellable
            else:
                new_total = int(round(exact_total))
                new_sellable = int(round(exact_sellable))
            position.total_quantity = new_total
            position.sellable_quantity = min(new_total, new_sellable)
        elif action_type == "cash_dividend":
            cash_per_share = float(action["cash_per_share"])
            if not np.isfinite(cash_per_share) or cash_per_share < 0:
                raise ExecutionError("cash_per_share must be finite and non-negative")
            withholding_tax_rate = float(action.get("withholding_tax_rate", 0.0))
            if not 0 <= withholding_tax_rate <= 1:
                raise ExecutionError("withholding_tax_rate must be in [0, 1]")
            entitled_quantity = int(action.get("entitled_quantity", position.total_quantity))
            if entitled_quantity < 0:
                raise ExecutionError("entitled_quantity must be non-negative")
            cash = entitled_quantity * cash_per_share * (1.0 - withholding_tax_rate)
            self.cash += cash
            position.cumulative_dividends += cash
        elif action_type == "delisting_cash_settlement":
            settlement_price = float(action["settlement_price"])
            if not np.isfinite(settlement_price) or settlement_price < 0:
                raise ExecutionError("settlement_price must be finite and non-negative")
            proceeds = position.total_quantity * settlement_price
            position.realized_pnl += proceeds - position.cost_basis_total
            self.cash += proceeds
            position.total_quantity = 0
            position.sellable_quantity = 0
            position.cost_basis_total = 0.0
        else:
            raise ExecutionError(
                f"unsupported corporate action {action_type}; rights issues require explicit orders"
            )
        return {
            "instrument_id": instrument_id,
            "action_type": action_type,
            "effective_date": effective_date,
            "quantity_before": before_quantity,
            "quantity_after": position.total_quantity,
            "cash_before": cash_before,
            "cash_after": self.cash,
            "fractional_total_discarded": fractional_total_discarded,
            "fractional_sellable_discarded": fractional_sellable_discarded,
        }

    def mark_to_market(
        self, prices: Mapping[str, float], valuation_date: Any
    ) -> Tuple[pd.DataFrame, Dict[str, float]]:
        """Produce position-level and account-level NAV without adjusted prices."""
        rows = []
        for instrument_id, position in sorted(self.positions.items()):
            price = float(prices.get(instrument_id, np.nan))
            if position.total_quantity and (not np.isfinite(price) or price <= 0):
                raise ExecutionError(f"missing raw valuation price for {instrument_id}")
            market_value = position.total_quantity * (price if np.isfinite(price) else 0.0)
            rows.append(
                {
                    "valuation_date": pd.Timestamp(valuation_date).normalize(),
                    "instrument_id": instrument_id,
                    "total_quantity": position.total_quantity,
                    "sellable_quantity": position.sellable_quantity,
                    "raw_valuation_price": price,
                    "market_value": market_value,
                    "cost_basis_total": position.cost_basis_total,
                    "unrealized_pnl": market_value - position.cost_basis_total,
                    "realized_pnl": position.realized_pnl,
                    "cumulative_dividends": position.cumulative_dividends,
                }
            )
        detail = pd.DataFrame(
            rows,
            columns=[
                "valuation_date",
                "instrument_id",
                "total_quantity",
                "sellable_quantity",
                "raw_valuation_price",
                "market_value",
                "cost_basis_total",
                "unrealized_pnl",
                "realized_pnl",
                "cumulative_dividends",
            ],
        )
        market_value = float(detail["market_value"].sum()) if not detail.empty else 0.0
        return detail, {
            "cash": self.cash,
            "market_value": market_value,
            "nav": self.cash + market_value,
            "realized_pnl": float(
                sum(position.realized_pnl for position in self.positions.values())
            ),
            "unrealized_pnl": float(
                detail["unrealized_pnl"].sum() if not detail.empty else 0.0
            ),
            "cumulative_dividends": float(
                sum(position.cumulative_dividends for position in self.positions.values())
            ),
        }


class DailyExecutionEngine:
    def __init__(
        self,
        ledger: PortfolioLedger,
        spec: Optional[ExecutionSpec] = None,
        fees: Optional[FeePolicy] = None,
    ) -> None:
        self.ledger = ledger
        self.spec = (spec or ExecutionSpec()).validate()
        self.fees = fees or FeePolicy()

    def execute(
        self, order: Mapping[str, Any], market: Mapping[str, Any]
    ) -> Dict[str, Any]:
        normalized = _normalize_order(order)
        trade_date = normalized["trade_date"]
        self.ledger.advance(trade_date)
        _validate_market_alignment(normalized, market)
        identity = normalized["instrument_id"]
        side = normalized["side"]
        requested = normalized["quantity"]
        exchange = _exchange(normalized["symbol"])
        before = self.ledger.position(identity)
        base = self._base_result(normalized, before)
        base.update(
            {
                "market_open": market.get("open"),
                "market_up_limit": market.get("up_limit"),
                "market_down_limit": market.get("down_limit"),
                "market_has_bounded_price_limit": bool(
                    market.get("has_bounded_price_limit", False)
                ),
                "market_is_suspended": bool(market.get("is_suspended", False)),
                "market_standard_research_eligible": bool(
                    market.get("standard_research_eligible", False)
                ),
                "p05_tradability_version": market.get("tradability_version", ""),
                "p05_tradability_spec_sha256": market.get(
                    "tradability_spec_sha256", ""
                ),
            }
        )

        market_block = _market_block_reason(market, side, self.spec)
        if market_block:
            return self._reject(base, market_block)

        submitted, quantity_note = _submitted_quantity(
            requested=requested,
            side=side,
            exchange=exchange,
            sellable=before.sellable_quantity,
            total=before.total_quantity,
            enforce_t_plus_one=self.spec.enforce_t_plus_one,
        )
        base["submitted_quantity"] = submitted
        if submitted <= 0:
            reason = "t_plus_one_no_sellable_shares" if side == "sell" else "below_minimum_lot"
            return self._reject(base, reason)

        reference_price = float(market["open"])
        if not np.isfinite(reference_price) or reference_price <= 0:
            return self._reject(base, "invalid_open_price")
        minimum_lot, lot_increment = _lot_rule(exchange)
        capacity_result = assess_order_capacity(
            normalized,
            reference_price=reference_price,
            side=side,
            current_position=before.total_quantity,
            submitted_quantity=submitted,
            lot_increment=lot_increment,
            minimum_lot=minimum_lot,
            spec=self.spec,
        )
        base.update(capacity_result)
        if capacity_result.get("block_reason"):
            return self._reject(base, str(capacity_result["block_reason"]))
        capacity = int(capacity_result["capacity_quantity"])
        if capacity <= 0:
            return self._reject(base, "below_liquidity_capacity")
        if capacity < submitted and not self.spec.allow_partial_fills:
            return self._reject(base, "liquidity_partial_fill_disabled")
        fill_quantity = min(submitted, capacity)

        adv = normalized["adv20_shares_lag1"]
        if not np.isfinite(adv):
            adv = normalized["adv_shares_lag1"]
        participation = 0.0 if not np.isfinite(adv) else fill_quantity / adv
        liquidity_amount = float(capacity_result["liquidity_reference_amount_lag1"])
        amount_participation = (
            fill_quantity * reference_price / liquidity_amount
            if np.isfinite(liquidity_amount) and liquidity_amount > 0
            else np.nan
        )
        volatility = normalized["volatility20_daily_lag1"]
        execution_price, slippage_bps, impact_bps = _execution_price(
            reference_price,
            side,
            amount_participation,
            volatility,
            market,
            self.spec,
        )
        base.update(
            {
                "reference_price": reference_price,
                "execution_price": execution_price,
                "participation_rate": participation,
                "amount_participation_rate_for_impact": amount_participation,
                "volatility20_daily_lag1": volatility,
                "impact_y": self.spec.impact_y,
                "impact_bps": impact_bps,
                "slippage_bps": slippage_bps,
            }
        )
        limit_price = normalized["limit_price"]
        if limit_price is not None and (
            (side == "buy" and execution_price > limit_price)
            or (side == "sell" and execution_price < limit_price)
        ):
            return self._reject(base, "order_limit_not_marketable")

        cash_limited = False
        if side == "buy":
            fill_quantity, execution_price, slippage_bps, impact_bps = self._fit_cash(
                fill_quantity,
                liquidity_amount,
                volatility,
                reference_price,
                exchange,
                trade_date,
                market,
            )
            if fill_quantity <= 0:
                return self._reject(base, "insufficient_cash")
            cash_limited = fill_quantity < min(submitted, capacity)
            participation = 0.0 if not np.isfinite(adv) else fill_quantity / adv
            amount_participation = (
                fill_quantity * reference_price / liquidity_amount
                if np.isfinite(liquidity_amount) and liquidity_amount > 0
                else np.nan
            )

        free_float = normalized["free_float_market_cap_lag1"]
        projected_position = (
            before.total_quantity + fill_quantity
            if side == "buy"
            else max(0, before.total_quantity - fill_quantity)
        )
        projected_value = projected_position * reference_price
        base.update(
            {
                "order_amount_participation_rate": (
                    fill_quantity * reference_price / liquidity_amount
                    if np.isfinite(liquidity_amount) and liquidity_amount > 0
                    else np.nan
                ),
                "amount_participation_rate_for_impact": amount_participation,
                "impact_bps": impact_bps,
                "projected_free_float_fraction": (
                    projected_value / free_float
                    if np.isfinite(free_float) and free_float > 0
                    else np.nan
                ),
                "normal_exit_days": (
                    projected_value
                    / (liquidity_amount * self.spec.normal_exit_participation_rate)
                    if np.isfinite(liquidity_amount) and liquidity_amount > 0
                    else np.nan
                ),
                "stress_exit_days": (
                    projected_value
                    / (liquidity_amount * self.spec.stress_exit_participation_rate)
                    if np.isfinite(liquidity_amount) and liquidity_amount > 0
                    else np.nan
                ),
            }
        )

        notional = execution_price * fill_quantity
        fee_parts = self.fees.calculate(notional, side, trade_date, exchange)
        if side == "buy":
            self.ledger.apply_buy(
                identity, fill_quantity, notional, fee_parts["total_fees"]
            )
        else:
            self.ledger.apply_sell(
                identity, fill_quantity, notional, fee_parts["total_fees"]
            )
        after = self.ledger.position(identity)
        status = "filled" if fill_quantity == requested else "partial"
        notes = [note for note in [quantity_note] if note]
        if capacity < submitted:
            notes.append("liquidity_capacity")
        if cash_limited:
            notes.append("cash_capacity")
        base.update(
            {
                "status": status,
                "block_reason": "",
                "execution_notes": " | ".join(notes),
                "filled_quantity": fill_quantity,
                "unfilled_quantity": requested - fill_quantity,
                "reference_price": reference_price,
                "execution_price": execution_price,
                "notional": notional,
                "participation_rate": participation,
                "slippage_bps": slippage_bps,
                "slippage_cost": abs(execution_price - reference_price) * fill_quantity,
                **fee_parts,
                "cash_after": self.ledger.cash,
                "position_after": after.total_quantity,
                "sellable_after": after.sellable_quantity,
            }
        )
        return base

    def _fit_cash(
        self,
        quantity: int,
        liquidity_amount: float,
        volatility: float,
        reference_price: float,
        exchange: str,
        trade_date: pd.Timestamp,
        market: Mapping[str, Any],
    ) -> Tuple[int, float, float, float]:
        candidate = quantity
        minimum, increment = _lot_rule(exchange)
        while candidate > 0:
            amount_participation = (
                candidate * reference_price / liquidity_amount
                if np.isfinite(liquidity_amount) and liquidity_amount > 0
                else np.nan
            )
            price, slippage, impact = _execution_price(
                reference_price,
                "buy",
                amount_participation,
                volatility,
                market,
                self.spec,
            )
            notional = price * candidate
            fees = self.fees.calculate(notional, "buy", trade_date, exchange)["total_fees"]
            if notional + fees <= self.ledger.cash + 1e-9:
                return candidate, price, slippage, impact
            affordable = int(math.floor(max(0.0, self.ledger.cash - fees) / price))
            candidate = min(candidate - increment, affordable)
            candidate = _round_down(candidate, increment)
            if candidate < minimum:
                return 0, 0.0, 0.0, 0.0
        return 0, 0.0, 0.0, 0.0

    def _base_result(self, order: Mapping[str, Any], position: Position) -> Dict[str, Any]:
        return {
            **order,
            "status": "rejected",
            "block_reason": "",
            "execution_notes": "",
            "submitted_quantity": 0,
            "filled_quantity": 0,
            "unfilled_quantity": order["quantity"],
            "reference_price": np.nan,
            "execution_price": np.nan,
            "notional": 0.0,
            "participation_rate": 0.0,
            "order_amount_participation_rate": np.nan,
            "projected_free_float_fraction": np.nan,
            "normal_exit_days": np.nan,
            "stress_exit_days": np.nan,
            "capacity_quantity": 0,
            "capacity_binding_constraint": "",
            "capacity_adv20_shares": np.nan,
            "capacity_lagged_amount_shares": np.nan,
            "capacity_free_float_shares": np.nan,
            "capacity_stress_exit_shares": np.nan,
            "capacity_impact_shares": np.nan,
            "impact_participation_limit": np.nan,
            "liquidity_reference_amount_lag1": np.nan,
            "amount_participation_rate_for_impact": np.nan,
            "volatility20_daily_lag1": np.nan,
            "impact_y": self.spec.impact_y,
            "impact_bps": 0.0,
            "slippage_bps": 0.0,
            "slippage_cost": 0.0,
            "commission": 0.0,
            "stamp_duty": 0.0,
            "transfer_fee": 0.0,
            "total_fees": 0.0,
            "cash_before": self.ledger.cash,
            "cash_after": self.ledger.cash,
            "position_before": position.total_quantity,
            "position_after": position.total_quantity,
            "sellable_before": position.sellable_quantity,
            "sellable_after": position.sellable_quantity,
            "execution_version": self.spec.version,
            "execution_spec_sha256": self.spec.fingerprint,
            "fee_policy_version": self.fees.version,
            "fee_policy_sha256": self.fees.fingerprint,
        }

    def _reject(self, result: Dict[str, Any], reason: str) -> Dict[str, Any]:
        result["block_reason"] = reason
        return result


def simulate_orders(
    orders: pd.DataFrame,
    tradability: pd.DataFrame,
    initial_cash: float,
    spec: Optional[ExecutionSpec] = None,
    fees: Optional[FeePolicy] = None,
) -> Tuple[pd.DataFrame, PortfolioLedger]:
    """Execute an ordered batch against P0.5 rows and return a full audit trail."""
    required_orders = {
        "order_id",
        "trade_date",
        "instrument_id",
        "symbol",
        "side",
        "quantity",
        "adv_shares_lag1",
    }
    missing_orders = sorted(required_orders - set(orders.columns))
    if missing_orders:
        raise ExecutionError(f"Orders missing columns: {missing_orders}")
    if orders.empty:
        raise ExecutionError("Order blotter is empty")
    if orders["order_id"].astype(str).duplicated().any():
        raise ExecutionError("Order blotter contains duplicate order_id values")
    required_market = {
        "trade_date",
        "instrument_id",
        "symbol",
        "open",
        "data_complete",
        "has_bar",
        "is_suspended",
        "can_buy_at_open",
        "can_sell_at_open",
        "standard_research_eligible",
        "execution_only",
        "research_feature_allowed",
    }
    missing_market = sorted(required_market - set(tradability.columns))
    if missing_market:
        raise ExecutionError(f"Tradability matrix missing columns: {missing_market}")
    market = tradability.copy()
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    if market.duplicated(["instrument_id", "trade_date"]).any():
        raise ExecutionError("Tradability matrix contains duplicate instrument-date keys")
    market_index = market.set_index(["instrument_id", "trade_date"], drop=False)
    work = orders.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"]).dt.normalize()
    work["_sequence"] = np.arange(len(work))
    work = work.sort_values(["trade_date", "_sequence"])
    ledger = PortfolioLedger(initial_cash)
    engine = DailyExecutionEngine(ledger, spec=spec, fees=fees)
    results = []
    for row in work.drop(columns="_sequence").to_dict("records"):
        key = (str(row["instrument_id"]), row["trade_date"])
        if key not in market_index.index:
            raise ExecutionError(f"No P0.5 row for order {row['order_id']}: {key}")
        market_row = market_index.loc[key]
        if isinstance(market_row, pd.DataFrame):
            raise ExecutionError(f"Multiple P0.5 rows for order {row['order_id']}")
        results.append(engine.execute(row, market_row.to_dict()))
    return pd.DataFrame(results), ledger


def _normalize_order(order: Mapping[str, Any]) -> Dict[str, Any]:
    required = {
        "order_id",
        "trade_date",
        "instrument_id",
        "symbol",
        "side",
        "quantity",
        "adv_shares_lag1",
    }
    missing = sorted(required - set(order))
    if missing:
        raise ExecutionError(f"Order missing fields: {missing}")
    side = str(order["side"]).lower()
    if side not in {"buy", "sell"}:
        raise ExecutionError(f"Unsupported order side: {side}")
    quantity = int(order["quantity"])
    if quantity <= 0 or quantity != float(order["quantity"]):
        raise ExecutionError("Order quantity must be a positive integer")
    limit_price = order.get("limit_price")
    if limit_price is not None and not pd.isna(limit_price):
        limit_price = float(limit_price)
        if not np.isfinite(limit_price) or limit_price <= 0:
            raise ExecutionError("limit_price must be finite and positive")
    else:
        limit_price = None
    normalized = {
        **dict(order),
        "order_id": str(order["order_id"]),
        "trade_date": pd.Timestamp(order["trade_date"]).normalize(),
        "instrument_id": str(order["instrument_id"]),
        "symbol": str(order["symbol"]),
        "side": side,
        "quantity": quantity,
        "adv_shares_lag1": float(order["adv_shares_lag1"]),
        "limit_price": limit_price,
    }
    for field in CAPACITY_FIELDS:
        value = order.get(field, np.nan)
        normalized[field] = float(value) if value is not None and not pd.isna(value) else np.nan
    return normalized


def _validate_market_alignment(order: Mapping[str, Any], market: Mapping[str, Any]) -> None:
    if pd.Timestamp(market["trade_date"]).normalize() != order["trade_date"]:
        raise ExecutionError("Order and tradability trade_date do not match")
    if str(market["instrument_id"]) != order["instrument_id"]:
        raise ExecutionError("Order and tradability instrument_id do not match")
    if str(market["symbol"]) != order["symbol"]:
        raise ExecutionError("Order must use the point-in-time historical symbol")
    if market.get("execution_only") is not True and market.get("execution_only") != np.bool_(True):
        raise ExecutionError("P0.5 row is not marked execution_only")
    if bool(market.get("research_feature_allowed", True)):
        raise ExecutionError("P0.5 row is incorrectly allowed as a research feature")


def _market_block_reason(
    market: Mapping[str, Any], side: str, spec: ExecutionSpec
) -> str:
    if not bool(market.get("data_complete", False)):
        return "data_quality_failure"
    if not bool(market.get("has_bar", False)):
        return "no_executable_bar"
    if bool(market.get("is_suspended", False)):
        return "suspended"
    if spec.require_standard_research_eligible and not bool(
        market.get("standard_research_eligible", False)
    ):
        return "outside_standard_research_universe"
    allowed = bool(market[f"can_{side}_at_open"])
    if not allowed:
        return str(market.get(f"{side}_block_reason") or f"{side}_not_executable_at_open")
    return ""


def _submitted_quantity(
    requested: int,
    side: str,
    exchange: str,
    sellable: int,
    total: int,
    enforce_t_plus_one: bool,
) -> Tuple[int, str]:
    minimum, increment = _lot_rule(exchange)
    if side == "buy":
        submitted = _round_down(requested, increment)
        return (submitted if submitted >= minimum else 0), (
            "lot_rounding" if submitted != requested else ""
        )
    available = sellable if enforce_t_plus_one else total
    desired = min(requested, available)
    if desired <= 0:
        return 0, ""
    if desired == available and requested >= available:
        return desired, "position_cap" if requested > available else ""
    submitted = _round_down(desired, increment)
    if submitted < minimum:
        return 0, ""
    return submitted, "lot_rounding" if submitted != requested else ""


def _capacity_quantity(raw_capacity: int, exchange: str, submitted: int) -> int:
    if raw_capacity >= submitted:
        return submitted
    minimum, increment = _lot_rule(exchange)
    capacity = _round_down(raw_capacity, increment)
    return capacity if capacity >= minimum else 0


def _execution_price(
    reference_price: float,
    side: str,
    amount_participation: float,
    volatility_daily: float,
    market: Mapping[str, Any],
    spec: ExecutionSpec,
) -> Tuple[float, float, float]:
    if not np.isfinite(amount_participation) or amount_participation < 0:
        raise ExecutionError("impact calculation requires non-negative amount participation")
    if not np.isfinite(volatility_daily) or volatility_daily <= 0:
        raise ExecutionError("impact calculation requires positive lagged volatility")
    impact = (
        spec.impact_y
        * volatility_daily
        * 10_000.0
        * amount_participation ** spec.impact_exponent
    )
    if impact > spec.max_executable_impact_bps + 1e-9:
        raise ExecutionError("impact capacity failed to constrain executable order")
    slippage_bps = spec.base_slippage_bps + impact
    direction = 1.0 if side == "buy" else -1.0
    raw = reference_price * (1.0 + direction * slippage_bps / 10_000.0)
    tick = spec.price_tick_cny
    if side == "buy":
        price = math.ceil((raw - 1e-12) / tick) * tick
    else:
        price = math.floor((raw + 1e-12) / tick) * tick
    if bool(market.get("has_bounded_price_limit", False)):
        if side == "buy" and pd.notna(market.get("up_limit")):
            price = min(price, float(market["up_limit"]))
        if side == "sell" and pd.notna(market.get("down_limit")):
            price = max(price, float(market["down_limit"]))
    return round(price, 6), slippage_bps, impact


def _lot_rule(exchange: str) -> Tuple[int, int]:
    if exchange == "BJ":
        return 100, 1
    if exchange in {"SH", "SZ"}:
        return 100, 100
    raise ExecutionError(f"Unsupported cash-equity exchange: {exchange}")


def _exchange(symbol: str) -> str:
    suffix = str(symbol).rsplit(".", 1)[-1].upper()
    if suffix not in {"SH", "SZ", "BJ"}:
        raise ExecutionError(f"Cannot infer exchange from symbol: {symbol}")
    return suffix


def _round_down(quantity: int, increment: int) -> int:
    return max(0, int(quantity) // increment * increment)


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
