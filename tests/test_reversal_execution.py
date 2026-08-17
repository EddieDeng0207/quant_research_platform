import pandas as pd
import pytest

from qrp.research.reversal_execution import (
    ReversalExecutionInputSpec,
    _quality_summary,
)


def test_reversal_execution_spec_rejects_noncausal_capacity_policy():
    with pytest.raises(ValueError, match="unsupported capacity_policy"):
        ReversalExecutionInputSpec(capacity_policy="same_day_liquidity").validate()


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
        targets, capacity, market, ReversalExecutionInputSpec()
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
        targets, capacity, market, ReversalExecutionInputSpec()
    )

    assert not quality["promotion_passed"]
    assert quality["hard_failures"] == {
        "target_weight_sum_breach_dates": 1,
        "target_market_key_missing_rows": 0,
        "incomplete_capacity_on_observed_target_rows": 0,
        "capacity_available_after_execution_rows": 1,
    }
