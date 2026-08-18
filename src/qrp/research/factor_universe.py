"""Pre-outcome research-universe and factor-applicability contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

import pandas as pd

SPECIAL_CHINEXT_CODES = frozenset({"302132"})


@dataclass(frozen=True)
class ResearchUniverseSpec:
    """Frozen scope rules that must be applied before outcome labels are read."""

    name: str
    allowed_market_segments: tuple[str, ...]
    require_industry_classification: bool
    allowed_company_types: tuple[str, ...]
    minimum_base_universe_retention: float = 0.80
    version: str = "factor_research_universe_v2_special_chinext_codes"

    def validate(self) -> "ResearchUniverseSpec":
        if not self.name or not self.allowed_market_segments:
            raise ValueError("universe name and market segments are required")
        unknown = set(self.allowed_market_segments) - {
            "sh_main",
            "sz_main",
            "chinext",
            "star",
            "bse",
        }
        if unknown:
            raise ValueError(f"unsupported market segments: {sorted(unknown)}")
        if not self.allowed_company_types:
            raise ValueError("allowed_company_types cannot be empty")
        if not 0 < self.minimum_base_universe_retention <= 1:
            raise ValueError("minimum_base_universe_retention must be in (0, 1]")
        return self

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


CN_A_FULL = ResearchUniverseSpec(
    name="cn_a_full",
    allowed_market_segments=("sh_main", "sz_main", "chinext", "star", "bse"),
    require_industry_classification=False,
    allowed_company_types=("1",),
)

CN_A_SW_L1_CORE = ResearchUniverseSpec(
    name="cn_a_sw_l1_core",
    allowed_market_segments=("sh_main", "sz_main", "chinext"),
    require_industry_classification=True,
    allowed_company_types=("1",),
)

RESEARCH_UNIVERSE_PROFILES = {
    CN_A_FULL.name: CN_A_FULL,
    CN_A_SW_L1_CORE.name: CN_A_SW_L1_CORE,
}


def attach_research_universe(
    frame: pd.DataFrame,
    spec: ResearchUniverseSpec,
) -> pd.DataFrame:
    """Attach auditable scope flags without modifying base P0.5 eligibility."""
    frozen = spec.validate()
    required = {
        "symbol",
        "research_eligible",
        "industry_code",
        "latest_company_type",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"factor observations missing universe columns: {missing}")
    result = frame.copy()
    if not pd.api.types.is_bool_dtype(result["research_eligible"].dtype):
        raise ValueError("base research_eligible must be boolean")
    result["market_segment"] = result["symbol"].map(classify_market_segment).astype("string")
    segment_in_scope = result["market_segment"].isin(frozen.allowed_market_segments)
    has_industry = result["industry_code"].notna() & result["industry_code"].astype(
        "string"
    ).str.strip().ne("")
    company_type = result["latest_company_type"].astype("string").str.strip()
    company_type_known = company_type.notna() & company_type.ne("")
    # Missing statements are a coverage failure, not evidence that a company is
    # outside the formula domain.  Only a known, unsupported type may be excluded.
    company_applicable = ~company_type_known | company_type.isin(frozen.allowed_company_types)
    result["universe_in_scope"] = segment_in_scope & (
        has_industry if frozen.require_industry_classification else True
    )
    result["factor_applicable"] = company_applicable
    result["evaluation_eligible"] = (
        result["research_eligible"]
        & result["universe_in_scope"]
        & result["factor_applicable"]
    )
    result["scope_exclusion_reason"] = ""
    result.loc[~segment_in_scope, "scope_exclusion_reason"] = "market_segment_out_of_scope"
    result.loc[
        segment_in_scope & frozen.require_industry_classification & ~has_industry,
        "scope_exclusion_reason",
    ] = "missing_required_industry"
    result.loc[
        result["universe_in_scope"] & ~company_applicable,
        "scope_exclusion_reason",
    ] = "unsupported_company_type"
    result.loc[~result["research_eligible"], "scope_exclusion_reason"] = "p05_ineligible"
    result["research_universe_name"] = frozen.name
    result["research_universe_version"] = frozen.version
    result["research_universe_sha256"] = frozen.fingerprint
    result["research_universe_minimum_retention"] = frozen.minimum_base_universe_retention
    return result


def classify_market_segment(symbol: object) -> str:
    value = str(symbol).strip().upper()
    code, _, exchange = value.partition(".")
    if exchange == "BJ":
        return "bse"
    if exchange == "SH" and code.startswith(("688", "689")):
        return "star"
    if exchange == "SZ" and (
        code.startswith(("300", "301")) or code in SPECIAL_CHINEXT_CODES
    ):
        return "chinext"
    if exchange == "SH" and code.startswith(("600", "601", "603", "605")):
        return "sh_main"
    if exchange == "SZ" and code.startswith(("000", "001", "002", "003")):
        return "sz_main"
    raise ValueError(f"cannot classify A-share market segment: {value}")
