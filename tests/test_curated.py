import json

import pandas as pd
import pytest

from qrp.data.curated import build_curated_price_artifact
from qrp.data.providers.base import FetchResult
from qrp.data.storage import ParquetLake


def _result(dataset, trade_date, frame):
    return FetchResult(
        dataset=dataset,
        provider="tushare",
        frame=frame,
        query={"trade_date": trade_date},
        partition_values={"trade_date": trade_date},
    )


def _write_day(lake, trade_date, include_factor=True):
    common = {
        "symbol": ["000001.SZ"],
        "trade_date": [pd.Timestamp(trade_date)],
        "source": ["tushare"],
        "ingested_at": [pd.Timestamp("2024-02-01", tz="UTC")],
    }
    bars = pd.DataFrame(
        {
            **common,
            "open": [10.0],
            "high": [10.2],
            "low": [9.8],
            "close": [10.1],
            "volume": [1000.0],
            "amount": [10100.0],
            "adjustment": ["raw"],
        }
    )
    lake.write(_result("daily_bars", trade_date, bars))
    if include_factor:
        factors = pd.DataFrame({**common, "adj_factor": [1.0]})
        lake.write(_result("adjustment_factors", trade_date, factors))


def test_curated_artifact_is_content_addressed_and_repeatable(tmp_path):
    lake = ParquetLake(tmp_path / "lake")
    _write_day(lake, "2024-01-02")
    _write_day(lake, "2024-01-03")
    first = build_curated_price_artifact(
        tmp_path / "lake", tmp_path / "curated", "2024-01-02", "2024-01-03"
    )
    first_manifest = (first / "manifest.json").read_text(encoding="utf-8")
    second = build_curated_price_artifact(
        tmp_path / "lake", tmp_path / "curated", "2024-01-02", "2024-01-03"
    )
    assert first == second
    assert (second / "manifest.json").read_text(encoding="utf-8") == first_manifest
    manifest = json.loads(first_manifest)
    assert manifest["guardrails"]["future_qfq_anchor_allowed"] is False
    assert manifest["quality"]["factor_missing"] == 0


def test_curated_artifact_rejects_mismatched_daily_partition_coverage(tmp_path):
    lake = ParquetLake(tmp_path / "lake")
    _write_day(lake, "2024-01-02")
    _write_day(lake, "2024-01-03", include_factor=False)
    with pytest.raises(ValueError, match="partition coverage differs"):
        build_curated_price_artifact(
            tmp_path / "lake", tmp_path / "curated", "2024-01-02", "2024-01-03"
        )
