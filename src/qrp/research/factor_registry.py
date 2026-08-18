"""Frozen taxonomy and definitions for reproducible factor research."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Dict, Iterable

RAW_FACTOR_FAMILIES = frozenset(
    {"fundamental", "analyst_expectation", "alternative", "price_volume_close"}
)
FACTOR_LAYERS = frozenset({"raw_signal", "composite_selection"})


@dataclass(frozen=True)
class FactorDefinition:
    """Research identity for one raw signal or composite selection score."""

    name: str
    display_name: str
    family: str
    category: str
    layer: str
    frequency: str
    expected_direction: int
    formula: str
    required_datasets: tuple[str, ...]
    point_in_time_policy: str
    missing_value_policy: str
    company_scope: str
    version: str

    def validate(self) -> "FactorDefinition":
        if not self.name or not self.version:
            raise ValueError("factor name and version are required")
        if self.layer not in FACTOR_LAYERS:
            raise ValueError(f"unsupported factor layer: {self.layer}")
        if self.layer == "raw_signal" and self.family not in RAW_FACTOR_FAMILIES:
            raise ValueError(f"unsupported raw factor family: {self.family}")
        if self.expected_direction not in {-1, 1}:
            raise ValueError("expected_direction must be -1 or 1")
        if not self.required_datasets:
            raise ValueError("a factor must declare at least one required dataset")
        return self

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class FactorRegistry:
    """Small fail-closed registry; portfolio rules do not belong here."""

    def __init__(self, definitions: Iterable[FactorDefinition] = ()) -> None:
        self._definitions: Dict[str, FactorDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: FactorDefinition) -> None:
        frozen = definition.validate()
        existing = self._definitions.get(frozen.name)
        if existing is not None and existing != frozen:
            raise ValueError(f"conflicting factor definition: {frozen.name}")
        self._definitions[frozen.name] = frozen

    def get(self, name: str) -> FactorDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise KeyError(f"unregistered factor: {name}") from exc

    def all(self) -> tuple[FactorDefinition, ...]:
        return tuple(self._definitions[name] for name in sorted(self._definitions))


SP_TTM = FactorDefinition(
    name="sp_ttm",
    display_name="Sales-to-Price (TTM)",
    family="fundamental",
    category="valuation",
    layer="raw_signal",
    frequency="weekly_decision_quarterly_information",
    expected_direction=1,
    formula="pit_ttm_operating_revenue_cny / decision_date_total_market_value_cny",
    required_datasets=(
        "fundamentals_income",
        "daily_indicators",
        "historical_industry_membership",
        "tradability",
    ),
    point_in_time_policy=(
        "each TTM component uses the latest unambiguous statement version available "
        "at decision_at and ingested no later than research_as_of_at"
    ),
    missing_value_policy=(
        "no annualization or zero fill; missing TTM components, stale reports, "
        "non-positive sales, and unsupported company types produce NaN"
    ),
    company_scope="ordinary_industrial_comp_type_1",
    version="sp_ttm_pit_v1",
)

REV20_SKIP1 = FactorDefinition(
    name="rev20_skip1",
    display_name="20-session Reversal, Skip Most Recent Session",
    family="price_volume_close",
    category="reversal",
    layer="raw_signal",
    frequency="weekly_decision_daily_information",
    expected_direction=-1,
    formula="compound(causal_total_return[t-20:t-1])",
    required_datasets=("tradability", "adjustment_factors", "daily_indicators"),
    point_in_time_policy="formation data must be available no later than decision_at",
    missing_value_policy="require at least 15 observed sessions; suspension is missing",
    company_scope="standard_research_eligible_a_share",
    version="rev20_skip1_pit_v2",
)

DEFAULT_FACTOR_REGISTRY = FactorRegistry((SP_TTM, REV20_SKIP1))
