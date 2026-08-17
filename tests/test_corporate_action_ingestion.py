import hashlib
import json
from pathlib import Path

import pandas as pd

from qrp.data.corporate_action_ingestion import (
    CorporateActionBackfillConfig,
    CorporateActionIngestionRunner,
    frozen_symbols_from_tradability,
    load_completed_corporate_action_run,
)
from qrp.data.corporate_actions import (
    build_corporate_action_artifact_from_ingestion_run,
)
from qrp.data.providers.base import FetchResult
from qrp.data.storage import ParquetLake


def _raw_frame(symbol: str, empty: bool) -> pd.DataFrame:
    columns = {
        "symbol": pd.Series(dtype="string"),
        "report_period": pd.Series(dtype="datetime64[ns]"),
        "announcement_date": pd.Series(dtype="datetime64[ns]"),
        "process_status": pd.Series(dtype="string"),
        "bonus_share_ratio": pd.Series(dtype="float64"),
        "bonus_issue_ratio": pd.Series(dtype="float64"),
        "capitalization_ratio": pd.Series(dtype="float64"),
        "cash_per_share_after_tax": pd.Series(dtype="float64"),
        "cash_per_share_tax": pd.Series(dtype="float64"),
        "record_date": pd.Series(dtype="datetime64[ns]"),
        "ex_date": pd.Series(dtype="datetime64[ns]"),
        "pay_date": pd.Series(dtype="datetime64[ns]"),
        "bonus_listing_date": pd.Series(dtype="datetime64[ns]"),
        "implementation_announcement_date": pd.Series(dtype="datetime64[ns]"),
        "base_date": pd.Series(dtype="datetime64[ns]"),
        "base_shares_10k": pd.Series(dtype="float64"),
        "source_action_id": pd.Series(dtype="string"),
        "source": pd.Series(dtype="string"),
        "ingested_at": pd.Series(dtype="datetime64[ns, UTC]"),
    }
    if empty:
        return pd.DataFrame(columns)
    return pd.DataFrame(
        {
            "symbol": [symbol],
            "report_period": [pd.Timestamp("2022-12-31")],
            "announcement_date": [pd.Timestamp("2023-04-01")],
            "process_status": ["实施"],
            "bonus_share_ratio": [0.0],
            "bonus_issue_ratio": [0.0],
            "capitalization_ratio": [0.0],
            "cash_per_share_after_tax": [0.09],
            "cash_per_share_tax": [0.10],
            "record_date": [pd.Timestamp("2023-05-10")],
            "ex_date": [pd.Timestamp("2023-05-11")],
            "pay_date": [pd.Timestamp("2023-05-12")],
            "bonus_listing_date": [pd.NaT],
            "implementation_announcement_date": [pd.Timestamp("2023-05-01")],
            "base_date": [pd.Timestamp("2023-05-01")],
            "base_shares_10k": [100.0],
            "source_action_id": ["action-1"],
            "source": ["fake"],
            "ingested_at": [pd.Timestamp("2026-08-17", tz="UTC")],
        }
    )


class _Provider:
    def __init__(self):
        self.calls = []

    def fetch_corporate_actions(self, symbol, start_date, end_date):
        self.calls.append((symbol, start_date, end_date))
        return FetchResult(
            dataset="corporate_actions",
            provider="fake",
            frame=_raw_frame(symbol, empty=symbol == "600000.SH"),
            query={"symbol": symbol, "start": start_date, "end": end_date},
            partition_values={"symbol": symbol},
        ).validate()


def _p05(root: Path) -> Path:
    artifact = root / "p05"
    artifact.mkdir()
    dates = pd.bdate_range("2023-01-02", "2023-12-29")
    market = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "instrument_id": f"CN_EQ:{symbol}",
                "trade_date": date,
                "list_date": pd.Timestamp("1991-04-03"),
            }
            for date in dates
            for symbol in ("000001.SZ", "600000.SH")
        ]
    )
    output = artifact / "tradability.parquet"
    market.to_parquet(output, index=False)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    (artifact / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_id": "p05-actions-test",
                "quality": {"promotion_passed": True},
                "output": {"sha256": digest},
            }
        ),
        encoding="utf-8",
    )
    return artifact


def test_full_universe_action_runner_resumes_and_proves_empty_queries(tmp_path):
    p05 = _p05(tmp_path)
    symbols, identity = frozen_symbols_from_tradability(p05)
    provider = _Provider()
    runner = CorporateActionIngestionRunner(
        provider,
        ParquetLake(tmp_path / "lake"),
        tmp_path / "artifacts",
        tmp_path / "state",
    )
    config = CorporateActionBackfillConfig(
        start_date="2022-01-01",
        end_date="2023-12-31",
        symbols=symbols,
        universe_artifact_id=identity["artifact_id"],
        universe_manifest_sha256=identity["manifest_sha256"],
        requests_per_minute=100_000,
        workers=1,
    )
    first = runner.run(config)
    calls = list(provider.calls)
    second = runner.run(config)
    assert provider.calls == calls
    assert len(calls) == 2
    paths, queried, run_identity = load_completed_corporate_action_run(first)
    assert queried == symbols
    assert len(paths) == 2
    assert run_identity["query_symbols"] == 2
    second_summary = json.loads((second / "summary.json").read_text())
    assert second_summary["skipped_from_checkpoint"] == 2

    artifact = build_corporate_action_artifact_from_ingestion_run(
        first, p05, tmp_path / "curated"
    )
    manifest = json.loads((artifact / "manifest.json").read_text())
    assert manifest["quality"]["promotion_passed"]
    assert manifest["quality"]["query_coverage"] == 1.0
    assert manifest["quality"]["empty_raw_snapshot_files"] == 1
    assert manifest["guardrails"]["full_universe_query_coverage_proven"]
