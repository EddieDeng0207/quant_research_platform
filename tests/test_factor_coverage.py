import pandas as pd

from qrp.data.factor_coverage import _daily_coverage


def test_daily_coverage_counts_overlaps_once_and_preserves_components():
    date = pd.Timestamp("2023-01-06")
    panel = pd.DataFrame(
        {
            "trade_date": [date] * 8,
            "missing_code": list(range(8)),
        }
    )

    coverage, joint = _daily_coverage(panel, minimum_coverage=0.80)

    row = coverage.iloc[0]
    assert row["eligible_rows"] == 8
    assert row["usable_rows"] == 1
    assert row["missing_factor_rows"] == 4
    assert row["invalid_market_cap_rows"] == 4
    assert row["missing_industry_rows"] == 4
    assert row["union_missing_rows"] == 7
    assert row["coverage_ratio"] == 0.125
    assert row["coverage_if_factor_fixed"] == 0.25
    assert row["below_minimum_coverage"]
    assert len(joint) == 8
    assert joint["rows"].sum() == 8
