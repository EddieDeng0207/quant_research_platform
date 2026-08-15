import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from qrp.data.ingestion import (
    DEFAULT_REQUESTS_PER_MINUTE,
    INGESTION_POLICY_VERSION,
    P0BackfillConfig,
    P0IngestionRunner,
    RateLimiter,
)
from qrp.data.providers.base import FetchResult
from qrp.data.storage import ParquetLake


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeProvider:
    def __init__(self):
        self.calls = []

    def fetch_instruments(self):
        self.calls.append("instruments")
        return FetchResult(
            dataset="instruments",
            provider="fake",
            frame=pd.DataFrame(
                {
                    "symbol": ["600000.SH"],
                    "name": ["测试"],
                    "exchange": ["SH"],
                    "list_status": ["L"],
                    "source": ["fake"],
                    "ingested_at": [pd.Timestamp.now(tz="UTC")],
                }
            ),
            query={"endpoint": "instruments"},
        )

    def fetch_calendar(self, start_date, end_date, exchange):
        self.calls.append("calendar")
        return FetchResult(
            dataset="trading_calendar",
            provider="fake",
            frame=pd.DataFrame(
                {
                    "exchange": [exchange, exchange, exchange],
                    "calendar_date": pd.to_datetime(
                        ["2024-01-02", "2024-01-03", "2024-01-04"]
                    ),
                    "is_open": [True, False, True],
                    "source": ["fake"] * 3,
                    "ingested_at": [pd.Timestamp.now(tz="UTC")] * 3,
                }
            ),
            query={"endpoint": "calendar"},
        )

    def fetch_security_code_mappings(self):
        self.calls.append("security_code_mappings")
        return FetchResult(
            dataset="security_code_mappings",
            provider="fake",
            frame=pd.DataFrame(
                {
                    "historical_symbol": ["873690.BJ"],
                    "current_symbol": ["920690.BJ"],
                    "name": ["测试北证"],
                    "list_date": [pd.Timestamp("2024-01-05")],
                    "source": ["fake"],
                    "ingested_at": [pd.Timestamp.now(tz="UTC")],
                }
            ),
            query={"endpoint": "bse_mapping"},
        )

    def fetch_daily_bars_by_date(self, trade_date):
        self.calls.append(f"daily_bars:{trade_date}")
        return _daily_bars_result(trade_date)

    def fetch_adjustment_factors_by_date(self, trade_date):
        self.calls.append(f"adjustment_factors:{trade_date}")
        return FetchResult(
            dataset="adjustment_factors",
            provider="fake",
            frame=pd.DataFrame(
                {
                    "symbol": ["600000.SH"],
                    "trade_date": [pd.Timestamp(trade_date)],
                    "adj_factor": [1.0],
                    "source": ["fake"],
                    "ingested_at": [pd.Timestamp.now(tz="UTC")],
                }
            ),
            query={"trade_date": trade_date},
            partition_values={"trade_date": trade_date},
        )


def _daily_bars_result(trade_date):
    return FetchResult(
        dataset="daily_bars",
        provider="fake",
        frame=pd.DataFrame(
            {
                "symbol": ["600000.SH"],
                "trade_date": [pd.Timestamp(trade_date)],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.5],
                "volume": [1000.0],
                "amount": [10500.0],
                "adjustment": ["raw"],
                "source": ["fake"],
                "ingested_at": [pd.Timestamp.now(tz="UTC")],
            }
        ),
        query={"trade_date": trade_date},
        partition_values={"trade_date": trade_date},
    )


class IngestionTests(unittest.TestCase):
    def test_default_capacity_is_250_requests_per_minute(self):
        config = P0BackfillConfig(start_date="2024-01-02", end_date="2024-01-02")
        limiter = RateLimiter(DEFAULT_REQUESTS_PER_MINUTE)
        self.assertEqual(config.requests_per_minute, 250)
        self.assertEqual(
            config.ingestion_policy_version, "p0_tushare_max_page_v2_rpm250"
        )
        self.assertEqual(
            config.ingestion_policy_version, INGESTION_POLICY_VERSION
        )
        self.assertAlmostEqual(limiter.minimum_interval, 0.24)

    def test_runner_checkpoints_and_skips_completed_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()
            clock = FakeClock()
            runner = P0IngestionRunner(
                provider=provider,
                lake=ParquetLake(root / "lake"),
                artifact_root=root / "artifacts",
                state_root=root / "state",
                limiter=RateLimiter(120, clock=clock, sleeper=clock.sleep),
                sleeper=clock.sleep,
            )
            config = P0BackfillConfig(
                start_date="2024-01-02",
                end_date="2024-01-04",
                datasets=("daily_bars", "adjustment_factors"),
            )
            first_run = runner.run(config)
            calls_after_first = list(provider.calls)
            second_run = runner.run(config)
            self.assertEqual(provider.calls, calls_after_first)
            self.assertEqual(len(calls_after_first), 6)
            first_manifest = json.loads(
                (first_run / "run_manifest.json").read_text(encoding="utf-8")
            )
            second_manifest = json.loads(
                (second_run / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first_manifest["status"], "completed")
            self.assertEqual(first_manifest["summary"]["files_written"], 6)
            self.assertEqual(second_manifest["summary"]["files_written"], 0)
            self.assertEqual(second_manifest["summary"]["skipped_from_checkpoint"], 6)
            self.assertTrue((first_run / "source_snapshot" / "src" / "qrp" / "cli.py").exists())
            self.assertTrue((first_run / "source_snapshot" / "source_manifest.json").exists())
            self.assertFalse((first_run / "source_snapshot" / ".env").exists())

    def test_runner_freezes_security_code_mapping_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()
            clock = FakeClock()
            runner = P0IngestionRunner(
                provider=provider,
                lake=ParquetLake(root / "lake"),
                artifact_root=root / "artifacts",
                state_root=root / "state",
                limiter=RateLimiter(120, clock=clock, sleeper=clock.sleep),
                sleeper=clock.sleep,
            )
            run = runner.run(
                P0BackfillConfig(
                    start_date="2024-01-02",
                    end_date="2024-01-02",
                    datasets=("daily_bars",),
                    include_instruments=False,
                    include_security_code_mappings=True,
                    job_name="p05_test",
                )
            )
            manifest = json.loads(
                (run / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                provider.calls,
                [
                    "security_code_mappings",
                    "calendar",
                    "daily_bars:2024-01-02",
                    "daily_bars:2024-01-04",
                ],
            )
            self.assertEqual(manifest["summary"]["files_written"], 4)

    def test_runner_rate_limits_each_provider_request_without_double_waiting(self):
        class RequestAwareProvider(FakeProvider):
            def set_request_limiter(self, before_request):
                self.before_request = before_request

            def fetch_calendar(self, start_date, end_date, exchange):
                self.before_request()
                return super().fetch_calendar(start_date, end_date, exchange)

            def fetch_daily_bars_by_date(self, trade_date):
                self.before_request()
                return super().fetch_daily_bars_by_date(trade_date)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = RequestAwareProvider()
            clock = FakeClock()
            runner = P0IngestionRunner(
                provider=provider,
                lake=ParquetLake(root / "lake"),
                artifact_root=root / "artifacts",
                state_root=root / "state",
                limiter=RateLimiter(250, clock=clock, sleeper=clock.sleep),
                sleeper=clock.sleep,
            )
            runner.run(
                P0BackfillConfig(
                    start_date="2024-01-02",
                    end_date="2024-01-02",
                    datasets=("daily_bars",),
                    include_instruments=False,
                    job_name="request_rate_limit_test",
                )
            )
            self.assertEqual(clock.value, 0.48)
