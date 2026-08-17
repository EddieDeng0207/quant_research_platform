import pandas as pd

from qrp.data.corporate_actions import build_corporate_action_events


def _tradability():
    dates = pd.bdate_range("2024-04-29", "2024-05-13")
    return pd.DataFrame(
        {
            "symbol": "000001.SZ",
            "instrument_id": "CN_EQ:000001.SZ",
            "trade_date": dates,
            "list_date": pd.Timestamp("1991-04-03"),
        }
    )


def _raw_actions():
    base = {
        "symbol": "000001.SZ",
        "report_period": "2023-12-31",
        "announcement_date": "2024-03-15",
        "record_date": "2024-05-09",
        "ex_date": "2024-05-10",
        "pay_date": "2024-05-10",
        "cash_per_share_tax": 0.10,
        "bonus_share_ratio": 0.20,
        "source": "tushare",
        "ingested_at": "2024-06-01T00:00:00Z",
    }
    return pd.DataFrame(
        [
            {
                **base,
                "source_action_id": "proposal",
                "process_status": "预案",
                "implementation_announcement_date": None,
            },
            {
                **base,
                "source_action_id": "implemented-old",
                "process_status": "实施",
                "implementation_announcement_date": "2024-04-29",
            },
            {
                **base,
                "source_action_id": "implemented-latest",
                "process_status": "实施",
                "implementation_announcement_date": "2024-04-30",
            },
        ]
    )


def test_corporate_action_builder_uses_latest_implementation_and_splits_legs():
    events, quality = build_corporate_action_events(_raw_actions(), _tradability())
    assert quality["promotion_passed"]
    assert set(events["action_type"]) == {"cash_dividend", "bonus"}
    assert set(events["source_action_id"]) == {"implemented-latest"}
    cash = events.loc[events["action_type"] == "cash_dividend"].iloc[0]
    bonus = events.loc[events["action_type"] == "bonus"].iloc[0]
    assert cash["cash_per_share"] == 0.10
    assert cash["withholding_tax_rate"] == 0.0
    assert bonus["share_ratio"] == 1.20
    assert cash["available_at"] < pd.Timestamp("2024-05-10 01:30:00Z")


def test_corporate_action_builder_fails_closed_on_late_knowledge():
    raw = _raw_actions().tail(1).copy()
    raw["implementation_announcement_date"] = "2024-05-10"
    events, quality = build_corporate_action_events(raw, _tradability())
    assert events.empty
    assert not quality["promotion_passed"]
    assert quality["hard_failures"]["implemented_rows_known_after_ex_open"] == 1


def test_stopped_implementation_is_not_treated_as_implemented():
    raw = _raw_actions().tail(1).copy()
    raw["process_status"] = "停止实施"
    raw["ex_date"] = None
    events, quality = build_corporate_action_events(raw, _tradability())
    assert events.empty
    assert quality["promotion_passed"]
    assert quality["stopped_implementation_rows_excluded"] == 1


def test_prelisting_and_pre_window_actions_are_explicitly_excluded():
    prelisting_market = _tradability().copy()
    prelisting_market["list_date"] = pd.Timestamp("2024-05-13")
    prelisting = _raw_actions().tail(1).copy()
    prelisting["ex_date"] = "2024-05-10"
    events, quality = build_corporate_action_events(prelisting, prelisting_market)
    assert events.empty
    assert quality["promotion_passed"]
    assert quality["prelisting_action_rows_excluded"] == 1

    pre_window = _raw_actions().tail(1).copy()
    pre_window["ex_date"] = None
    pre_window["record_date"] = "2023-12-29"
    events, quality = build_corporate_action_events(pre_window, _tradability())
    assert events.empty
    assert quality["promotion_passed"]
    assert quality["outside_backtest_window_rows_excluded"] == 1


def test_truly_unknown_instrument_remains_a_hard_failure():
    raw = _raw_actions().tail(1).copy()
    raw["symbol"] = "999999.SZ"
    events, quality = build_corporate_action_events(raw, _tradability())
    assert events.empty
    assert not quality["promotion_passed"]
    assert quality["hard_failures"]["implemented_rows_with_unknown_instrument"] == 1
