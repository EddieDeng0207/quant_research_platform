import tempfile
import unittest
from pathlib import Path

import pandas as pd

from qrp.data.audit import audit_lake, compare_daily_bars
from qrp.data.providers.base import FetchResult
from qrp.data.storage import ParquetLake


def _bars(close=10.5):
    return pd.DataFrame(
        {
            "symbol": ["600000.SH"],
            "trade_date": [pd.Timestamp("2024-01-02")],
            "open": [10.0],
            "high": [11.0],
            "low": [9.5],
            "close": [close],
            "volume": [1000.0],
            "amount": [10500.0],
            "adjustment": ["raw"],
            "source": ["test"],
            "ingested_at": [pd.Timestamp.now(tz="UTC")],
        }
    )


class AuditTests(unittest.TestCase):
    def test_lake_audit_checks_hash_rows_and_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ParquetLake(root).write(
                FetchResult(
                    dataset="daily_bars",
                    provider="test",
                    frame=_bars(),
                    query={"trade_date": "2024-01-02"},
                )
            )
            report = audit_lake(root)
            self.assertTrue(report.passed)
            self.assertEqual(report.files_checked, 1)
            self.assertEqual(report.rows_checked, 1)

    def test_cross_source_comparison_applies_tolerances(self):
        result = compare_daily_bars(_bars(), _bars(close=10.505))
        self.assertTrue(result["passed"])
        result = compare_daily_bars(_bars(), _bars(close=10.52))
        self.assertFalse(result["passed"])
