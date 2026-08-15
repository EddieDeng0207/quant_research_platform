import io
import json
import unittest
from urllib.parse import parse_qs, urlparse

import pandas as pd

from qrp.data.providers.fred import FredProvider


class FakeResponse:
    def __init__(self, payload):
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self._body.read()


class RecordingOpener:
    def __init__(self):
        self.query = None

    def __call__(self, request, timeout):
        self.query = parse_qs(urlparse(request.full_url).query)
        return FakeResponse(
            {
                "observations": [
                    {
                        "realtime_start": "2024-02-01",
                        "realtime_end": "2024-03-01",
                        "date": "2024-01-01",
                        "value": "3.1",
                    }
                ]
            }
        )


class FredProviderTests(unittest.TestCase):
    def test_preserves_realtime_window_and_redacts_key_from_query_metadata(self):
        opener = RecordingOpener()
        result = FredProvider(api_key="not-a-real-key", opener=opener).fetch_series(
            "CPIAUCSL", observation_start="2024-01-01"
        )
        row = result.frame.iloc[0]
        self.assertEqual(row["value"], 3.1)
        self.assertEqual(row["realtime_start"], pd.Timestamp("2024-02-01"))
        self.assertNotIn("api_key", result.query)
        self.assertEqual(opener.query["api_key"], ["not-a-real-key"])


if __name__ == "__main__":
    unittest.main()
