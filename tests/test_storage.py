import tempfile
import unittest
from pathlib import Path

import pandas as pd

from qrp.data.providers.base import FetchResult
from qrp.data.storage import ParquetLake


class StorageTests(unittest.TestCase):
    def test_write_parquet_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            now = pd.Timestamp.now(tz="UTC")
            result = FetchResult(
                dataset="adjustment_factors",
                provider="test",
                frame=pd.DataFrame(
                    {
                        "symbol": ["600000.SH"],
                        "trade_date": [pd.Timestamp("2024-01-02")],
                        "adj_factor": [1.0],
                        "source": ["test"],
                        "ingested_at": [now],
                    }
                ),
                query={"symbol": "600000.SH"},
                partition_values={"trade_date": "2024-01-02"},
            )
            root = Path(directory)
            path = ParquetLake(root).write(result)
            self.assertTrue(path.exists())
            self.assertTrue((root / "manifest.jsonl").exists())
            self.assertIn("trade_date=2024-01-02", str(path))
            self.assertEqual(len(pd.read_parquet(path)), 1)


if __name__ == "__main__":
    unittest.main()
