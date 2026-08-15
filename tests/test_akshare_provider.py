import unittest

import pandas as pd

from qrp.data.providers.akshare import AkshareProvider


class FakeAkshare:
    @staticmethod
    def stock_zh_a_hist(**kwargs):
        return pd.DataFrame(
            {
                "日期": ["2024-01-02"],
                "开盘": [10.0],
                "最高": [11.0],
                "最低": [9.5],
                "收盘": [10.5],
                "成交量": [123.0],
                "成交额": [129150.0],
                "涨跌幅": [5.0],
                "换手率": [1.0],
            }
        )


class AkshareProviderTests(unittest.TestCase):
    def test_normalizes_units_and_symbol(self):
        result = AkshareProvider(module=FakeAkshare()).fetch_daily_bars(
            "600000", "2024-01-01", "2024-01-03"
        )
        row = result.frame.iloc[0]
        self.assertEqual(row["symbol"], "600000.SH")
        self.assertEqual(row["volume"], 12300.0)
        self.assertEqual(row["amount"], 129150.0)
        self.assertEqual(row["adjustment"], "raw")


if __name__ == "__main__":
    unittest.main()
