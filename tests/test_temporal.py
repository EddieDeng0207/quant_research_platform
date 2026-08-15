import pandas as pd
import pytest

from qrp.data.temporal import (
    FutureDataError,
    assert_causal,
    attach_available_at,
    attach_next_session_availability,
    causal_asof_view,
    next_session_open,
)


def test_daily_bar_is_not_available_before_conservative_close_timestamp():
    frame = attach_available_at(
        pd.DataFrame({"trade_date": ["2024-01-02"], "close": [10.0]}), "daily_bars"
    )
    assert frame.loc[0, "available_at"] == pd.Timestamp("2024-01-02 08:00:00+00:00")
    before = pd.Timestamp("2024-01-02 15:30", tz="Asia/Shanghai")
    after = pd.Timestamp("2024-01-02 16:01", tz="Asia/Shanghai")
    assert causal_asof_view(frame, before).empty
    assert len(causal_asof_view(frame, after)) == 1
    with pytest.raises(FutureDataError):
        assert_causal(frame, before)


def test_close_signal_can_only_execute_at_later_session_open():
    calendar = pd.DataFrame(
        {
            "calendar_date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "is_open": [True, False, True],
        }
    )
    execution = next_session_open("2024-01-02", calendar)
    assert execution == pd.Timestamp("2024-01-04 01:30:00+00:00")
    with pytest.raises(ValueError):
        next_session_open("2024-01-02", calendar, execution_lag_sessions=0)


def test_disclosure_is_conservatively_available_at_next_session_open():
    calendar = pd.DataFrame(
        {
            "calendar_date": ["2024-01-05", "2024-01-06", "2024-01-08"],
            "is_open": [True, False, True],
        }
    )
    disclosure = pd.DataFrame(
        {"symbol": ["000001.SZ"], "actual_announcement_date": ["2024-01-05"]}
    )
    result = attach_next_session_availability(
        disclosure, "actual_announcement_date", calendar, "fundamentals"
    )
    assert result.loc[0, "available_at"] == pd.Timestamp("2024-01-08 01:30:00+00:00")
