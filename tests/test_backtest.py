import hashlib
import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from qrp.backtest import (
    BacktestSpec,
    build_backtest_artifact,
    generate_backtest_report,
    run_portfolio_backtest,
)
from qrp.backtest.artifact import (
    _validate_corporate_action_input,
    backtest_quality_summary,
)
from qrp.backtest.engine import (
    _p05_terminal_delisting_actions,
    build_stale_valuation_bounds,
)
from qrp.execution import ExecutionSpec
from qrp.execution.daily import ExecutionError
from qrp.execution.scenarios import ExecutionScenario


def _market():
    rows = []
    for date, close in zip(pd.date_range("2024-01-02", periods=5), [10.0, 10.2, 10.1, 10.3, 10.4]):
        local = date + pd.Timedelta(hours=9, minutes=30)
        rows.append(
            {
                "trade_date": date,
                "instrument_id": "CN_EQ:000001.SZ",
                "symbol": "000001.SZ",
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "limit_pre_close": close,
                "up_limit": close * 1.10,
                "down_limit": close * 0.90,
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
                "can_mark_to_market": True,
                "execution_event_at": local.tz_localize("Asia/Shanghai").tz_convert("UTC"),
            }
        )
    return pd.DataFrame(rows)


def _capacity():
    rows = []
    for date in pd.date_range("2024-01-02", periods=5):
        rows.append(
            {
                "trade_date": date,
                "instrument_id": "CN_EQ:000001.SZ",
                "adv20_shares_lag1": 10_000_000,
                "adv20_amount_lag1": 100_000_000,
                "adv60_amount_lag1": 120_000_000,
                "median_amount20_lag1": 90_000_000,
                "free_float_market_cap_lag1": 10_000_000_000,
                "volatility20_daily_lag1": 0.025,
                "capacity_available_at": (
                    date + pd.Timedelta(hours=9, minutes=20)
                ).tz_localize("Asia/Shanghai").tz_convert("UTC"),
            }
        )
    return pd.DataFrame(rows)


def _targets():
    return pd.DataFrame(
        [
            {
                "trade_date": "2024-01-02",
                "instrument_id": "CN_EQ:000001.SZ",
                "symbol": "000001.SZ",
                "target_weight": 0.50,
                "decision_at": "2024-01-01T08:00:00Z",
            },
            {
                "trade_date": "2024-01-04",
                "instrument_id": "CN_EQ:000001.SZ",
                "symbol": "000001.SZ",
                "target_weight": 0.0,
                "decision_at": "2024-01-03T08:00:00Z",
            },
        ]
    )


def _actions():
    return pd.DataFrame(
        [
            {
                "action_id": "div-1",
                "instrument_id": "CN_EQ:000001.SZ",
                "symbol": "000001.SZ",
                "action_type": "cash_dividend",
                "announcement_at": "2023-12-01T01:00:00Z",
                "available_at": "2023-12-01T02:00:00Z",
                "ex_date": "2024-01-03",
                "record_date": "2024-01-02",
                "pay_date": "2024-01-05",
                "cash_per_share": 0.10,
                "withholding_tax_rate": 0.0,
            }
        ]
    )


def test_backtest_clock_preserves_dividend_entitlement_after_sale():
    result = run_portfolio_backtest(
        _targets(),
        _market(),
        _capacity(),
        initial_cash=100_000,
        corporate_actions=_actions(),
    )
    assert set(result.scenario_summary["scenario"]) == {
        "base_open",
        "commission_aware_open",
        "conservative_open",
        "delay_one_session",
    }
    base_actions = result.corporate_action_ledger.loc[
        result.corporate_action_ledger["scenario"] == "base_open"
    ]
    record = base_actions.loc[base_actions["processing_stage"] == "record_close"].iloc[0]
    pay = base_actions.loc[base_actions["processing_stage"] == "pay_open"].iloc[0]
    assert record["entitled_quantity"] > 0
    assert pay["cash_after"] - pay["cash_before"] == record["entitled_quantity"] * 0.10
    base_nav = result.daily_nav.loc[
        result.daily_nav["scenario"] == "base_open"
    ].set_index("trade_date")
    assert base_nav.loc[pd.Timestamp("2024-01-03"), "dividend_receivable"] > 0
    assert base_nav.loc[pd.Timestamp("2024-01-04"), "dividend_receivable"] > 0
    assert base_nav.loc[pd.Timestamp("2024-01-05"), "dividend_receivable"] == 0
    assert not result.daily_nav.duplicated(["scenario", "trade_date"]).any()
    assert (result.capacity_history["strategy_capacity_aum"].dropna() > 0).all()
    median_capacity = result.capacity_history.groupby("scenario")[
        "strategy_capacity_aum"
    ].median()
    assert median_capacity["conservative_open"] < median_capacity["base_open"]
    assert (result.scenario_summary["zero_capacity_dates"] == 0).all()
    assert (result.scenario_summary["positive_capacity_date_rate"] == 1.0).all()
    assert (
        result.scenario_summary["conditional_positive_capacity_p10"] > 0
    ).all()


def test_commission_aware_scenario_suppresses_small_routine_trade_but_not_exit():
    targets = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-02",
                "instrument_id": "CN_EQ:000001.SZ",
                "symbol": "000001.SZ",
                "target_weight": 0.10,
                "decision_at": "2024-01-01T08:00:00Z",
            },
            {
                "trade_date": "2024-01-04",
                "instrument_id": "CN_EQ:000001.SZ",
                "symbol": "000001.SZ",
                "target_weight": 0.0,
                "decision_at": "2024-01-03T08:00:00Z",
            },
        ]
    )
    result = run_portfolio_backtest(
        targets,
        _market(),
        _capacity(),
        initial_cash=100_000,
        scenarios=(
            ExecutionScenario(
                name="commission_aware_open",
                minimum_routine_trade_notional_cny=16_666.67,
            ),
        ),
    )
    assert len(result.suppressed_orders) == 1
    assert result.suppressed_orders.iloc[0]["order_reason"] == "routine_rebalance"
    assert result.executions.empty
    summary = result.scenario_summary.iloc[0]
    assert summary["suppressed_orders"] == 1
    assert summary["suppressed_notional"] > 0

    exit_target = targets.head(1).copy()
    exit_target["target_weight"] = 0.0
    initial_positions = pd.DataFrame(
        [
            {
                "instrument_id": "CN_EQ:000001.SZ",
                "total_quantity": 1_000,
                "sellable_quantity": 1_000,
                "average_cost": 10.0,
                "last_price": 10.0,
            }
        ]
    )
    exit_result = run_portfolio_backtest(
        exit_target,
        _market(),
        _capacity(),
        initial_cash=90_000,
        initial_positions=initial_positions,
        scenarios=(
            ExecutionScenario(
                name="commission_aware_open",
                minimum_routine_trade_notional_cny=16_666.67,
            ),
        ),
    )
    assert exit_result.suppressed_orders.empty
    assert exit_result.executions.iloc[0]["side"] == "sell"
    assert exit_result.executions.iloc[0]["status"] == "filled"


def test_unfilled_target_is_rebuilt_and_filled_on_next_session():
    market = _market()
    market.loc[market["trade_date"] == pd.Timestamp("2024-01-02"), "can_buy_at_open"] = False
    market.loc[market["trade_date"] == pd.Timestamp("2024-01-02"), "buy_block_reason"] = "limit_up_open"
    result = run_portfolio_backtest(
        _targets().head(1),
        market,
        _capacity(),
        initial_cash=100_000,
        scenarios=(ExecutionScenario(name="base_open"),),
    )
    executions = result.executions.sort_values("trade_date")
    assert executions.iloc[0]["status"] == "rejected"
    assert executions.iloc[1]["status"] in {"filled", "partial"}
    assert executions.iloc[0]["order_id"] != executions.iloc[1]["order_id"]


def test_cash_only_target_has_valid_nav_accounting():
    targets = _targets().head(1).copy()
    targets["target_weight"] = 0.0
    result = run_portfolio_backtest(
        targets,
        _market(),
        _capacity(),
        initial_cash=100_000,
        scenarios=(ExecutionScenario(name="base_open"),),
    )
    quality = backtest_quality_summary(result, BacktestSpec(), ExecutionSpec())
    assert quality["promotion_passed"]
    assert result.daily_positions.empty
    assert (result.daily_nav["nav"] == 100_000).all()


def test_stale_valuation_is_bounded_and_can_promote_within_tolerance():
    result = run_portfolio_backtest(
        _targets().head(1),
        _market(),
        _capacity(),
        initial_cash=100_000,
        scenarios=(ExecutionScenario(name="base_open"),),
    )
    result.daily_positions.loc[
        result.daily_positions.index[0], "stale_sessions"
    ] = 21
    spec = BacktestSpec(max_stale_valuation_nav_bound_pp=60.0)
    result.stale_valuation_bounds = build_stale_valuation_bounds(
        result.daily_nav,
        result.daily_positions,
        spec,
    )
    quality = backtest_quality_summary(result, spec, ExecutionSpec())
    assert quality["promotion_passed"]
    assert quality["hard_failures"]["stale_valuation_bound_not_reported"] == 0
    assert quality["hard_failures"]["stale_valuation_bound_exceeds_tolerance"] == 0
    assert quality["stale_valuation_breach_rows"] == 1
    assert quality["stale_valuation_breach_instruments"] == 1
    assert 0 < quality["stale_valuation_nav_bound_pp"] < 60


def test_missing_stale_valuation_endpoint_blocks_promotion():
    result = run_portfolio_backtest(
        _targets().head(1),
        _market(),
        _capacity(),
        initial_cash=100_000,
        scenarios=(ExecutionScenario(name="base_open"),),
    )
    result.stale_valuation_bounds = result.stale_valuation_bounds.loc[
        result.stale_valuation_bounds["valuation_scenario"] != "stale_at_zero"
    ].copy()
    quality = backtest_quality_summary(result, BacktestSpec(), ExecutionSpec())
    assert not quality["promotion_passed"]
    assert quality["hard_failures"]["stale_valuation_bound_not_reported"] > 0


def test_stale_valuation_bound_above_tolerance_blocks_promotion():
    result = run_portfolio_backtest(
        _targets().head(1),
        _market(),
        _capacity(),
        initial_cash=100_000,
        scenarios=(ExecutionScenario(name="base_open"),),
    )
    result.daily_positions.loc[
        result.daily_positions.index[0], "stale_sessions"
    ] = 21
    spec = BacktestSpec(max_stale_valuation_nav_bound_pp=2.0)
    result.stale_valuation_bounds = build_stale_valuation_bounds(
        result.daily_nav,
        result.daily_positions,
        spec,
    )
    quality = backtest_quality_summary(result, spec, ExecutionSpec())
    assert not quality["promotion_passed"]
    assert quality["hard_failures"]["stale_valuation_bound_not_reported"] == 0
    assert quality["hard_failures"]["stale_valuation_bound_exceeds_tolerance"] == 1


def test_p05_delist_date_creates_zero_recovery_terminal_event():
    market = _market().copy()
    market["delist_date"] = pd.Timestamp("2024-01-04")
    calendar = pd.DatetimeIndex(market["trade_date"].unique()).sort_values()
    actions = _p05_terminal_delisting_actions(market, calendar)
    assert len(actions) == 1
    assert actions.iloc[0]["action_type"] == "delisting_cash_settlement"
    assert actions.iloc[0]["settlement_price"] == 0.0
    assert actions.iloc[0]["available_at"] < pd.Timestamp(
        "2024-01-04T01:30:00Z"
    )


def test_backtest_artifact_is_promoted_and_deterministic():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        p05 = root / "p05"
        p05.mkdir()
        market_path = p05 / "tradability.parquet"
        _market().to_parquet(market_path, index=False)
        market_sha = hashlib.sha256(market_path.read_bytes()).hexdigest()
        (p05 / "manifest.json").write_text(
            json.dumps(
                {
                    "artifact_id": "p05-backtest-test",
                    "quality": {"promotion_passed": True},
                    "output": {"sha256": market_sha},
                }
            ),
            encoding="utf-8",
        )
        capacity_path = root / "capacity.parquet"
        target_path = root / "targets.parquet"
        action_path = root / "actions.parquet"
        _capacity().to_parquet(capacity_path, index=False)
        _targets().to_parquet(target_path, index=False)
        _actions().to_parquet(action_path, index=False)
        first = build_backtest_artifact(
            p05,
            capacity_path,
            target_path,
            root / "curated",
            initial_cash=100_000,
            corporate_actions_path=action_path,
        )
        second = build_backtest_artifact(
            p05,
            capacity_path,
            target_path,
            root / "curated",
            initial_cash=100_000,
            corporate_actions_path=action_path,
        )
        manifest = json.loads((first / "manifest.json").read_text())
        report = generate_backtest_report(first, root / "report.md")
        assert first == second
        assert manifest["quality"]["promotion_passed"]
        assert (
            manifest["schema_version"]
            == "p063_portfolio_backtest_v3_capacity_diagnostics"
        )
        assert manifest["outputs"]["stale_valuation_bounds"]["rows"] > 0
        assert manifest["quality"]["hard_failures"]["nav_accounting_tie_failure_rows"] == 0
        assert manifest["artifact_id"] in report.read_text(encoding="utf-8")


def test_formal_corporate_action_input_requires_full_query_coverage():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        action_path = root / "corporate_actions.parquet"
        _actions().to_parquet(action_path, index=False)
        action_sha = hashlib.sha256(action_path.read_bytes()).hexdigest()
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "artifact_id": "actions-test",
                    "identity": {
                        "p05_artifact_id": "p05-test",
                        "p05_manifest_sha256": "p05-manifest-sha",
                    },
                    "quality": {
                        "promotion_passed": True,
                        "query_coverage": None,
                        "hard_failures": {},
                    },
                    "output": {"sha256": action_sha},
                    "guardrails": {
                        "full_universe_query_coverage_proven": False,
                    },
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ExecutionError, match="query-coverage proof"):
            _validate_corporate_action_input(
                action_path,
                {"artifact_id": "p05-test"},
                "p05-manifest-sha",
                require_full_query_coverage=True,
            )
