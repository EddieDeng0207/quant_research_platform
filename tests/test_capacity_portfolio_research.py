import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qrp.data.temporal import FutureDataError
from qrp.execution import (
    ExecutionSpec,
    PortfolioLedger,
    build_lagged_capacity_panel,
    calibrate_broker_fills,
    generate_target_weight_orders,
    net_orders,
    simulate_execution_scenarios,
)
from qrp.research import (
    ExperimentSpec,
    benjamini_hochberg,
    register_experiment,
    validate_factor_timing,
    walk_forward_splits,
)
from qrp.versioning import VersionControlError


def _capacity_values():
    return {
        "adv_shares_lag1": 1_000_000,
        "adv20_shares_lag1": 1_000_000,
        "adv20_amount_lag1": 10_000_000,
        "adv60_amount_lag1": 12_000_000,
        "median_amount20_lag1": 9_000_000,
        "free_float_market_cap_lag1": 10_000_000_000,
        "volatility20_daily_lag1": 0.025,
    }


def _market(dates):
    return pd.DataFrame(
        [
            {
                "trade_date": date,
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
                "standard_research_eligible": True,
                "execution_only": True,
                "research_feature_allowed": False,
            }
            for date in dates
        ]
    )


def test_capacity_panel_is_shifted_and_point_in_time():
    dates = pd.date_range("2024-01-01", periods=70, freq="D")
    bars = pd.DataFrame(
        {
            "symbol": "000001.SZ",
            "trade_date": dates,
            "volume": np.arange(1, 71) * 100.0,
            "amount": np.arange(1, 71) * 1000.0,
            "close": 10.0 + np.sin(np.arange(1, 71) / 3.0),
        }
    )
    indicators = pd.DataFrame(
        {
            "symbol": "000001.SZ",
            "trade_date": dates,
            "circ_mv": np.arange(1, 71) * 1_000_000.0,
        }
    )
    adjustments = pd.DataFrame(
        {
            "symbol": "000001.SZ",
            "trade_date": dates,
            "adj_factor": 1.0,
        }
    )
    panel = build_lagged_capacity_panel(bars, indicators, adjustments)
    day_61 = panel.iloc[60]
    assert day_61["adv20_shares_lag1"] == pytest.approx(np.mean(np.arange(41, 61) * 100))
    assert day_61["adv60_amount_lag1"] == pytest.approx(np.mean(np.arange(1, 61) * 1000))
    assert day_61["free_float_market_cap_lag1"] == 60_000_000
    assert day_61["volatility20_daily_lag1"] > 0
    assert day_61["capacity_inputs_complete"]


def test_capacity_defaults_are_one_percent_and_exit_constrained():
    spec = ExecutionSpec()
    assert spec.max_participation_rate == 0.01
    assert spec.stress_exit_participation_rate == 0.02
    assert spec.max_stress_exit_days == 3.0


def test_target_weights_generate_sell_first_orders_with_capacity():
    targets = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-02",
                "instrument_id": "CN_EQ:000001.SZ",
                "symbol": "000001.SZ",
                "target_weight": 0.10,
            }
        ]
    )
    positions = pd.DataFrame(
        [
            {"instrument_id": "CN_EQ:000001.SZ", "total_quantity": 20_000},
            {"instrument_id": "CN_EQ:000002.SZ", "total_quantity": 1_000},
        ]
    )
    prices = pd.DataFrame(
        [
            {
                "trade_date": "2024-01-02",
                "instrument_id": "CN_EQ:000001.SZ",
                "symbol": "000001.SZ",
                "reference_price": 10.0,
            },
            {
                "trade_date": "2024-01-02",
                "instrument_id": "CN_EQ:000002.SZ",
                "symbol": "000002.SZ",
                "reference_price": 20.0,
            },
            {
                "trade_date": "2024-01-02",
                "instrument_id": "CN_EQ:000003.SZ",
                "symbol": "000003.SZ",
                "reference_price": np.nan,
            },
        ]
    )
    panel = prices[["trade_date", "instrument_id", "symbol"]].copy()
    for key, value in _capacity_values().items():
        if key != "adv_shares_lag1":
            panel[key] = value
    orders = generate_target_weight_orders(
        targets,
        positions,
        prices,
        portfolio_nav=1_000_000,
        capacity_panel=panel,
    )
    assert list(orders["side"]) == ["sell", "sell"]
    assert set(orders["instrument_id"]) == {"CN_EQ:000001.SZ", "CN_EQ:000002.SZ"}
    assert orders["adv20_amount_lag1"].notna().all()


def test_order_netting_removes_self_turnover():
    orders = pd.DataFrame(
        [
            {"trade_date": "2024-01-02", "instrument_id": "x", "symbol": "000001.SZ", "side": "buy", "quantity": 500},
            {"trade_date": "2024-01-02", "instrument_id": "x", "symbol": "000001.SZ", "side": "sell", "quantity": 200},
        ]
    )
    result = net_orders(orders)
    assert len(result) == 1
    assert result.iloc[0]["side"] == "buy"
    assert result.iloc[0]["quantity"] == 300


def test_ledger_tracks_cost_pnl_dividends_and_split():
    ledger = PortfolioLedger(100_000)
    ledger.apply_buy("x", 1000, 10_000, 5)
    ledger.advance("2024-01-02")
    ledger.apply_corporate_action(
        {"instrument_id": "x", "action_type": "cash_dividend", "cash_per_share": 0.2, "effective_date": "2024-01-03"}
    )
    ledger.apply_corporate_action(
        {"instrument_id": "x", "action_type": "split", "share_ratio": 2.0, "effective_date": "2024-01-03"}
    )
    detail, account = ledger.mark_to_market({"x": 5.5}, "2024-01-03")
    assert ledger.position("x").total_quantity == 2000
    assert ledger.position("x").average_cost == pytest.approx(5.0025)
    assert account["cumulative_dividends"] == 200
    assert detail.iloc[0]["unrealized_pnl"] == pytest.approx(995)


def test_execution_scenarios_include_stress_and_delay_coverage():
    orders = pd.DataFrame(
        [
            {
                "order_id": "o1",
                "trade_date": "2024-01-02",
                "instrument_id": "CN_EQ:000001.SZ",
                "symbol": "000001.SZ",
                "side": "buy",
                "quantity": 100,
                **_capacity_values(),
            }
        ]
    )
    executions, summary = simulate_execution_scenarios(
        orders,
        _market(["2024-01-02", "2024-01-03"]),
        initial_cash=100_000,
    )
    assert set(summary["scenario"]) == {
        "base_open",
        "commission_aware_open",
        "conservative_open",
        "delay_one_session",
    }
    assert set(executions["scenario"]) == set(summary["scenario"])
    delayed = executions.loc[executions["scenario"] == "delay_one_session"].iloc[0]
    assert str(delayed["trade_date"].date()) == "2024-01-03"


def test_factor_timing_fails_closed_on_future_vintage():
    valid = pd.DataFrame(
        [
            {
                "estimate_published_at": "2024-01-01T01:00:00Z",
                "estimate_vintage_at": "2024-01-01T02:00:00Z",
                "available_at": "2024-01-01T02:00:00Z",
                "ingested_at": "2026-08-14T03:00:00Z",
                "research_as_of_at": "2026-08-15T00:00:00Z",
                "decision_at": "2024-01-01T08:00:00Z",
                "execution_at": "2024-01-02T01:30:00Z",
            }
        ]
    )
    assert not any(validate_factor_timing(valid, "analyst_expectation").values())
    invalid = valid.copy()
    invalid["estimate_vintage_at"] = "2024-01-03T00:00:00Z"
    with pytest.raises(FutureDataError):
        validate_factor_timing(invalid, "analyst_expectation")
    invalid_ingestion = valid.copy()
    invalid_ingestion["ingested_at"] = "2026-08-16T00:00:00Z"
    with pytest.raises(FutureDataError):
        validate_factor_timing(invalid_ingestion, "analyst_expectation")


def test_experiment_registry_is_deterministic_and_periods_are_locked():
    spec = ExperimentSpec(
        name="value_v1",
        hypothesis="cheaper stocks outperform after neutralization",
        train_start="2015-01-01",
        train_end="2019-12-31",
        validation_start="2020-01-01",
        validation_end="2021-12-31",
        test_start="2022-01-01",
        test_end="2024-12-31",
        random_seed=7,
        factor_family="fundamental",
        universe_version="u1",
        execution_artifact_id="e1",
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repository = root / "repo"
        repository.mkdir()
        code_file = repository / "factor.py"
        code_file.write_text("FACTOR_VERSION = 1\n", encoding="utf-8")
        (repository / "requirements.lock").write_text(
            "pandas==2.3.3\n", encoding="utf-8"
        )
        subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repository,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "baseline"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        experiment_root = root / "experiments"
        first = register_experiment(
            experiment_root,
            spec,
            data_artifacts={"prices": "abc"},
            code_files=[code_file],
            parameters={"winsorize": 0.01},
            repository_root=repository,
        )
        second = register_experiment(
            experiment_root,
            spec,
            data_artifacts={"prices": "abc"},
            code_files=[code_file],
            parameters={"winsorize": 0.01},
            repository_root=repository,
        )
        assert first == second
        manifest = json.loads((first / "manifest.json").read_text())
        assert manifest["guardrails"]["locked_test_period"]
        assert manifest["guardrails"]["git_commit_bound"]
        assert manifest["code_identity"]["working_tree_clean"]
        assert (first / "source_bundle.tar").exists()

        code_file.write_text("FACTOR_VERSION = 2\n", encoding="utf-8")
        with pytest.raises(VersionControlError):
            register_experiment(
                experiment_root,
                spec,
                data_artifacts={"prices": "abc"},
                code_files=[code_file],
                parameters={"winsorize": 0.01},
                repository_root=repository,
            )


def test_multiple_testing_and_walk_forward_helpers():
    adjusted = benjamini_hochberg([0.01, 0.04, 0.03])
    assert np.allclose(adjusted, [0.03, 0.04, 0.04])
    splits = walk_forward_splits(
        pd.date_range("2024-01-01", periods=12),
        train_size=4,
        validation_size=2,
        test_size=2,
    )
    assert len(splits) == 3
    assert splits[0]["test_start"] > splits[0]["validation_end"]


def test_broker_calibration_stays_unready_below_sample_gate():
    fills = pd.DataFrame(
        [
            {
                "order_id": "o1",
                "trade_date": "2024-01-02",
                "instrument_id": "x",
                "side": "buy",
                "arrival_price": 10.0,
                "filled_price": 10.01,
                "filled_quantity": 1000,
                "commission": 5.0,
                "tax": 0.0,
                "transfer_fee": 0.1,
                "adv20_amount_lag1": 10_000_000,
            }
        ]
    )
    summary, diagnostics = calibrate_broker_fills(fills, minimum_group_samples=30)
    assert not diagnostics["implementation_ready"]
    assert not summary.iloc[0]["calibration_ready"]
