"""Fail-closed point-in-time contracts for research factor observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd

from qrp.data.temporal import FutureDataError


@dataclass(frozen=True)
class FactorTimingContract:
    family: str
    event_at_column: str
    available_at_column: str = "available_at"
    ingested_at_column: str = "ingested_at"
    research_as_of_at_column: str = "research_as_of_at"
    decision_at_column: str = "decision_at"
    execution_at_column: str = "execution_at"
    vintage_column: Optional[str] = None
    rationale: str = ""


FACTOR_TIMING_CONTRACTS: Dict[str, FactorTimingContract] = {
    "fundamental": FactorTimingContract(
        family="fundamental",
        event_at_column="announcement_at",
        rationale="use actual disclosure time; report period is never knowledge time",
    ),
    "analyst_expectation": FactorTimingContract(
        family="analyst_expectation",
        event_at_column="estimate_published_at",
        vintage_column="estimate_vintage_at",
        rationale="historical consensus must come from a frozen estimate vintage",
    ),
    "alternative": FactorTimingContract(
        family="alternative",
        event_at_column="source_published_at",
        rationale="source publication and platform ingestion are independently audited",
    ),
    "price_volume_close": FactorTimingContract(
        family="price_volume_close",
        event_at_column="market_close_at",
        rationale="close-derived features execute no earlier than a later session",
    ),
}


def validate_factor_timing(
    frame: pd.DataFrame,
    family: str,
    *,
    contract: Optional[FactorTimingContract] = None,
    active_mask: Optional[pd.Series] = None,
) -> Dict[str, int]:
    """Validate timing for rows that can contribute a finite research signal.

    Rows with a missing factor value may legitimately lack an event timestamp (for
    example, a company without enough PIT statements to construct TTM revenue).
    Callers must pass that exclusion explicitly; the default remains fail-closed
    over every row.
    """
    selected = contract or FACTOR_TIMING_CONTRACTS.get(family)
    if selected is None:
        raise ValueError(f"no factor timing contract registered for {family}")
    columns = {
        selected.event_at_column,
        selected.available_at_column,
        selected.ingested_at_column,
        selected.research_as_of_at_column,
        selected.decision_at_column,
        selected.execution_at_column,
    }
    if selected.vintage_column:
        columns.add(selected.vintage_column)
    missing = sorted(columns - set(frame.columns))
    if missing:
        raise FutureDataError(f"{family} factor observations missing timing fields: {missing}")
    if active_mask is None:
        active = pd.Series(True, index=frame.index)
    else:
        active = pd.Series(active_mask, index=frame.index).fillna(False).astype(bool)
    selected_frame = frame.loc[active]
    if selected_frame.empty:
        raise FutureDataError(f"{family} factor observations contain no active timing rows")
    parsed = {
        column: pd.to_datetime(selected_frame[column], utc=True, errors="coerce")
        for column in columns
    }
    null_rows = pd.concat(parsed, axis=1).isna().any(axis=1)
    event_after_available = parsed[selected.event_at_column] > parsed[selected.available_at_column]
    available_after_decision = (
        parsed[selected.available_at_column] > parsed[selected.decision_at_column]
    )
    ingested_after_research_as_of = (
        parsed[selected.ingested_at_column]
        > parsed[selected.research_as_of_at_column]
    )
    decision_after_research_as_of = (
        parsed[selected.decision_at_column]
        > parsed[selected.research_as_of_at_column]
    )
    decision_not_before_execution = (
        parsed[selected.decision_at_column] >= parsed[selected.execution_at_column]
    )
    vintage_after_decision = pd.Series(False, index=frame.index)
    if selected.vintage_column:
        vintage_after_decision = (
            parsed[selected.vintage_column] > parsed[selected.decision_at_column]
        )
    failures = {
        "null_or_invalid_timestamp_rows": int(null_rows.sum()),
        "event_after_available_rows": int(event_after_available.sum()),
        "available_after_decision_rows": int(available_after_decision.sum()),
        "ingested_after_research_as_of_rows": int(
            ingested_after_research_as_of.sum()
        ),
        "decision_after_research_as_of_rows": int(
            decision_after_research_as_of.sum()
        ),
        "multiple_research_as_of_values": int(
            parsed[selected.research_as_of_at_column].nunique() != 1
        ),
        "decision_not_before_execution_rows": int(decision_not_before_execution.sum()),
        "vintage_after_decision_rows": int(vintage_after_decision.sum()),
    }
    if any(failures.values()):
        raise FutureDataError(f"{family} timing contract failed: {failures}")
    return failures
