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
    (fundamental / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_id": "f1",
                "schema_version": "p08_fundamental_pit_v1",
                "identity": {"research_as_of_at": "2026-08-15T00:00:00Z"},
                "quality": {
                    "promotion_passed": True,
                    "symbols": 1,
                    "statements": {
                        "income": {},
                        "balance_sheet": {},
                        "cashflow": {},
                        "financial_indicators": {},
                    },
                },
                "outputs": {
                    "version_index": {
                        "path": output.name,
                        "sha256": digest,
                    }
                },
            }
        ),
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
