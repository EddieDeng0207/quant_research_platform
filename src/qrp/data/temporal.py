"""Knowledge-time policies and causal data access checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import pandas as pd


class FutureDataError(RuntimeError):
    """Raised when a research decision attempts to observe unavailable data."""


@dataclass(frozen=True)
class AvailabilityPolicy:
    dataset: str
    event_time_column: str
    local_hour: int
    local_minute: int
    rationale: str


AVAILABILITY_POLICIES: Dict[str, AvailabilityPolicy] = {
    "daily_bars": AvailabilityPolicy(
        dataset="daily_bars",
        event_time_column="trade_date",
        local_hour=16,
        local_minute=0,
        rationale="conservative vendor completion time after the A-share close",
    ),
    "adjustment_factors": AvailabilityPolicy(
        dataset="adjustment_factors",
        event_time_column="trade_date",
        local_hour=9,
        local_minute=20,
        rationale="vendor states the daily factor is completed before the open",
    ),
    "daily_indicators": AvailabilityPolicy(
        dataset="daily_indicators",
        event_time_column="trade_date",
        local_hour=17,
        local_minute=0,
        rationale="conservative end of the vendor update window",
    ),
    "stock_status": AvailabilityPolicy(
        dataset="stock_status",
        event_time_column="trade_date",
        local_hour=9,
        local_minute=20,
        rationale="vendor publishes the daily risk-warning snapshot before the open",
    ),
    "daily_limits": AvailabilityPolicy(
        dataset="daily_limits",
        event_time_column="trade_date",
        local_hour=8,
        local_minute=40,
        rationale="vendor states daily limit prices are updated around 08:40",
    ),
    "daily_suspensions": AvailabilityPolicy(
        dataset="daily_suspensions",
        event_time_column="trade_date",
        local_hour=16,
        local_minute=0,
        rationale="vendor update timing is irregular; safe only as an execution-day event",
    ),
}


def attach_available_at(frame: pd.DataFrame, dataset: str) -> pd.DataFrame:
    """Attach conservative UTC knowledge timestamps from a reviewed policy."""
    if dataset not in AVAILABILITY_POLICIES:
        raise ValueError(f"No availability policy registered for {dataset}")
    policy = AVAILABILITY_POLICIES[dataset]
    if policy.event_time_column not in frame:
        raise ValueError(f"Missing event time column: {policy.event_time_column}")
    result = frame.copy()
    local_date = pd.to_datetime(result[policy.event_time_column], errors="coerce").dt.normalize()
    if local_date.isna().any():
        raise ValueError(f"Invalid event dates in {dataset}")
    local_time = local_date + pd.Timedelta(
        hours=policy.local_hour, minutes=policy.local_minute
    )
    result["available_at"] = local_time.dt.tz_localize(
        "Asia/Shanghai", ambiguous="raise", nonexistent="raise"
    ).dt.tz_convert("UTC")
    result["availability_policy"] = f"{dataset}:v1"
    return result


def causal_asof_view(
    frame: pd.DataFrame,
    decision_time: pd.Timestamp,
    available_column: str = "available_at",
) -> pd.DataFrame:
    """Return only rows knowable by a UTC decision timestamp."""
    if available_column not in frame:
        raise FutureDataError(f"Dataset has no {available_column}; causal access is impossible")
    decision = pd.Timestamp(decision_time)
    if decision.tzinfo is None:
        raise ValueError("decision_time must be timezone-aware")
    available = pd.to_datetime(frame[available_column], utc=True, errors="coerce")
    if available.isna().any():
        raise FutureDataError(f"Dataset contains null or invalid {available_column}")
    return frame.loc[available <= decision.tz_convert("UTC")].copy()


def assert_causal(
    frame: pd.DataFrame,
    decision_time: pd.Timestamp,
    available_column: str = "available_at",
) -> None:
    """Fail if even one row was unavailable at the research decision time."""
    view = causal_asof_view(frame, decision_time, available_column)
    if len(view) != len(frame):
        future = len(frame) - len(view)
        raise FutureDataError(
            f"{future} rows were unavailable at decision_time={pd.Timestamp(decision_time).isoformat()}"
        )


def next_session_open(
    signal_date: str,
    trading_calendar: pd.DataFrame,
    execution_lag_sessions: int = 1,
) -> pd.Timestamp:
    """Map a close-derived signal to a later executable A-share session open."""
    if execution_lag_sessions < 1:
        raise ValueError("close-derived signals require execution_lag_sessions >= 1")
    dates = pd.to_datetime(
        trading_calendar.loc[trading_calendar["is_open"], "calendar_date"]
    ).sort_values().drop_duplicates().reset_index(drop=True)
    signal = pd.Timestamp(signal_date).normalize()
    later = dates[dates > signal]
    if len(later) < execution_lag_sessions:
        raise ValueError("Trading calendar does not extend to the requested execution session")
    execution_date = later.iloc[execution_lag_sessions - 1]
    local = execution_date + pd.Timedelta(hours=9, minutes=30)
    return local.tz_localize("Asia/Shanghai").tz_convert("UTC")


def attach_next_session_availability(
    frame: pd.DataFrame,
    event_date_column: str,
    trading_calendar: pd.DataFrame,
    dataset: str,
    open_hour: int = 9,
    open_minute: int = 30,
) -> pd.DataFrame:
    """Conservatively expose disclosures only at the next open trading session."""
    if event_date_column not in frame:
        raise ValueError(f"Missing event date column: {event_date_column}")
    sessions = pd.DatetimeIndex(
        pd.to_datetime(
            trading_calendar.loc[trading_calendar["is_open"], "calendar_date"],
            errors="coerce",
        )
    ).normalize().dropna().unique().sort_values()
    events = pd.to_datetime(frame[event_date_column], errors="coerce").dt.normalize()
    if events.isna().any():
        raise ValueError(f"Invalid {event_date_column} values in {dataset}")
    positions = sessions.searchsorted(events, side="right")
    if (positions >= len(sessions)).any():
        raise ValueError("Trading calendar does not extend beyond every event date")
    available_sessions = pd.Series(sessions.take(positions), index=frame.index)
    local = available_sessions + pd.Timedelta(hours=open_hour, minutes=open_minute)
    result = frame.copy()
    result["available_at"] = local.dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
    result["availability_policy"] = f"{dataset}:next_open_v1"
    return result
