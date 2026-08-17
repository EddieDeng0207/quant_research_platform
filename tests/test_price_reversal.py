import numpy as np
import pandas as pd
import pytest

from qrp.research.price_reversal import (
    PriceReversalError,
    PriceReversalInputSpec,
    _calculate_reversal_variants,
    _decision_schedule,
    _local_time,
    _market_cap_cny,
    _prepare_forward_returns,
    _resolve_vendor_field,
)


def test_reversal_window_treats_suspension_as_missing_and_skip1_excludes_latest():
    dates = pd.bdate_range("2024-01-01", periods=125)
    returns = np.full(len(dates), 0.01)
    returns[-2] = np.nan
    market = pd.DataFrame(
        {
            "instrument_id": "CN_EQ:000001.SZ",
            "trade_date": dates,
            "causal_return_1d": returns,
        }
    )
    result = _calculate_reversal_variants(market, PriceReversalInputSpec())
    row = result.iloc[-1]

    assert row["rev20_observed_sessions"] == 19
    assert row["rev20"] == pytest.approx(1.01**19 - 1.0)
    assert row["rev20_skip1_observed_sessions"] == 19
    assert row["rev20_skip1"] == pytest.approx(1.01**19 - 1.0)
    assert result.iloc[118]["listing_sessions"] == 119
    assert np.isnan(result.iloc[118]["rev20"])


def test_decision_schedule_requires_listing_warmup_and_complete_longest_horizon():
    sessions = pd.bdate_range("2023-01-02", periods=260)
    schedule = _decision_schedule(sessions, PriceReversalInputSpec())

    assert not schedule.empty
    first_decision_index = sessions.get_loc(schedule.iloc[0]["decision_date"])
    assert first_decision_index + 1 >= 120
    last = schedule.iloc[-1]
    assert last["horizon_60_end_date"] <= sessions[-1]
    assert last["execution_date"] > last["decision_date"]


def test_forward_returns_are_open_to_open_and_break_on_source_symbol_change():
    spec = PriceReversalInputSpec(horizons=(1,))
    decisions = pd.DataFrame(
        {
            "decision_date": [pd.Timestamp("2024-01-05")],
            "execution_date": [pd.Timestamp("2024-01-08")],
            "horizon_1_end_date": [pd.Timestamp("2024-01-09")],
        }
    )
    execution_at = _local_time(pd.Series([pd.Timestamp("2024-01-08")]), 9, 30).iloc[0]
    observation = pd.DataFrame(
        {
            "instrument_id": ["CN_EQ:000001.SZ", "CN_EQ:000002.SZ"],
            "execution_at": [execution_at, execution_at],
            "factor_value": [0.1, 0.2],
        }
    )
    market = pd.DataFrame(
        {
            "instrument_id": [
                "CN_EQ:000001.SZ",
                "CN_EQ:000001.SZ",
                "CN_EQ:000002.SZ",
                "CN_EQ:000002.SZ",
            ],
            "trade_date": pd.to_datetime(["2024-01-08", "2024-01-09", "2024-01-08", "2024-01-09"]),
            "source_symbol": ["000001.SZ", "000001.SZ", "000002.SZ", "000003.SZ"],
            "total_return_open": [10.0, 11.0, 20.0, 22.0],
            "has_bar": [True, True, True, True],
        }
    )
    labels = _prepare_forward_returns(
        market,
        {"rev20": observation, "rev20_skip1": observation},
        decisions,
        pd.Timestamp("2024-01-09"),
        spec,
    )

    first = labels.loc[labels["instrument_id"] == "CN_EQ:000001.SZ"].iloc[0]
    second = labels.loc[labels["instrument_id"] == "CN_EQ:000002.SZ"].iloc[0]
    assert first["forward_return"] == pytest.approx(0.10)
    assert pd.isna(second["forward_return"])
    assert first["label_start_at"] == first["execution_at"]
    assert first["label_end_at"] > first["label_start_at"]


def test_reversal_spec_rejects_inconsistent_observation_requirement():
    with pytest.raises(ValueError, match="min_observed_sessions"):
        PriceReversalInputSpec(min_observed_sessions=21).validate()
    with pytest.raises(ValueError, match="normalized to CNY"):
        PriceReversalInputSpec(market_value_unit="10k_CNY").validate()


def test_market_cap_preserves_lake_normalized_cny_unit():
    values = pd.Series([31_968_880_000.0])
    assert _market_cap_cny(values).iloc[0] == 31_968_880_000.0


def test_multi_source_alias_field_requires_economically_identical_values():
    frame = pd.DataFrame(
        {
            "instrument_id": ["CN_EQ:ALIAS"],
            "symbol": ["300114.SZ"],
            "source_bar_symbol": ["300114.SZ | 302132.SZ"],
            "trade_date": pd.to_datetime(["2024-01-05"]),
        }
    )
    vendor = pd.DataFrame(
        {
            "symbol": ["300114.SZ", "302132.SZ"],
            "trade_date": pd.to_datetime(["2024-01-05", "2024-01-05"]),
            "adj_factor": [2.5, 2.5],
        }
    )
    resolved = _resolve_vendor_field(
        frame,
        vendor,
        value_column="adj_factor",
        output_column="adj_factor",
        source_output_column="adjustment_source_symbol",
    )
    assert resolved.iloc[0]["adj_factor"] == 2.5
    assert resolved.iloc[0]["adjustment_source_symbol"] == "300114.SZ"

    vendor.loc[vendor["symbol"] == "302132.SZ", "adj_factor"] = 3.0
    with pytest.raises(PriceReversalError, match="alias candidates disagree"):
        _resolve_vendor_field(
            frame,
            vendor,
            value_column="adj_factor",
            output_column="adj_factor",
            source_output_column="adjustment_source_symbol",
        )
