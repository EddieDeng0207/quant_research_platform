import hashlib
import json
from pathlib import Path

import pandas as pd

from qrp.data.industry_coverage import build_industry_coverage_audit


def _artifact(root: Path, name: str, frame: pd.DataFrame, quality=True) -> Path:
    root.mkdir()
    output = root / f"{name}.parquet"
    frame.to_parquet(output, index=False)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_id": root.name,
                "quality": {"promotion_passed": quality},
                "outputs": {
                    name: {"path": output.name, "sha256": digest, "rows": len(frame)}
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def test_daily_industry_coverage_uses_pit_availability_and_effective_intervals(
    tmp_path: Path,
):
    fundamental = _artifact(
        tmp_path / "fundamental",
        "version_index",
        pd.DataFrame(
            {
                "instrument_id": ["CN_EQ:000001.SZ", "CN_EQ:000002.SZ"],
                "available_at": [
                    "2024-01-02 01:00:00+00:00",
                    "2024-01-04 01:00:00+00:00",
                ],
            }
        ),
    )
    industry = _artifact(
        tmp_path / "industry",
        "membership",
        pd.DataFrame(
            {
                "instrument_id": ["CN_EQ:000001.SZ"],
                "membership_start": ["2020-01-01"],
                "membership_end": ["2024-12-31"],
            }
        ),
    )
    calendar = tmp_path / "calendar.parquet"
    pd.DataFrame(
        {
            "calendar_date": pd.to_datetime(["2024-01-02", "2024-01-04"]),
            "is_open": [True, True],
        }
    ).to_parquet(calendar, index=False)
    output = build_industry_coverage_audit(
        fundamental,
        industry,
        calendar,
        tmp_path / "audits",
        "2024-01-02",
        "2024-01-04",
        require_clean_git=False,
    )
    daily = pd.read_parquet(output / "daily_coverage.parquet")
    assert daily["fundamental_to_industry_coverage"].tolist() == [1.0, 0.5]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["summary"]["sessions_below_minimum_coverage"] == 1
    assert not manifest["summary"]["conservative_stress_test_passed"]
    assert manifest["quality"]["promotion_passed"]
