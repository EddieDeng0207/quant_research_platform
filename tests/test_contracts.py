import unittest

import pandas as pd

from qrp.data.contracts import (
    DataContractError,
    normalize_cn_instrument_symbol,
    normalize_cn_symbol,
    validate_dataset,
)


class ContractTests(unittest.TestCase):
    def test_normalize_cn_symbols(self):
        self.assertEqual(normalize_cn_symbol("600000"), "600000.SH")
        self.assertEqual(normalize_cn_symbol("000001.sz"), "000001.SZ")
        self.assertEqual(normalize_cn_symbol("830799"), "830799.BJ")

    def test_legacy_instrument_identity_is_preserved_but_not_tradable_symbol(self):
        self.assertEqual(normalize_cn_instrument_symbol("T600018.SH"), "T600018.SH")
        with self.assertRaises(DataContractError):
            normalize_cn_symbol("T600018.SH")

    def test_reject_duplicate_daily_bars(self):
        now = pd.Timestamp.now(tz="UTC")
        row = {
            "symbol": "600000.SH",
            "trade_date": pd.Timestamp("2024-01-02"),
            "open": 1.0,
            "high": 2.0,
            "low": 1.0,
            "close": 1.5,
            "volume": 100.0,
            "amount": 150.0,
            "adjustment": "raw",
            "source": "test",
            "ingested_at": now,
        }
        with self.assertRaises(DataContractError):
            validate_dataset("daily_bars", pd.DataFrame([row, row]))


if __name__ == "__main__":
    unittest.main()
