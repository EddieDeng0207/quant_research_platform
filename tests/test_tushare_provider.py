import unittest

import pandas as pd

from qrp.data.providers.base import ProviderError
from qrp.data.providers.tushare import TUSHARE_PAGE_SIZES, TushareProvider


class FakeTushareClient:
    @staticmethod
    def daily(**kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": ["20240102"],
                "open": [10.0],
                "high": [11.0],
                "low": [9.5],
                "close": [10.5],
                "pre_close": [10.0],
                "change": [0.5],
                "pct_chg": [5.0],
                "vol": [123.0],
                "amount": [129.15],
            }
        )

    @staticmethod
    def income(**kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "ann_date": ["20240420"],
                "f_ann_date": ["20240422"],
                "end_date": ["20240331"],
                "report_type": ["1"],
                "revenue": [100.0],
            }
        )

    @staticmethod
    def adj_factor(**kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [kwargs.get("trade_date", "20240102")],
                "adj_factor": [1.23],
            }
        )

    @staticmethod
    def daily_basic(**kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [kwargs.get("trade_date", "20240102")],
                "turnover_rate": [1.2],
                "total_mv": [100.0],
                "circ_mv": [80.0],
            }
        )

    @staticmethod
    def stock_st(**kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "name": ["测试"],
                "trade_date": [kwargs["trade_date"]],
                "type": ["ST"],
                "type_name": ["风险警示板"],
            }
        )

    @staticmethod
    def stk_limit(**kwargs):
        return pd.DataFrame(
            {
                "trade_date": [kwargs["trade_date"]],
                "ts_code": ["000001.SZ"],
                "pre_close": [10.0],
                "up_limit": [11.0],
                "down_limit": [9.0],
            }
        )

    @staticmethod
    def suspend_d(**kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": [kwargs["trade_date"]],
                "suspend_type": ["S"],
                "suspend_timing": [None],
            }
        )

    @staticmethod
    def bak_basic(**kwargs):
        return pd.DataFrame(
            {
                "trade_date": [kwargs["trade_date"]],
                "ts_code": ["000001.SZ"],
                "name": ["平安银行"],
                "industry": ["银行"],
                "area": ["深圳"],
                "list_date": ["19910403"],
            }
        )

    @staticmethod
    def bse_mapping(**kwargs):
        return pd.DataFrame(
            {
                "name": ["测试北证"],
                "o_code": ["873690.BJ"],
                "n_code": ["920690.BJ"],
                "list_date": ["20240105"],
            }
        )

    @staticmethod
    def dividend(**kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "end_date": ["20231231"],
                "ann_date": ["20240315"],
                "div_proc": ["实施"],
                "stk_div": [0.2],
                "stk_bo_rate": [0.1],
                "stk_co_rate": [0.1],
                "cash_div": [0.09],
                "cash_div_tax": [0.1],
                "record_date": ["20240509"],
                "ex_date": ["20240510"],
                "pay_date": ["20240510"],
                "div_listdate": ["20240510"],
                "imp_ann_date": ["20240430"],
                "base_date": ["20240509"],
                "base_share": [10000.0],
            }
        )


class TushareProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = TushareProvider(client=FakeTushareClient())

    def test_daily_bar_units(self):
        row = self.provider.fetch_daily_bars(
            "000001.SZ", "2024-01-01", "2024-01-03"
        ).frame.iloc[0]
        self.assertEqual(row["volume"], 12300.0)
        self.assertEqual(row["amount"], 129150.0)

    def test_fundamental_uses_actual_announcement_date(self):
        row = self.provider.fetch_fundamentals(
            "income", "000001.SZ", "2024-01-01", "2024-12-31"
        ).frame.iloc[0]
        self.assertEqual(row["report_period"], pd.Timestamp("2024-03-31"))
        self.assertEqual(row["available_date"], pd.Timestamp("2024-04-22"))

    def test_full_market_daily_partitions_by_trade_date(self):
        result = self.provider.fetch_daily_bars_by_date("2024-01-02")
        self.assertEqual(result.partition_values, {"trade_date": "2024-01-02"})
        self.assertEqual(
            result.query,
            {"endpoint": "daily", "trade_date": "2024-01-02", "page_size": 6000},
        )
        self.assertEqual(result.metadata["pagination"]["rows_fetched"], 1)

    def test_full_market_adjustments_and_indicators_normalize_units(self):
        adjustment = self.provider.fetch_adjustment_factors_by_date("2024-01-02")
        indicators = self.provider.fetch_daily_indicators_by_date("2024-01-02")
        self.assertEqual(adjustment.frame.iloc[0]["adj_factor"], 1.23)
        self.assertEqual(indicators.frame.iloc[0]["total_mv"], 1_000_000.0)
        self.assertEqual(indicators.frame.iloc[0]["circ_mv"], 800_000.0)

    def test_stock_status_contract(self):
        row = self.provider.fetch_stock_status_by_date("2024-01-02").frame.iloc[0]
        self.assertEqual(row["status_type"], "ST")
        self.assertEqual(row["status_name"], "风险警示板")

    def test_daily_limit_and_suspension_contracts(self):
        limit = self.provider.fetch_daily_limits_by_date("2024-01-02")
        suspension = self.provider.fetch_daily_suspensions_by_date("2024-01-02")
        self.assertEqual(limit.frame.iloc[0]["up_limit"], 11.0)
        self.assertEqual(limit.partition_values, {"trade_date": "2024-01-02"})
        self.assertEqual(suspension.frame.iloc[0]["suspend_type"], "S")

    def test_daily_limit_allows_optional_pre_close(self):
        class NullPreCloseClient(FakeTushareClient):
            @staticmethod
            def stk_limit(**kwargs):
                frame = FakeTushareClient.stk_limit(**kwargs)
                frame["pre_close"] = None
                return frame

        result = TushareProvider(client=NullPreCloseClient()).fetch_daily_limits_by_date(
            "2024-01-02"
        )
        self.assertTrue(pd.isna(result.frame.iloc[0]["pre_close"]))

    def test_daily_limit_preserves_explicit_no_limit_sentinel(self):
        class NoLimitClient(FakeTushareClient):
            @staticmethod
            def stk_limit(**kwargs):
                frame = FakeTushareClient.stk_limit(**kwargs)
                frame["ts_code"] = "920690.BJ"
                frame["up_limit"] = 99999.99
                frame["down_limit"] = 0.0
                return frame

        result = TushareProvider(client=NoLimitClient()).fetch_daily_limits_by_date(
            "2024-01-05"
        )
        self.assertEqual(
            result.frame.iloc[0]["price_limit_regime"], "none_vendor_sentinel"
        )

    def test_empty_suspension_event_set_is_a_valid_covered_snapshot(self):
        class EmptySuspensionClient(FakeTushareClient):
            @staticmethod
            def suspend_d(**kwargs):
                return pd.DataFrame()

        provider = TushareProvider(client=EmptySuspensionClient())
        result = provider.fetch_daily_suspensions_by_date("2024-01-02")
        self.assertTrue(result.frame.empty)
        self.assertEqual(result.partition_values, {"trade_date": "2024-01-02"})

    def test_historical_instrument_snapshot_is_partitioned(self):
        result = self.provider.fetch_historical_instruments_by_date("2024-01-02")
        self.assertEqual(result.frame.iloc[0]["symbol"], "000001.SZ")
        self.assertEqual(result.partition_values, {"trade_date": "2024-01-02"})

    def test_bse_mapping_preserves_both_codes_and_listing_date(self):
        result = self.provider.fetch_security_code_mappings()
        row = result.frame.iloc[0]
        self.assertEqual(row["historical_symbol"], "873690.BJ")
        self.assertEqual(row["current_symbol"], "920690.BJ")
        self.assertEqual(row["list_date"], pd.Timestamp("2024-01-05"))

    def test_corporate_actions_preserve_knowledge_and_effective_dates(self):
        result = self.provider.fetch_corporate_actions(
            "000001.SZ", "2024-01-01", "2024-12-31"
        )
        row = result.frame.iloc[0]
        self.assertEqual(row["implementation_announcement_date"], pd.Timestamp("2024-04-30"))
        self.assertEqual(row["record_date"], pd.Timestamp("2024-05-09"))
        self.assertEqual(row["cash_per_share_tax"], 0.1)
        self.assertEqual(row["bonus_share_ratio"], 0.2)
        self.assertEqual(len(row["source_action_id"]), 24)

    def test_endpoint_page_sizes_match_reviewed_maxima(self):
        self.assertEqual(
            TUSHARE_PAGE_SIZES,
            {
                "stock_basic": 6000,
                "daily": 6000,
                "adj_factor": 6000,
                "daily_basic": 6000,
                "stock_st": 1000,
                "stk_limit": 5800,
                "suspend_d": 5000,
                "bak_basic": 7000,
                "bse_mapping": 1000,
                "dividend": 2000,
            },
        )

    def test_pagination_uses_maximum_page_and_advances_offset(self):
        class MultiPageClient:
            calls = []

            @classmethod
            def daily(cls, **kwargs):
                cls.calls.append((kwargs["limit"], kwargs["offset"]))
                if kwargs["offset"] == 0:
                    return pd.DataFrame({"row": range(6000)})
                return pd.DataFrame({"row": [6000]})

        request_ticks = []
        provider = TushareProvider(client=MultiPageClient())
        provider.set_request_limiter(lambda: request_ticks.append("request"))
        frame, metadata = provider._fetch_paginated("daily", {"trade_date": "20240102"})
        self.assertEqual(MultiPageClient.calls, [(6000, 0), (6000, 6000)])
        self.assertEqual(request_ticks, ["request", "request"])
        self.assertEqual(len(frame), 6001)
        self.assertEqual(metadata["requests"], 2)
        self.assertEqual(metadata["pages_with_data"], 2)

    def test_pagination_rejects_repeated_pages(self):
        class RepeatedPageClient:
            @staticmethod
            def daily(**kwargs):
                return pd.DataFrame({"row": range(6000)})

        with self.assertRaisesRegex(ProviderError, "repeated a page"):
            TushareProvider(client=RepeatedPageClient())._fetch_paginated(
                "daily", {"trade_date": "20240102"}
            )


if __name__ == "__main__":
    unittest.main()
