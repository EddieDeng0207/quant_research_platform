import json

import pandas as pd

from qrp.data.fundamental_ingestion import (
    FundamentalBackfillConfig,
    FundamentalIngestionRunner,
)
from qrp.data.providers.base import FetchResult
from qrp.data.storage import ParquetLake


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class _Provider:
    def __init__(self):
        self.calls = []

    def fetch_instruments(self, statuses):
        self.calls.append(("instruments", tuple(statuses)))
        now = pd.Timestamp("2026-08-15", tz="UTC")
        return FetchResult(
            dataset="instruments",
            provider="fake",
            frame=pd.DataFrame(
                {
                    "symbol": ["000001.SZ", "600000.SH", "T600018.SH"],
                    "name": ["平安银行", "浦发银行", "历史证券"],
                    "exchange": ["SZ", "SH", "SH"],
                    "list_status": ["L", "L", "D"],
                    "list_date": pd.to_datetime(["1991-04-03", "1999-11-10", "1993-01-01"]),
                    "instrument_kind": ["stock", "stock", "legacy_stock"],
                    "source": ["fake"] * 3,
                    "ingested_at": [now] * 3,
                }
            ),
            query={"endpoint": "stock_basic"},
        ).validate()

    def fetch_fundamentals(self, statement, symbol, start_date, end_date):
        self.calls.append((statement, symbol, start_date, end_date))
        empty = statement == "cashflow" and symbol == "600000.SH"
        columns = {
            "symbol": pd.Series(dtype="string"),
            "statement_type": pd.Series(dtype="string"),
            "report_period": pd.Series(dtype="datetime64[ns]"),
            "announcement_date": pd.Series(dtype="datetime64[ns]"),
            "actual_announcement_date": pd.Series(dtype="datetime64[ns]"),
            "available_date": pd.Series(dtype="datetime64[ns]"),
            "source_row_sha256": pd.Series(dtype="string"),
            "source_row_occurrence": pd.Series(dtype="int64"),
            "source": pd.Series(dtype="string"),
            "ingested_at": pd.Series(dtype="datetime64[ns, UTC]"),
        }
        frame = pd.DataFrame(columns)
        if not empty:
            frame = pd.DataFrame(
                {
                    "symbol": [symbol],
                    "statement_type": [statement],
                    "report_period": [pd.Timestamp("2023-12-31")],
                    "announcement_date": [pd.Timestamp("2024-04-20")],
                    "actual_announcement_date": [pd.Timestamp("2024-04-22")],
                    "available_date": [pd.Timestamp("2024-04-22")],
                    "source_row_sha256": ["a" * 64],
                    "source_row_occurrence": [0],
                    "source": ["fake"],
                    "ingested_at": [pd.Timestamp("2026-08-15", tz="UTC")],
                }
            )
        return FetchResult(
            dataset=f"fundamentals_{statement}",
            provider="fake",
            frame=frame,
            query={
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
            },
            partition_values={"symbol": symbol},
        ).validate()


def test_fundamental_runner_freezes_universe_checkpoints_and_empty_snapshots(tmp_path):
    provider = _Provider()
    clock = _Clock()
    runner = FundamentalIngestionRunner(
        provider=provider,
        lake=ParquetLake(tmp_path / "lake"),
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
        limiter=None,
        sleeper=clock.sleep,
    )
    config = FundamentalBackfillConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        statements=("income", "cashflow"),
        requests_per_minute=250,
    )
    first = runner.run(config)
    calls = list(provider.calls)
    second = runner.run(config)
    assert provider.calls == calls
    assert len(calls) == 5
    first_manifest = json.loads((first / "run_manifest.json").read_text())
    second_manifest = json.loads((second / "run_manifest.json").read_text())
    assert first_manifest["summary"]["expected_statement_tasks"] == 4
    assert first_manifest["summary"]["empty_snapshots"] == 1
    assert first_manifest["summary"]["excluded_legacy_instruments"] == 1
    assert second_manifest["summary"]["skipped_from_checkpoint"] == 5


def test_explicit_pilot_symbols_do_not_fetch_current_instrument_master(tmp_path):
    provider = _Provider()
    runner = FundamentalIngestionRunner(
        provider=provider,
        lake=ParquetLake(tmp_path / "lake"),
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    )
    runner.run(
        FundamentalBackfillConfig(
            start_date="2024-01-01",
            end_date="2024-12-31",
            statements=("income",),
            symbols=("000001.SZ",),
        )
    )
    assert provider.calls == [("income", "000001.SZ", "2024-01-01", "2024-12-31")]
