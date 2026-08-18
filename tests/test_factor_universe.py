import pandas as pd
import pytest

from qrp.research.factor_universe import (
    CN_A_FULL,
    CN_A_SW_L1_CORE,
    attach_research_universe,
    classify_market_segment,
)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000.SH", "sh_main"),
        ("000001.SZ", "sz_main"),
        ("300001.SZ", "chinext"),
        ("302132.SZ", "chinext"),
        ("688001.SH", "star"),
        ("920001.BJ", "bse"),
    ],
)
def test_market_segment_classification_is_explicit(symbol, expected):
    assert classify_market_segment(symbol) == expected


def test_sw_core_scope_exclusions_are_auditable_and_do_not_mutate_p05_eligibility():
    frame = pd.DataFrame(
        {
            "symbol": ["600000.SH", "688001.SH", "920001.BJ", "000001.SZ"],
            "research_eligible": [True, True, True, True],
            "industry_code": ["801780", pd.NA, pd.NA, "801180"],
            "latest_company_type": ["1", "1", "1", "2"],
        }
    )
    result = attach_research_universe(frame, CN_A_SW_L1_CORE)

    assert result["research_eligible"].all()
    assert result["evaluation_eligible"].tolist() == [True, False, False, False]
    assert result["scope_exclusion_reason"].tolist() == [
        "",
        "market_segment_out_of_scope",
        "market_segment_out_of_scope",
        "unsupported_company_type",
    ]
    assert result["research_universe_sha256"].nunique() == 1


def test_full_a_share_profile_keeps_boards_but_preserves_formula_applicability():
    frame = pd.DataFrame(
        {
            "symbol": ["688001.SH", "920001.BJ", "000001.SZ"],
            "research_eligible": [True, True, True],
            "industry_code": [pd.NA, pd.NA, "801180"],
            "latest_company_type": ["1", "2", pd.NA],
        }
    )
    result = attach_research_universe(frame, CN_A_FULL)

    assert result["universe_in_scope"].all()
    assert result["evaluation_eligible"].tolist() == [True, False, True]
    assert result.loc[1, "scope_exclusion_reason"] == "unsupported_company_type"
    assert result.loc[2, "scope_exclusion_reason"] == ""


@pytest.mark.parametrize("symbol", ["510300.SH", "302999.SZ"])
def test_unknown_security_code_fails_closed(symbol):
    with pytest.raises(ValueError, match="cannot classify"):
        classify_market_segment(symbol)
