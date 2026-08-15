import hashlib
import json
import math
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from qrp.execution import (
    ExecutionSpec,
    FeePolicy,
    PortfolioLedger,
    build_execution_artifact,
    simulate_orders,
)
from qrp.execution.daily import DailyExecutionEngine


def _market(trade_dates=("2024-01-02",), **overrides):
    rows = []
    for trade_date in trade_dates:
        row = {
            "trade_date": trade_date,
            "instrument_id": "CN_EQ:000001.SZ",
            "symbol": "000001.SZ",
            "open": 10.0,
            "up_limit": 11.0,
            "down_limit": 9.0,
            "has_bounded_price_limit": True,
            "data_complete": True,
            "has_bar": True,
            "is_suspended": False,
            "can_buy_at_open": True,
            "can_sell_at_open": True,
            "buy_block_reason": "",
            "sell_block_reason": "",
            "standard_research_eligible": True,
            "execution_only": True,
            "research_feature_allowed": False,
        }
        row.update(overrides)
        rows.append(row)
    return pd.DataFrame(rows)


def _orders(rows):
    defaults = {
        "instrument_id": "CN_EQ:000001.SZ",
        "symbol": "000001.SZ",
        "adv_shares_lag1": 1_000_000,
        "adv20_shares_lag1": 1_000_000,
        "adv20_amount_lag1": 10_000_000,
        "adv60_amount_lag1": 12_000_000,
        "median_amount20_lag1": 9_000_000,
        "free_float_market_cap_lag1": 10_000_000_000,
        "volatility20_daily_lag1": 0.025,
    }
    return pd.DataFrame([{**defaults, **row} for row in rows])


def test_fee_policy_is_side_and_effective_date_aware():
    fees = FeePolicy(commission_bps=3.0, minimum_commission_cny=5.0)
    buy = fees.calculate(100_000, "buy", "2024-01-02", "SZ")
    sell = fees.calculate(100_000, "sell", "2024-01-02", "SZ")
    old_sell = fees.calculate(100_000, "sell", "2023-08-25", "SZ")

    assert buy["commission"] == 30.0
    assert buy["stamp_duty"] == 0.0
    assert buy["transfer_fee"] == 1.0
    assert sell["stamp_duty"] == 50.0
    assert old_sell["stamp_duty"] == 100.0


def test_buy_lot_rounding_and_t_plus_one_are_audited():
    orders = _orders(
        [
            {
                "order_id": "buy",
                "trade_date": "2024-01-02",
                "side": "buy",
                "quantity": 1050,
            },
            {
                "order_id": "same_day_sell",
                "trade_date": "2024-01-02",
                "side": "sell",
                "quantity": 1000,
            },
            {
                "order_id": "next_day_sell",
                "trade_date": "2024-01-03",
                "side": "sell",
                "quantity": 500,
            },
        ]
    )
    results, ledger = simulate_orders(
        orders, _market(("2024-01-02", "2024-01-03")), initial_cash=100_000
    )
    by_id = results.set_index("order_id")

    assert by_id.loc["buy", "status"] == "partial"
    assert by_id.loc["buy", "submitted_quantity"] == 1000
    assert by_id.loc["buy", "filled_quantity"] == 1000
    assert by_id.loc["buy", "sellable_after"] == 0
    assert by_id.loc["same_day_sell", "block_reason"] == "t_plus_one_no_sellable_shares"
    assert by_id.loc["next_day_sell", "filled_quantity"] == 500
    assert ledger.position("CN_EQ:000001.SZ").total_quantity == 500
    assert ledger.position("CN_EQ:000001.SZ").sellable_quantity == 500


def test_directional_open_limit_uses_p05_block_reason():
    market = _market(
        can_buy_at_open=False,
        buy_block_reason="open_limit_buy",
        open=11.0,
    )
    orders = _orders(
        [
            {
                "order_id": "limit_up_buy",
                "trade_date": "2024-01-02",
                "side": "buy",
                "quantity": 100,
            }
        ]
    )
    results, _ = simulate_orders(orders, market, initial_cash=100_000)
    assert results.iloc[0]["status"] == "rejected"
    assert results.iloc[0]["block_reason"] == "open_limit_buy"


def test_lagged_adv_caps_participation_and_creates_partial_fill():
    orders = _orders(
        [
            {
                "order_id": "capacity",
                "trade_date": "2024-01-02",
                "side": "buy",
                "quantity": 1000,
                "adv_shares_lag1": 5000,
                "adv20_shares_lag1": 50_000,
                "adv20_amount_lag1": 500_000,
                "adv60_amount_lag1": 600_000,
                "median_amount20_lag1": 500_000,
            }
        ]
    )
    results, _ = simulate_orders(orders, _market(), initial_cash=100_000)
    row = results.iloc[0]
    assert row["status"] == "partial"
    assert row["filled_quantity"] == 500
    assert math.isclose(row["participation_rate"], 0.01)
    assert "liquidity_capacity" in row["execution_notes"]


def test_square_root_impact_scales_with_lagged_volatility():
    low_vol = _orders(
        [
            {
                "order_id": "low-vol",
                "trade_date": "2024-01-02",
                "side": "buy",
                "quantity": 10_000,
                "volatility20_daily_lag1": 0.01,
                "adv60_amount_lag1": 10_000_000,
                "median_amount20_lag1": 10_000_000,
            }
        ]
    )
    high_vol = low_vol.assign(
        order_id="high-vol",
        volatility20_daily_lag1=0.04,
    )
    low_result, _ = simulate_orders(low_vol, _market(), initial_cash=1_000_000)
    high_result, _ = simulate_orders(high_vol, _market(), initial_cash=1_000_000)
    assert low_result.iloc[0]["impact_bps"] == pytest.approx(5.0)
    assert high_result.iloc[0]["impact_bps"] == pytest.approx(20.0)


def test_impact_tolerance_reduces_fill_instead_of_capping_cost():
    orders = _orders(
        [
            {
                "order_id": "impact-capacity",
                "trade_date": "2024-01-02",
                "side": "buy",
                "quantity": 100_000,
                "volatility20_daily_lag1": 0.10,
            }
        ]
    )
    spec = ExecutionSpec(
        max_participation_rate=0.25,
        impact_y=1.0,
        max_executable_impact_bps=100.0,
    )
    result, _ = simulate_orders(
        orders,
        _market(),
        initial_cash=10_000_000,
        spec=spec,
    )
    row = result.iloc[0]
    assert row["status"] == "partial"
    assert row["impact_bps"] <= 100.0 + 1e-9
    assert row["filled_quantity"] < row["submitted_quantity"]


def test_missing_lagged_liquidity_fails_closed():
    orders = _orders(
        [
            {
                "order_id": "missing_adv",
                "trade_date": "2024-01-02",
                "side": "buy",
                "quantity": 100,
                "adv_shares_lag1": float("nan"),
                "adv20_shares_lag1": float("nan"),
            }
        ]
    )
    results, _ = simulate_orders(orders, _market(), initial_cash=100_000)
    assert results.iloc[0]["block_reason"].startswith("missing_capacity_inputs:")


def test_cash_capacity_includes_minimum_commission_and_transfer_fee():
    orders = _orders(
        [
            {
                "order_id": "cash",
                "trade_date": "2024-01-02",
                "side": "buy",
                "quantity": 1000,
            }
        ]
    )
    results, ledger = simulate_orders(orders, _market(), initial_cash=1005.0)
    row = results.iloc[0]
    assert row["filled_quantity"] == 0
    assert row["block_reason"] == "insufficient_cash"
    assert ledger.cash == 1005.0


def test_bse_quantity_can_increment_by_one_after_minimum_100():
    market = _market().assign(
        instrument_id="CN_EQ:BSE:920690.BJ",
        symbol="873690.BJ",
    )
    orders = _orders(
        [
            {
                "order_id": "bj",
                "trade_date": "2024-01-02",
                "instrument_id": "CN_EQ:BSE:920690.BJ",
                "symbol": "873690.BJ",
                "side": "buy",
                "quantity": 123,
            }
        ]
    )
    results, _ = simulate_orders(orders, market, initial_cash=100_000)
    assert results.iloc[0]["filled_quantity"] == 123


def test_standard_universe_gate_is_configurable_but_default_closed():
    market = _market(standard_research_eligible=False)
    order = _orders(
        [
            {
                "order_id": "st",
                "trade_date": "2024-01-02",
                "side": "buy",
                "quantity": 100,
            }
        ]
    )
    default_result, _ = simulate_orders(order, market, initial_cash=100_000)
    opt_in_result, _ = simulate_orders(
        order,
        market,
        initial_cash=100_000,
        spec=ExecutionSpec(require_standard_research_eligible=False),
    )
    assert default_result.iloc[0]["block_reason"] == "outside_standard_research_universe"
    assert opt_in_result.iloc[0]["status"] == "filled"


def test_seeded_position_supports_starting_portfolio_and_odd_lot_liquidation():
    ledger = PortfolioLedger(0.0)
    ledger.seed_position("CN_EQ:000001.SZ", 299)
    engine = DailyExecutionEngine(ledger)
    order = _orders(
        [
            {
                "order_id": "liquidate",
                "trade_date": "2024-01-02",
                "side": "sell",
                "quantity": 299,
            }
        ]
    ).iloc[0].to_dict()
    result = engine.execute(order, _market().iloc[0].to_dict())
    assert result["filled_quantity"] == 299
    assert ledger.position("CN_EQ:000001.SZ").total_quantity == 0


def test_execution_artifact_is_immutable_and_deterministic():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        p05 = root / "p05"
        p05.mkdir()
        p05_parquet = p05 / "tradability.parquet"
        _market().to_parquet(p05_parquet, index=False)
        p05_sha = hashlib.sha256(p05_parquet.read_bytes()).hexdigest()
        (p05 / "manifest.json").write_text(
            json.dumps(
                {
                    "artifact_id": "p05-test",
                    "quality": {"promotion_passed": True},
                    "output": {"sha256": p05_sha},
                }
            ),
            encoding="utf-8",
        )
        order_path = root / "orders.csv"
        _orders(
            [
                {
                    "order_id": "artifact-buy",
                    "trade_date": "2024-01-02",
                    "side": "buy",
                    "quantity": 100,
                }
            ]
        ).to_csv(order_path, index=False)

        first = build_execution_artifact(
            p05, order_path, root / "curated", initial_cash=100_000
        )
        second = build_execution_artifact(
            p05, order_path, root / "curated", initial_cash=100_000
        )
        manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))

        assert first == second
        assert manifest["quality"]["promotion_passed"]
        assert manifest["quality"]["filled_orders"] == 1
        assert manifest["guardrails"]["current_day_volume_for_order_sizing_forbidden"]
