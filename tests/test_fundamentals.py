import json
import warnings

import pandas as pd
import pytest

from qrp.data.fundamental_ingestion import (
    FundamentalBackfillConfig,
    FundamentalIngestionRunner,
)
from qrp.data.fundamentals import (
    FundamentalPITError,
    build_fundamental_pit_artifact,
    select_fundamentals_as_of,
)
from qrp.data.providers.base import FetchResult
from qrp.data.storage import ParquetLake


class _VersionedProvider:
    def fetch_fundamentals(self, statement, symbol, start_date, end_date):
        frame = pd.DataFrame(
            {
                "symbol": [symbol, symbol],
                "statement_type": [statement, statement],
                "report_period": pd.to_datetime(["2023-12-31", "2023-12-31"]),
                "announcement_date": pd.to_datetime(["2024-04-20", "2024-05-01"]),
                "actual_announcement_date": pd.to_datetime(["2024-04-20", "2024-05-01"]),
                "available_date": pd.to_datetime(["2024-04-20", "2024-05-01"]),
                "report_type": ["1", "1"],
                "comp_type": ["1", "1"],
                "end_type": ["4", "4"],
                "update_flag": ["0", "1"],
                "total_hldr_eqy_exc_min_int": [100.0, 120.0],
                "source_row_sha256": ["a" * 64, "b" * 64],
                "source_row_occurrence": [0, 0],
                "source": ["fake", "fake"],
                "ingested_at": [
                    pd.Timestamp("2026-08-15", tz="UTC"),
                    pd.Timestamp("2026-08-15", tz="UTC"),
                ],
            }
        )
        return FetchResult(
            dataset=f"fundamentals_{statement}",
            provider="fake",
            frame=frame,
            query={"start_date": start_date, "end_date": end_date},
            partition_values={"symbol": symbol},
        ).validate()

    def fetch_fundamentals_by_period(self, statement, period):
        result = self.fetch_fundamentals(statement, "000001.SZ", period, period)
        result.partition_values = {"report_period": period}
        return result


def _calendar(path):
    frame = pd.DataFrame(
        {
            "calendar_date": pd.to_datetime(
                ["2024-04-19", "2024-04-22", "2024-05-01", "2024-05-02"]
            ),
            "is_open": [True, True, False, True],
        }
    )
    frame.to_parquet(path, index=False)


def test_pit_artifact_delays_date_only_disclosures_and_selects_historical_revision(tmp_path):
    lake = ParquetLake(tmp_path / "lake")
    run = FundamentalIngestionRunner(
        provider=_VersionedProvider(),
        lake=lake,
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    ).run(
        FundamentalBackfillConfig(
            start_date="2024-01-01",
            end_date="2024-12-31",
            statements=("balance_sheet",),
            symbols=("000001.SZ",),
        )
    )
    calendar = tmp_path / "calendar.parquet"
    _calendar(calendar)
    artifact = build_fundamental_pit_artifact(
        run,
        tmp_path / "lake",
        calendar,
        tmp_path / "curated",
    )
    repeated = build_fundamental_pit_artifact(
        run,
        tmp_path / "lake",
        calendar,
        tmp_path / "curated",
    )
    assert artifact == repeated
    frame = pd.read_parquet(artifact / "balance_sheet.parquet")
    assert list(frame["available_at"].dt.tz_convert("Asia/Shanghai").dt.date) == [
        pd.Timestamp("2024-04-22").date(),
        pd.Timestamp("2024-05-02").date(),
    ]
    first = select_fundamentals_as_of(
        frame,
        "2024-04-25T07:00:00Z",
        "2026-08-15T23:59:00Z",
    )
    second = select_fundamentals_as_of(
        frame,
        "2024-05-03T07:00:00Z",
        "2026-08-15T23:59:00Z",
    )
    assert first.iloc[0]["total_hldr_eqy_exc_min_int"] == 100.0
    assert second.iloc[0]["total_hldr_eqy_exc_min_int"] == 120.0
    assert first.iloc[0]["source_ingested_at"] > first.iloc[0]["decision_at"]
    manifest = json.loads((artifact / "manifest.json").read_text())
    assert manifest["quality"]["promotion_passed"]
    assert manifest["guardrails"]["report_period_is_not_knowledge_time"]


def test_pit_artifact_accepts_full_market_report_period_grid(tmp_path):
    lake = ParquetLake(tmp_path / "lake")
    run = FundamentalIngestionRunner(
        provider=_VersionedProvider(),
        lake=lake,
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    ).run(
        FundamentalBackfillConfig(
            start_date="2023-12-31",
            end_date="2023-12-31",
            statements=("balance_sheet",),
            period_mode=True,
            requests_per_minute=100000,
        )
    )
    calendar = tmp_path / "calendar.parquet"
    _calendar(calendar)
    artifact = build_fundamental_pit_artifact(
        run, tmp_path / "lake", calendar, tmp_path / "curated"
    )
    manifest = json.loads((artifact / "manifest.json").read_text())
    assert manifest["quality"]["request_axis"] == "report_period"
    assert manifest["quality"]["symbols"] == 1


def test_as_of_selection_rejects_ambiguous_same_timestamp_versions():
    frame = pd.DataFrame(
        {
            "instrument_id": ["x", "x"],
            "report_period": pd.to_datetime(["2023-12-31", "2023-12-31"]),
            "available_at": pd.to_datetime(
                ["2024-04-22T01:30:00Z", "2024-04-22T01:30:00Z"], utc=True
            ),
            "source_ingested_at": pd.to_datetime(
                ["2026-08-15T00:00:00Z", "2026-08-15T00:00:00Z"], utc=True
            ),
            "version_id": ["a", "b"],
            "source_row_sha256": ["a" * 64, "b" * 64],
            "source_row_occurrence": [0, 0],
            "report_type": ["1", "1"],
            "update_flag": ["1", "1"],
        }
    )
    with pytest.raises(FundamentalPITError, match="ambiguous"):
        select_fundamentals_as_of(
            frame,
            "2024-04-23T00:00:00Z",
            "2026-08-16T00:00:00Z",
        )


def test_pit_builder_does_not_depend_on_deprecated_all_null_concat(tmp_path):
    lake = ParquetLake(tmp_path / "lake")
    run = FundamentalIngestionRunner(
        provider=_VersionedProvider(),
        lake=lake,
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    ).run(
        FundamentalBackfillConfig(
            start_date="2024-01-01",
            end_date="2024-12-31",
            statements=("balance_sheet",),
            symbols=("000001.SZ", "600000.SH"),
        )
    )
    calendar = tmp_path / "calendar.parquet"
    _calendar(calendar)
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        warnings.simplefilter("error", pd.errors.PerformanceWarning)
        build_fundamental_pit_artifact(
            run,
            tmp_path / "lake",
            calendar,
            tmp_path / "curated",
        )
