import pandas as pd
import pytest

from qrp.research.factor_registry import DEFAULT_FACTOR_REGISTRY, SP_TTM, FactorDefinition
from qrp.research.fundamental_factors import (
    FundamentalFactorError,
    SalesToPriceInputSpec,
    _fundamental_decision_schedule,
    _listing_sessions_since_actual_list_date,
    build_pit_ttm_revenue_snapshots,
)


def _income_row(
    period: str,
    revenue: float,
    available: str,
    version: str,
    *,
    instrument: str = "CN_EQ:000001.SZ",
    update_flag: str = "1",
    company_type: str = "1",
):
    available_at = pd.Timestamp(available, tz="UTC")
    return {
        "instrument_id": instrument,
        "report_period": pd.Timestamp(period),
        "report_type": "1",
        "comp_type": company_type,
        "available_at": available_at,
        "announcement_at": available_at - pd.Timedelta(hours=10),
        "source_ingested_at": pd.Timestamp("2026-08-16", tz="UTC"),
        "source_row_sha256": version,
        "source_row_occurrence": 0,
        "version_id": version,
        "update_flag": update_flag,
        "revenue": revenue,
    }


def _decisions(*dates: str) -> pd.DataFrame:
    values = pd.to_datetime(list(dates))
    return pd.DataFrame(
        {
            "decision_date": values,
            "execution_date": values + pd.offsets.BDay(1),
        }
    )


def test_sp_registry_separates_raw_factor_from_selection_layer():
    assert DEFAULT_FACTOR_REGISTRY.get("sp_ttm") == SP_TTM
    assert SP_TTM.layer == "raw_signal"
    assert SP_TTM.family == "fundamental"
    assert SP_TTM.expected_direction == 1
    with pytest.raises(ValueError, match="unsupported raw factor family"):
        FactorDefinition(
            name="bad",
            display_name="bad",
            family="quantitative_stock_selection",
            category="composite",
            layer="raw_signal",
            frequency="weekly",
            expected_direction=1,
            formula="x",
            required_datasets=("x",),
            point_in_time_policy="x",
            missing_value_policy="x",
            company_scope="x",
            version="v1",
        ).validate()


def test_ttm_revenue_replays_revisions_at_their_actual_availability():
    income = pd.DataFrame(
        [
            _income_row("2022-03-31", 20.0, "2022-04-30 01:30", "q1_22"),
            _income_row("2022-12-31", 100.0, "2023-04-20 01:30", "fy_22"),
            _income_row("2023-03-31", 30.0, "2023-04-28 01:30", "q1_23_original", update_flag="0"),
            _income_row("2023-03-31", 35.0, "2023-06-01 01:30", "q1_23_revision"),
        ]
    )
    result = build_pit_ttm_revenue_snapshots(
        income,
        _decisions("2023-05-05", "2023-06-02"),
        "2026-08-17T00:00:00Z",
    )

    first, second = result.sort_values("decision_at").to_dict("records")
    assert first["ttm_revenue"] == pytest.approx(110.0)
    assert first["component_version_ids"] == "q1_23_original|fy_22|q1_22"
    assert second["ttm_revenue"] == pytest.approx(115.0)
    assert second["component_version_ids"] == "q1_23_revision|fy_22|q1_22"
    assert first["ttm_method"] == "current_ytd_plus_prior_fy_minus_prior_ytd"


def test_economically_identical_alias_versions_are_collapsed_and_counted():
    duplicate = _income_row("2022-12-31", 100.0, "2023-04-20 01:30", "fy_22_alias")
    income = pd.DataFrame(
        [
            _income_row("2022-12-31", 100.0, "2023-04-20 01:30", "fy_22"),
            duplicate,
        ]
    )
    result = build_pit_ttm_revenue_snapshots(
        income,
        _decisions("2023-05-05"),
        "2026-08-17T00:00:00Z",
    )

    assert result.iloc[0]["ttm_revenue"] == pytest.approx(100.0)
    assert result.iloc[0]["component_source_version_count"] == 2


def test_sp_collapses_field_equivalent_versions_with_conservative_audit_clock():
    earlier = _income_row("2022-12-31", 100.0, "2023-05-02 01:30", "earlier")
    later = _income_row("2022-12-31", 100.0, "2023-05-02 01:30", "later")
    earlier["announcement_at"] = pd.Timestamp("2023-04-28 08:00", tz="UTC")
    later["announcement_at"] = pd.Timestamp("2023-05-01 08:00", tz="UTC")
    later["source_ingested_at"] = pd.Timestamp("2026-08-17", tz="UTC")

    result = build_pit_ttm_revenue_snapshots(
        pd.DataFrame([earlier, later]),
        _decisions("2023-05-05"),
        "2026-08-18T00:00:00Z",
    )

    assert result.iloc[0]["ttm_revenue"] == pytest.approx(100.0)
    assert result.iloc[0]["component_source_version_count"] == 2
    assert result.iloc[0]["component_equivalent_version_ids"] == "earlier,later"
    assert result.iloc[0]["announcement_at"] == pd.Timestamp(
        "2023-05-01 08:00", tz="UTC"
    )
    assert result.iloc[0]["source_ingested_at"] == pd.Timestamp(
        "2026-08-17", tz="UTC"
    )


def test_sp_rejects_versions_with_different_revenue_at_same_availability():
    income = pd.DataFrame(
        [
            _income_row("2022-12-31", 100.0, "2023-05-02 01:30", "version_a"),
            _income_row("2022-12-31", 101.0, "2023-05-02 01:30", "version_b"),
        ]
    )

    with pytest.raises(FundamentalFactorError, match="economically ambiguous"):
        build_pit_ttm_revenue_snapshots(
            income,
            _decisions("2023-05-05"),
            "2026-08-18T00:00:00Z",
        )


def test_business_discontinuity_prevents_cross_regime_ttm_stitching():
    instrument = "CN_EQ:RESET"
    income = pd.DataFrame(
        [
            _income_row("2022-03-31", 20.0, "2022-04-30 01:30", "q1_22", instrument=instrument),
            _income_row("2022-12-31", 100.0, "2023-04-20 01:30", "fy_22", instrument=instrument),
            _income_row("2023-03-31", 30.0, "2023-04-28 01:30", "q1_23", instrument=instrument),
            _income_row("2023-12-31", 180.0, "2024-04-20 01:30", "fy_23", instrument=instrument),
        ]
    )
    result = build_pit_ttm_revenue_snapshots(
        income,
        _decisions("2023-05-05", "2024-04-26"),
        "2026-08-17T00:00:00Z",
        resets={instrument: pd.Timestamp("2023-01-01")},
    ).sort_values("decision_at")

    assert result.iloc[0]["ttm_status"] == "missing_ttm_component"
    assert pd.isna(result.iloc[0]["ttm_revenue"])
    assert result.iloc[1]["ttm_status"] == "ok"
    assert result.iloc[1]["ttm_revenue"] == pytest.approx(180.0)
    assert result["business_chain_reset_applied"].all()


def test_unsupported_company_type_is_reported_instead_of_silently_missing():
    income = pd.DataFrame(
        [
            _income_row(
                "2022-12-31",
                100.0,
                "2023-04-20 01:30",
                "bank_fy_22",
                company_type="2",
            )
        ]
    )
    result = build_pit_ttm_revenue_snapshots(
        income,
        _decisions("2023-05-05"),
        "2026-08-17T00:00:00Z",
    )

    assert result.iloc[0]["ttm_status"] == "unsupported_company_type"
    assert pd.isna(result.iloc[0]["ttm_revenue"])


def test_sp_spec_rejects_formula_and_unit_drift():
    with pytest.raises(ValueError, match="operating revenue"):
        SalesToPriceInputSpec(revenue_column="total_revenue").validate()
    with pytest.raises(ValueError, match="normalized to CNY"):
        SalesToPriceInputSpec(market_value_unit="10k_CNY").validate()


def test_fundamental_schedule_uses_first_week_without_price_formation_warmup():
    sessions = pd.bdate_range("2023-01-02", periods=80)

    result = _fundamental_decision_schedule(sessions, (5, 10, 20, 60), "W-FRI")

    assert result.iloc[0]["decision_date"] == pd.Timestamp("2023-01-06")
    assert result.iloc[0]["execution_date"] == pd.Timestamp("2023-01-09")
    last = result.iloc[-1]
    decision_position = sessions.get_loc(last["decision_date"])
    assert decision_position + 1 + 60 < len(sessions)


def test_listing_sessions_use_actual_list_date_before_research_window():
    calendar_dates = pd.date_range("2021-01-01", "2023-01-10")
    calendar = pd.DataFrame(
        {
            "calendar_date": calendar_dates,
            "is_open": calendar_dates.weekday < 5,
        }
    )
    market = pd.DataFrame(
        {
            "list_date": [pd.Timestamp("2021-01-04"), pd.Timestamp("2023-01-06")],
            "trade_date": [pd.Timestamp("2023-01-06"), pd.Timestamp("2023-01-10")],
        }
    )

    result = _listing_sessions_since_actual_list_date(market, calendar)

    assert result.iloc[0] > 500
    assert result.iloc[1] == 3


def test_listing_sessions_fail_closed_when_list_date_follows_trade_date():
    calendar = pd.DataFrame(
        {
            "calendar_date": pd.date_range("2021-01-01", "2023-01-10"),
            "is_open": True,
        }
    )
    market = pd.DataFrame(
        {
            "list_date": [pd.Timestamp("2023-01-10")],
            "trade_date": [pd.Timestamp("2023-01-09")],
        }
    )

    with pytest.raises(FundamentalFactorError, match="after trade_date"):
        _listing_sessions_since_actual_list_date(market, calendar)
