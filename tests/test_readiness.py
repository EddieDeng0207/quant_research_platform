import hashlib
import json

import pandas as pd

from qrp.data.readiness import (
    DEFAULT_REQUIRED_DAILY_DATASETS,
    audit_research_readiness,
)


def test_research_readiness_requires_complete_daily_fundamental_and_industry_inputs(
    tmp_path,
):
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    calendar = tmp_path / "calendar.parquet"
    pd.DataFrame({"calendar_date": dates, "is_open": [True, True]}).to_parquet(
        calendar, index=False
    )
    lake = tmp_path / "lake"
    lake.mkdir()
    entries = []
    for dataset in DEFAULT_REQUIRED_DAILY_DATASETS:
        for date in dates:
            entries.append(
                {
                    "provider": "tushare",
                    "dataset": dataset,
                    "written_at": "2026-08-15T00:00:00Z",
                    "rows": 1,
                    "path": f"{dataset}/{date.date()}.parquet",
                    "partition_values": {"trade_date": str(date.date())},
                }
            )
    (lake / "manifest.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )

    fundamental = tmp_path / "fundamental"
    fundamental.mkdir()
    output = fundamental / "version_index.parquet"
    pd.DataFrame({"version_id": ["v1"]}).to_parquet(output, index=False)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    outputs = {
        "version_index": {
            "path": output.name,
            "sha256": digest,
            "rows": 1,
        }
    }
    statement_quality = {}
    for statement in (
        "income",
        "balance_sheet",
        "cashflow",
        "financial_indicators",
    ):
        statement_output = fundamental / f"{statement}.parquet"
        pd.DataFrame({"value": [1.0]}).to_parquet(statement_output, index=False)
        outputs[statement] = {
            "path": statement_output.name,
            "sha256": hashlib.sha256(statement_output.read_bytes()).hexdigest(),
            "rows": 1,
        }
        statement_quality[statement] = {
            "rows": 1,
            "report_period_start": "2020-03-31",
            "report_period_end": "2024-03-31",
            "available_at_start": "2020-04-30T01:30:00+00:00",
            "available_at_end": "2024-04-30T01:30:00+00:00",
        }
    fundamental_manifest = {
        "artifact_id": "f1",
        "schema_version": "p08_fundamental_pit_v1",
        "identity": {"research_as_of_at": "2026-08-15T00:00:00Z"},
        "quality": {
            "promotion_passed": True,
            "symbols": 1,
            "statements": statement_quality,
        },
        "outputs": outputs,
    }
    (fundamental / "manifest.json").write_text(
        json.dumps(fundamental_manifest),
        encoding="utf-8",
    )
    industry = tmp_path / "industry.parquet"
    pd.DataFrame(
        {
            "instrument_id": ["CN_EQ:000001.SZ"],
            "industry_code": ["I1"],
            "membership_start": [pd.Timestamp("2020-01-01")],
            "membership_end": [pd.NaT],
        }
    ).to_parquet(industry, index=False)

    report = audit_research_readiness(
        lake,
        calendar,
        "2024-01-02",
        "2024-01-03",
        fundamental_artifact=fundamental,
        industry_membership_path=industry,
        minimum_weekly_periods=1,
        minimum_fundamental_symbols=1,
        minimum_industry_instruments=1,
    )
    assert report.passed
    assert all(item["complete"] for item in report.datasets.values())

    stale_manifest = json.loads(json.dumps(fundamental_manifest))
    for quality in stale_manifest["quality"]["statements"].values():
        quality["report_period_end"] = "2016-12-31"
        quality["available_at_end"] = "2017-04-30T01:30:00+00:00"
    (fundamental / "manifest.json").write_text(
        json.dumps(stale_manifest), encoding="utf-8"
    )
    stale_report = audit_research_readiness(
        lake,
        calendar,
        "2024-01-02",
        "2024-01-03",
        fundamental_artifact=fundamental,
        industry_membership_path=industry,
        minimum_weekly_periods=1,
        minimum_fundamental_symbols=1,
        minimum_industry_instruments=1,
    )
    assert not stale_report.passed
    assert set(stale_report.fundamentals["statements_outside_requested_interval"]) == {
        "income",
        "balance_sheet",
        "cashflow",
        "financial_indicators",
    }

    missing_statement_manifest = json.loads(json.dumps(fundamental_manifest))
    del missing_statement_manifest["outputs"]["income"]
    (fundamental / "manifest.json").write_text(
        json.dumps(missing_statement_manifest), encoding="utf-8"
    )
    missing_statement_report = audit_research_readiness(
        lake,
        calendar,
        "2024-01-02",
        "2024-01-03",
        fundamental_artifact=fundamental,
        industry_membership_path=industry,
        minimum_weekly_periods=1,
        minimum_fundamental_symbols=1,
        minimum_industry_instruments=1,
    )
    assert not missing_statement_report.passed
    assert missing_statement_report.fundamentals["missing_statements"] == ["income"]

    (fundamental / "manifest.json").write_text(
        json.dumps(fundamental_manifest), encoding="utf-8"
    )

    invalid_namespace = tmp_path / "industry_invalid_namespace.parquet"
    pd.read_parquet(industry).assign(instrument_id="000001.SZ").to_parquet(
        invalid_namespace, index=False
    )
    namespace_report = audit_research_readiness(
        lake,
        calendar,
        "2024-01-02",
        "2024-01-03",
        fundamental_artifact=fundamental,
        industry_membership_path=invalid_namespace,
        minimum_weekly_periods=1,
        minimum_fundamental_symbols=1,
        minimum_industry_instruments=1,
    )
    assert not namespace_report.passed
    assert namespace_report.industry_membership["invalid_instrument_namespace_rows"] == 1

    missing_industry = audit_research_readiness(
        lake,
        calendar,
        "2024-01-02",
        "2024-01-03",
        fundamental_artifact=fundamental,
        minimum_weekly_periods=1,
        minimum_fundamental_symbols=1,
        minimum_industry_instruments=1,
    )
    assert not missing_industry.passed
    assert (
        missing_industry.hard_failures[
            "historical_industry_membership_missing_or_failed"
        ]
        == 1
    )

    empty_statement_manifest = json.loads(json.dumps(fundamental_manifest))
    empty_income = fundamental / "income.parquet"
    pd.DataFrame({"value": pd.Series(dtype="float64")}).to_parquet(
        empty_income, index=False
    )
    empty_statement_manifest["outputs"]["income"].update(
        {
            "sha256": hashlib.sha256(empty_income.read_bytes()).hexdigest(),
            "rows": 0,
        }
    )
    empty_statement_manifest["quality"]["statements"]["income"]["rows"] = 0
    (fundamental / "manifest.json").write_text(
        json.dumps(empty_statement_manifest), encoding="utf-8"
    )
    empty_statement_report = audit_research_readiness(
        lake,
        calendar,
        "2024-01-02",
        "2024-01-03",
        fundamental_artifact=fundamental,
        industry_membership_path=industry,
        minimum_weekly_periods=1,
        minimum_fundamental_symbols=1,
        minimum_industry_instruments=1,
    )
    assert not empty_statement_report.passed
    assert empty_statement_report.fundamentals["empty_statements"] == ["income"]
