import hashlib
import json

import pandas as pd
import pytest

from qrp.research.reversal_execution import (
    FactorExecutionInputSpec,
    _load_factor_targets,
    _quality_summary,
)


def test_factor_execution_spec_rejects_noncausal_capacity_policy():
    with pytest.raises(ValueError, match="unsupported capacity_policy"):
        FactorExecutionInputSpec(capacity_policy="same_day_liquidity").validate()


def test_execution_input_quality_accepts_lagged_capacity_before_open():
    trade_date = pd.Timestamp("2023-01-03")
    targets = pd.DataFrame(
        {
            "trade_date": [trade_date, trade_date],
            "instrument_id": ["CN_EQ:000001.SZ", "CN_EQ:600000.SH"],
            "target_weight": [0.49, 0.49],
        }
    )
    market = pd.DataFrame(
        {
            "trade_date": [trade_date, trade_date],
            "instrument_id": ["CN_EQ:000001.SZ", "CN_EQ:600000.SH"],
            "has_bar": [True, True],
            "execution_event_at": pd.to_datetime(
                ["2023-01-03T01:30:00Z", "2023-01-03T01:30:00Z"], utc=True
            ),
        }
    )
    capacity = pd.DataFrame(
        {
            "trade_date": [trade_date, trade_date],
            "instrument_id": ["CN_EQ:000001.SZ", "CN_EQ:600000.SH"],
            "capacity_inputs_complete": [True, True],
            "capacity_available_at": pd.to_datetime(
                ["2023-01-03T01:20:00Z", "2023-01-03T01:20:00Z"], utc=True
            ),
        }
    )

    quality = _quality_summary(
        targets, capacity, market, FactorExecutionInputSpec()
    )

    assert quality["promotion_passed"]
    assert quality["complete_observed_target_rate"] == 1.0


def test_execution_input_quality_rejects_future_capacity_and_bad_weight_sum():
    trade_date = pd.Timestamp("2023-01-03")
    targets = pd.DataFrame(
        {
            "trade_date": [trade_date],
            "instrument_id": ["CN_EQ:000001.SZ"],
            "target_weight": [0.97],
        }
    )
    market = pd.DataFrame(
        {
            "trade_date": [trade_date],
            "instrument_id": ["CN_EQ:000001.SZ"],
            "has_bar": [True],
            "execution_event_at": pd.to_datetime(
                ["2023-01-03T01:30:00Z"], utc=True
            ),
        }
    )
    capacity = pd.DataFrame(
        {
            "trade_date": [trade_date],
            "instrument_id": ["CN_EQ:000001.SZ"],
            "capacity_inputs_complete": [True],
            "capacity_available_at": pd.to_datetime(
                ["2023-01-03T01:40:00Z"], utc=True
            ),
        }
    )

    quality = _quality_summary(
        targets, capacity, market, FactorExecutionInputSpec()
    )

    assert not quality["promotion_passed"]
    assert quality["hard_failures"] == {
        "target_weight_sum_breach_dates": 1,
        "target_market_key_missing_rows": 0,
        "incomplete_capacity_on_observed_target_rows": 0,
        "capacity_available_after_execution_rows": 1,
    }


def test_execution_input_quality_uses_p07_frozen_target_gross_weight():
    trade_date = pd.Timestamp("2023-01-03")
    targets = pd.DataFrame(
        {
            "trade_date": [trade_date, trade_date],
            "instrument_id": ["CN_EQ:000001.SZ", "CN_EQ:600000.SH"],
            "target_weight": [0.45, 0.45],
        }
    )
    market = pd.DataFrame(
        {
            "trade_date": [trade_date, trade_date],
            "instrument_id": ["CN_EQ:000001.SZ", "CN_EQ:600000.SH"],
            "has_bar": [True, True],
            "execution_event_at": pd.to_datetime(
                ["2023-01-03T01:30:00Z", "2023-01-03T01:30:00Z"], utc=True
            ),
        }
    )
    capacity = pd.DataFrame(
        {
            "trade_date": [trade_date, trade_date],
            "instrument_id": ["CN_EQ:000001.SZ", "CN_EQ:600000.SH"],
            "capacity_inputs_complete": [True, True],
            "capacity_available_at": pd.to_datetime(
                ["2023-01-03T01:20:00Z", "2023-01-03T01:20:00Z"], utc=True
            ),
        }
    )

    quality = _quality_summary(
        targets,
        capacity,
        market,
        FactorExecutionInputSpec(),
        expected_target_gross_weight=0.90,
    )

    assert quality["promotion_passed"]
    assert quality["expected_target_gross_weight"] == 0.90


def test_factor_target_handoff_reads_identity_and_gross_weight_from_p07(tmp_path):
    artifact = tmp_path / "artifact_id=test"
    artifact.mkdir()
    targets = pd.DataFrame(
        {
            "decision_at": pd.to_datetime(["2023-01-06T08:00:00Z"], utc=True),
            "execution_at": pd.to_datetime(["2023-01-09T01:30:00Z"], utc=True),
            "instrument_id": ["CN_EQ:000001.SZ"],
            "target_weight": [0.90],
            "factor_name": ["sp_ttm"],
        }
    )
    target_path = artifact / "target_weights.parquet"
    targets.to_parquet(target_path, index=False)
    target_sha = hashlib.sha256(target_path.read_bytes()).hexdigest()
    manifest = {
        "artifact_id": "test",
        "quality": {"promotion_passed": True},
        "identity": {"git_commit": "abc"},
        "factor_spec": {
            "factor_name": "sp_ttm",
            "factor_family": "fundamental",
            "target_gross_weight": 0.90,
            "sha256": "factor-spec-sha",
        },
        "outputs": {"target_weights": {"sha256": target_sha}},
    }
    (artifact / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result, identity = _load_factor_targets(
        artifact, FactorExecutionInputSpec(execution_year=2023)
    )

    assert result.iloc[0]["trade_date"] == pd.Timestamp("2023-01-09")
    assert identity["factor_name"] == "sp_ttm"
    assert identity["target_gross_weight"] == 0.90
