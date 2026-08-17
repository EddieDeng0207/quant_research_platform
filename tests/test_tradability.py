import pandas as pd

from qrp.data.tradability import (
    build_tradability_matrix,
    tradability_quality_summary,
)


def _instruments(include_unknown=False):
    symbols = ["000001.SZ", "000002.SZ", "000003.SZ"]
    if include_unknown:
        symbols.append("000004.SZ")
    return pd.DataFrame(
        {
            "symbol": symbols,
            "name": [f"股票{i}" for i in range(len(symbols))],
            "exchange": ["SZSE"] * len(symbols),
            "list_status": ["L"] * len(symbols),
            "list_date": ["2020-01-01"] * len(symbols),
            "delist_date": [pd.NaT] * len(symbols),
        }
    )


def _inputs(include_unknown=False):
    bars = pd.DataFrame(
        {
            "symbol": ["000001.SZ", "000003.SZ"],
            "trade_date": ["2024-01-02", "2024-01-02"],
            "open": [10.0, 5.5],
            "high": [10.5, 5.5],
            "low": [9.8, 5.5],
            "close": [10.2, 5.5],
            "pre_close": [10.0, 5.0],
            "volume": [1000.0, 2000.0],
            "amount": [10200.0, 11000.0],
        }
    )
    limits = pd.DataFrame(
        {
            "symbol": ["000001.SZ", "000003.SZ"],
            "trade_date": ["2024-01-02", "2024-01-02"],
            "pre_close": [10.0, 5.0],
            "up_limit": [11.0, 5.5],
            "down_limit": [9.0, 4.5],
        }
    )
    suspensions = pd.DataFrame(
        {
            "symbol": ["000002.SZ"],
            "trade_date": ["2024-01-02"],
            "suspend_type": ["S"],
            "suspend_timing": [None],
        }
    )
    status = pd.DataFrame(
        {
            "symbol": ["000003.SZ"],
            "trade_date": ["2024-01-02"],
            "status_type": ["ST"],
            "status_name": ["风险警示板"],
        }
    )
    return bars, limits, suspensions, status, _instruments(include_unknown)


def test_matrix_blocks_suspension_and_limit_up_without_zeroing_valuation():
    matrix = build_tradability_matrix(*_inputs(), trading_dates=["2024-01-02"])
    normal = matrix.set_index("symbol").loc["000001.SZ"]
    suspended = matrix.set_index("symbol").loc["000002.SZ"]
    st_limit_up = matrix.set_index("symbol").loc["000003.SZ"]

    assert normal["can_buy_at_open"]
    assert normal["can_sell_at_open"]
    assert not suspended["can_buy_at_open"]
    assert suspended["valuation_method"] == "carry_forward_prior_close"
    assert suspended["data_complete"]
    assert not st_limit_up["can_buy_at_open"]
    assert st_limit_up["can_sell_at_open"]
    assert st_limit_up["one_price_limit_up"]
    assert not st_limit_up["standard_research_eligible"]
    assert matrix["execution_only"].all()
    assert not matrix["research_feature_allowed"].any()
    assert tradability_quality_summary(matrix)["promotion_passed"]


def test_unexplained_missing_bar_fails_promotion_instead_of_becoming_suspension():
    matrix = build_tradability_matrix(*_inputs(include_unknown=True), trading_dates=["2024-01-02"])
    unknown = matrix.set_index("symbol").loc["000004.SZ"]
    quality = tradability_quality_summary(matrix)
    assert unknown["unexplained_missing_bar"]
    assert not unknown["is_suspended"]
    assert not unknown["can_buy_at_open"]
    assert not quality["promotion_passed"]
    assert quality["hard_failures"]["unexplained_missing_bar_rows"] == 1


def test_reviewed_symbol_alias_preserves_historical_code_and_stable_identity():
    instruments = pd.DataFrame(
        {
            "symbol": ["302132.SZ"],
            "name": ["中航成飞"],
            "exchange": ["SZSE"],
            "list_status": ["L"],
            "list_date": ["2010-08-27"],
            "delist_date": [pd.NaT],
        }
    )
    history = pd.DataFrame(
        {
            "symbol": ["300114.SZ"],
            "trade_date": ["2024-01-02"],
            "name": ["中航电测"],
            "list_date": ["2010-08-27"],
        }
    )
    bars = pd.DataFrame(
        {
            "symbol": ["300114.SZ", "302132.SZ"],
            "trade_date": ["2024-01-02", "2024-01-02"],
            "open": [10.0, 10.0],
            "high": [10.5, 10.5],
            "low": [9.8, 9.8],
            "close": [10.2, 10.2],
            "pre_close": [pd.NA, 10.0],
            "volume": [1000.0, 1000.0],
            "amount": [10200.0, 10200.0],
        }
    )
    limits = pd.DataFrame(
        {
            "symbol": ["300114.SZ", "302132.SZ"],
            "trade_date": ["2024-01-02", "2024-01-02"],
            "pre_close": [10.0, 10.0],
            "up_limit": [11.0, 11.0],
            "down_limit": [9.0, 9.0],
        }
    )
    aliases = [
        {
            "current_symbol": "302132.SZ",
            "historical_symbol": "300114.SZ",
            "effective_date": "2025-02-17",
            "stable_instrument_id": "CN_EQ:AVIC_CAC_20100827",
            "legal_continuity": True,
            "business_continuity": False,
            "price_chain_policy": "continuous",
            "fundamental_chain_policy": "reset_at_effective_date",
            "evidence": "https://example.test/reviewed-announcement.pdf",
        }
    ]
    matrix = build_tradability_matrix(
        bars,
        limits,
        pd.DataFrame(columns=["symbol", "trade_date", "suspend_type", "suspend_timing"]),
        pd.DataFrame(columns=["symbol", "trade_date", "status_name"]),
        instruments,
        ["2024-01-02"],
        historical_instruments=history,
        symbol_aliases=aliases,
    )
    row = matrix.iloc[0]
    assert row["symbol"] == "300114.SZ"
    assert row["source_bar_symbol"] == "300114.SZ | 302132.SZ"
    assert row["instrument_id"] == "CN_EQ:AVIC_CAC_20100827"
    assert row["identity_alias_resolved"]
    assert row["limit_pre_close"] == 10.0


def test_bse_mapping_restores_historical_code_in_universe_and_market_data():
    instruments = pd.DataFrame(
        {
            "symbol": ["920690.BJ", "000001.SZ"],
            "name": ["测试北证", "测试深证"],
            "exchange": ["BSE", "SZSE"],
            "list_status": ["L", "L"],
            "list_date": ["2024-01-05", "2020-01-01"],
            "delist_date": [pd.NaT, pd.NaT],
        }
    )
    history = pd.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "trade_date": ["2024-01-05"],
            "name": ["测试深证"],
            "list_date": ["2020-01-01"],
        }
    )
    bars = pd.DataFrame(
        {
            "symbol": ["920690.BJ"],
            "trade_date": ["2024-01-05"],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "pre_close": [10.0],
            "volume": [1000.0],
            "amount": [10200.0],
        }
    )
    limits = pd.DataFrame(
        {
            "symbol": ["920690.BJ"],
            "trade_date": ["2024-01-05"],
            "pre_close": [10.0],
            "up_limit": [99999.99],
            "down_limit": [0.0],
            "price_limit_regime": ["none_vendor_sentinel"],
        }
    )
    mappings = pd.DataFrame(
        {
            "historical_symbol": ["873690.BJ"],
            "current_symbol": ["920690.BJ"],
            "name": ["测试北证"],
            "list_date": ["2024-01-05"],
        }
    )
    matrix = build_tradability_matrix(
        bars,
        limits,
        pd.DataFrame(columns=["symbol", "trade_date", "suspend_type", "suspend_timing"]),
        pd.DataFrame(columns=["symbol", "trade_date", "status_name"]),
        instruments,
        ["2024-01-05"],
        historical_instruments=history,
        security_code_mappings=mappings,
    )
    row = matrix.set_index("symbol").loc["873690.BJ"]
    assert row.name == "873690.BJ"
    assert row["source_universe_symbol"] == "920690.BJ"
    assert row["source_bar_symbol"] == "920690.BJ"
    assert row["instrument_id"] == "CN_EQ:BSE:920690.BJ"
    assert row["identity_alias_resolved"]
    assert row["identity_alias_policy_version"] == "bse_920_transition_v1"
    assert row["universe_source"] == "reviewed_bse_mapping_master_supplement"


def test_pre_bse_neeq_bar_does_not_expand_a_share_universe_backwards():
    instruments = pd.DataFrame(
        {
            "symbol": ["920690.BJ", "000001.SZ"],
            "name": ["测试北证", "测试深证"],
            "exchange": ["BSE", "SZSE"],
            "list_status": ["L", "L"],
            "list_date": ["2024-01-05", "2020-01-01"],
            "delist_date": [pd.NaT, pd.NaT],
        }
    )
    history = pd.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "trade_date": ["2023-01-05"],
            "name": ["测试深证"],
            "list_date": ["2020-01-01"],
        }
    )
    bars = pd.DataFrame(
        {
            "symbol": ["920690.BJ", "000001.SZ"],
            "trade_date": ["2023-01-05", "2023-01-05"],
            "open": [10.0, 10.0],
            "high": [10.5, 10.5],
            "low": [9.8, 9.8],
            "close": [10.2, 10.2],
            "pre_close": [10.0, 10.0],
            "volume": [1000.0, 1000.0],
            "amount": [10200.0, 10200.0],
        }
    )
    mappings = pd.DataFrame(
        {
            "historical_symbol": ["873690.BJ"],
            "current_symbol": ["920690.BJ"],
            "name": ["测试北证"],
            "list_date": ["2024-01-05"],
        }
    )
    matrix = build_tradability_matrix(
        bars,
        pd.DataFrame(
            {
                "symbol": ["000001.SZ"],
                "trade_date": ["2023-01-05"],
                "pre_close": [10.0],
                "up_limit": [11.0],
                "down_limit": [9.0],
            }
        ),
        pd.DataFrame(columns=["symbol", "trade_date", "suspend_type", "suspend_timing"]),
        pd.DataFrame(columns=["symbol", "trade_date", "status_name"]),
        instruments,
        ["2023-01-05"],
        historical_instruments=history,
        security_code_mappings=mappings,
    )

    assert set(matrix["symbol"]) == {"000001.SZ"}
    quality = tradability_quality_summary(matrix)
    assert quality["pre_bse_listing_bar_rows_excluded"] == 1
    assert quality["promotion_passed"]


def test_listing_day_bar_can_repair_a_bounded_historical_master_gap():
    instruments = pd.DataFrame(
        {
            "symbol": ["301260.SZ", "000001.SZ"],
            "name": ["格力博", "测试深证"],
            "exchange": ["SZSE", "SZSE"],
            "list_status": ["L", "L"],
            "list_date": ["2023-02-08", "2020-01-01"],
            "delist_date": [pd.NaT, pd.NaT],
        }
    )
    history = pd.DataFrame(
        {
            "symbol": ["000001.SZ"],
            "trade_date": ["2023-02-08"],
            "name": ["测试深证"],
            "list_date": ["2020-01-01"],
        }
    )
    bars = pd.DataFrame(
        {
            "symbol": ["301260.SZ"],
            "trade_date": ["2023-02-08"],
            "open": [30.0],
            "high": [32.0],
            "low": [29.0],
            "close": [31.0],
            "pre_close": [30.0],
            "volume": [1000.0],
            "amount": [31000.0],
        }
    )
    limits = pd.DataFrame(
        {
            "symbol": ["301260.SZ"],
            "trade_date": ["2023-02-08"],
            "pre_close": [30.0],
            "up_limit": [99999.99],
            "down_limit": [0.0],
            "price_limit_regime": ["none_vendor_sentinel"],
        }
    )
    matrix = build_tradability_matrix(
        bars,
        limits,
        pd.DataFrame(columns=["symbol", "trade_date", "suspend_type", "suspend_timing"]),
        pd.DataFrame(columns=["symbol", "trade_date", "status_name"]),
        instruments,
        ["2023-02-08"],
        historical_instruments=history,
    )
    row = matrix.set_index("symbol").loc["301260.SZ"]
    assert row["has_bar"]
    assert row["universe_source"].endswith("current_master_lifecycle_validated_observed_bar")
