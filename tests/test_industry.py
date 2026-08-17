import json
from pathlib import Path

import pandas as pd

from qrp.data.industry import (
    _assign_membership_ids,
    _bridge_short_membership_gaps,
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
                    "source_membership_end": [
                        pd.Timestamp("2021-12-10") if taxonomy == "SW2014" else pd.NaT
                    ],
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


def _write_instrument_lifecycle(lake: ParquetLake, symbols: list[str]) -> None:
    lake.write(
        FetchResult(
            dataset="instruments",
            provider="tushare",
            frame=pd.DataFrame(
                {
                    "symbol": symbols,
                    "source_symbol": [symbol.split(".")[0] for symbol in symbols],
                    "name": ["test"] * len(symbols),
                    "area": [""] * len(symbols),
                    "industry": [""] * len(symbols),
                    "market": ["main"] * len(symbols),
                    "exchange": [symbol.split(".")[1] for symbol in symbols],
                    "list_status": ["L"] * len(symbols),
                    "list_date": [pd.Timestamp("1991-01-01")] * len(symbols),
                    "delist_date": [pd.NaT] * len(symbols),
                    "instrument_kind": ["stock"] * len(symbols),
                    "source": ["tushare"] * len(symbols),
                    "ingested_at": [pd.Timestamp.now(tz="UTC")] * len(symbols),
                }
            ),
            query={"test": True},
        )
    )


def test_industry_artifact_switches_taxonomy_without_hindsight(tmp_path: Path):
    lake = ParquetLake(tmp_path / "lake")
    run = IndustryIngestionRunner(
        provider=FakeIndustryProvider(),
        lake=lake,
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    ).run(IndustryBackfillConfig(requests_per_minute=100000, retry_base_seconds=0))
    _write_instrument_lifecycle(lake, ["000001.SZ"])
    artifact = build_historical_industry_artifact(
        run,
        tmp_path / "lake",
        tmp_path / "curated",
        "2016-01-01",
        "2024-12-31",
    )
    membership = pd.read_parquet(artifact / "membership.parquet")
    assert len(membership) == 2
    assert set(membership["instrument_id"]) == {"CN_EQ:000001.SZ"}
    old = select_industry_as_of(membership, "2021-12-10 15:00:00+08:00")
    new = select_industry_as_of(membership, "2021-12-13 15:00:00+08:00")
    assert old.iloc[0]["taxonomy"] == "SW2014"
    assert new.iloc[0]["taxonomy"] == "SW2021"
    assert membership.loc[membership["taxonomy"] == "SW2014", "membership_end"].iloc[
        0
    ] == pd.Timestamp("2021-12-12")
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["quality"]["promotion_passed"]
    assert manifest["guardrails"]["taxonomy_revision_not_backfilled"]
    assert manifest["guardrails"]["stable_instrument_identity_mapping"]
    assert manifest["quality"]["hard_failures"]["invalid_instrument_namespace_rows"] == 0
    assert manifest["quality"]["interval_bridge"]["bridged_interval_rows"] == 1
    assert manifest["quality"]["interval_bridge"]["interval_gap_rows"] == 0


def test_industry_artifact_reuses_bse_security_code_mapping(tmp_path: Path):
    class FakeBSEIndustryProvider(FakeIndustryProvider):
        def fetch_industry_members(self, *args, **kwargs):
            result = super().fetch_industry_members(*args, **kwargs)
            result.frame["symbol"] = "873690.BJ"
            return result

    lake = ParquetLake(tmp_path / "lake")
    run = IndustryIngestionRunner(
        provider=FakeBSEIndustryProvider(),
        lake=lake,
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    ).run(IndustryBackfillConfig(requests_per_minute=100000, retry_base_seconds=0))
    _write_instrument_lifecycle(lake, ["920690.BJ"])
    mappings = tmp_path / "bse_mappings.parquet"
    pd.DataFrame(
        {
            "historical_symbol": ["873690.BJ"],
            "current_symbol": ["920690.BJ"],
        }
    ).to_parquet(mappings, index=False)
    artifact = build_historical_industry_artifact(
        run,
        tmp_path / "lake",
        tmp_path / "curated",
        "2016-01-01",
        "2024-12-31",
        security_code_mappings_path=mappings,
    )
    membership = pd.read_parquet(artifact / "membership.parquet")
    assert set(membership["instrument_id"]) == {"CN_EQ:BSE:920690.BJ"}
    assert membership["identity_alias_resolved"].all()
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    assert "security_code_mappings" in manifest["identity"]["aliases"]


def test_industry_gap_bridge_requires_short_gap_and_active_listing():
    rows = []
    for instrument_id, previous_end, next_start in (
        ("CN_EQ:SHORT.SZ", "2021-12-09", "2021-12-13"),
        ("CN_EQ:LONG.SZ", "2021-11-30", "2021-12-13"),
        ("CN_EQ:DELISTED.SZ", "2021-12-09", "2021-12-13"),
    ):
        rows.extend(
            [
                {
                    "instrument_id": instrument_id,
                    "taxonomy": "SW2014",
                    "industry_code": "OLD",
                    "membership_start": pd.Timestamp("2020-01-01"),
                    "membership_end": pd.Timestamp(previous_end),
                },
                {
                    "instrument_id": instrument_id,
                    "taxonomy": "SW2021",
                    "industry_code": "NEW",
                    "membership_start": pd.Timestamp(next_start),
                    "membership_end": pd.Timestamp("2022-12-31"),
                },
            ]
        )
    membership = _assign_membership_ids(pd.DataFrame(rows))
    lifecycle = pd.DataFrame(
        {
            "instrument_id": [
                "CN_EQ:SHORT.SZ",
                "CN_EQ:LONG.SZ",
                "CN_EQ:DELISTED.SZ",
            ],
            "listed_from": [pd.Timestamp("2010-01-01")] * 3,
            "listed_through": [pd.NaT, pd.NaT, pd.Timestamp("2021-12-09")],
        }
    )
    bridged, diagnostics = _bridge_short_membership_gaps(
        membership,
        lifecycle,
        maximum_gap_calendar_days=7,
    )
    old_end = bridged.loc[bridged["taxonomy"].eq("SW2014")].set_index("instrument_id")[
        "membership_end"
    ]
    assert old_end["CN_EQ:SHORT.SZ"] == pd.Timestamp("2021-12-12")
    assert old_end["CN_EQ:LONG.SZ"] == pd.Timestamp("2021-11-30")
    assert old_end["CN_EQ:DELISTED.SZ"] == pd.Timestamp("2021-12-09")
    assert diagnostics["bridged_interval_rows"] == 1
    assert diagnostics["bridged_calendar_days"] == 3
    assert diagnostics["long_interval_gap_rows_not_bridgeable"] == 1
    assert diagnostics["interval_gap_rows"] == 0


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
