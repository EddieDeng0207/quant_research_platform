import json
from pathlib import Path

import pandas as pd

from qrp.data.industry import (
    build_historical_industry_artifact,
    select_industry_as_of,
)
from qrp.data.industry_ingestion import IndustryBackfillConfig, IndustryIngestionRunner
from qrp.data.providers.base import FetchResult
from qrp.data.storage import ParquetLake


class FakeIndustryProvider:
    def fetch_industry_classification(self, taxonomy, industry_level):
        code = "OLD.SI" if taxonomy == "SW2014" else "NEW.SI"
        return FetchResult(
            dataset="industry_classification",
            provider="fake",
            frame=pd.DataFrame(
                {
                    "taxonomy": [taxonomy],
                    "industry_level": [industry_level],
                    "industry_code": ["OLD" if taxonomy == "SW2014" else "NEW"],
                    "industry_name": ["旧行业" if taxonomy == "SW2014" else "新行业"],
                    "source_index_code": [code],
                    "source": ["fake"],
                    "ingested_at": [pd.Timestamp.now(tz="UTC")],
                }
            ),
            query={"taxonomy": taxonomy},
            partition_values={"taxonomy": taxonomy},
        )

    def fetch_industry_members(
        self,
        taxonomy,
        source_index_code,
        industry_code,
        industry_name,
        industry_level,
    ):
        return FetchResult(
            dataset="industry_membership",
            provider="fake",
            frame=pd.DataFrame(
                {
                    "taxonomy": [taxonomy],
                    "industry_level": [industry_level],
                    "industry_code": [industry_code],
                    "industry_name": [industry_name],
                    "source_index_code": [source_index_code],
                    "symbol": ["000001.SZ"],
                    "source_membership_start": [pd.Timestamp("1991-04-03")],
                    "source_membership_end": [pd.NaT],
                    "is_current": ["Y"],
                    "source": ["fake"],
                    "ingested_at": [pd.Timestamp.now(tz="UTC")],
                }
            ),
            query={"source_index_code": source_index_code},
            partition_values={
                "taxonomy": taxonomy,
                "source_index_code": source_index_code,
            },
        )


def test_industry_artifact_switches_taxonomy_without_hindsight(tmp_path: Path):
    run = IndustryIngestionRunner(
        provider=FakeIndustryProvider(),
        lake=ParquetLake(tmp_path / "lake"),
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    ).run(IndustryBackfillConfig(requests_per_minute=100000, retry_base_seconds=0))
    artifact = build_historical_industry_artifact(
        run,
        tmp_path / "lake",
        tmp_path / "curated",
        "2016-01-01",
        "2024-12-31",
    )
    membership = pd.read_parquet(artifact / "membership.parquet")
    assert len(membership) == 2
    old = select_industry_as_of(membership, "2021-12-10 15:00:00+08:00")
    new = select_industry_as_of(membership, "2021-12-13 15:00:00+08:00")
    assert old.iloc[0]["taxonomy"] == "SW2014"
    assert new.iloc[0]["taxonomy"] == "SW2021"
    assert membership.loc[membership["taxonomy"] == "SW2014", "membership_end"].iloc[0] == pd.Timestamp("2021-12-12")
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["quality"]["promotion_passed"]
    assert manifest["guardrails"]["taxonomy_revision_not_backfilled"]


def test_industry_runner_resumes_from_category_checkpoints(tmp_path: Path):
    provider = FakeIndustryProvider()
    runner = IndustryIngestionRunner(
        provider=provider,
        lake=ParquetLake(tmp_path / "lake"),
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    )
    config = IndustryBackfillConfig(requests_per_minute=100000, retry_base_seconds=0)
    runner.run(config)
    repeated = runner.run(config)
    summary = json.loads((repeated / "summary.json").read_text(encoding="utf-8"))
    assert summary["files_written"] == 0
    assert summary["skipped_from_checkpoint"] == 2
