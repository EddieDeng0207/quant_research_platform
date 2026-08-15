import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qrp.data.temporal import FutureDataError
from qrp.research.factor_artifact import (
    build_factor_evaluation_artifact,
    generate_factor_evaluation_report,
)
from qrp.research.factor_evaluation import (
    FactorEvaluationError,
    FactorEvaluationSpec,
    evaluate_single_factor,
)


def _factor_inputs(periods: int = 30, securities: int = 30):
    observations = []
    labels = []
    start = pd.Timestamp("2022-01-03", tz="UTC")
    for period in range(periods):
        decision = start + pd.Timedelta(days=7 * period, hours=8)
        execution = decision + pd.Timedelta(days=1)
        for security in range(securities):
            instrument_id = f"CN{security:04d}"
            industry = f"I{security % 3}"
            market_cap = float(1e9 * (1.0 + security / 10.0))
            residual = (security % 10 - 4.5) + 0.07 * period
            raw_factor = (
                residual
                + 1.5 * (security % 3)
                + 0.8 * np.log(market_cap / 1e9)
            )
            observations.append(
                {
                    "instrument_id": instrument_id,
                    "factor_value": raw_factor,
                    "industry_code": industry,
                    "market_cap": market_cap,
                    "research_eligible": True,
                    "announcement_at": decision - pd.Timedelta(days=4),
                    "available_at": decision - pd.Timedelta(days=3),
                    "ingested_at": decision - pd.Timedelta(days=2),
                    "decision_at": decision,
                    "execution_at": execution,
                }
            )
            for horizon in (5, 20):
                noise = ((security * 7 + period * 3 + horizon) % 11 - 5) * 0.0002
                labels.append(
                    {
                        "instrument_id": instrument_id,
                        "execution_at": execution,
                        "horizon_sessions": horizon,
                        "label_start_at": execution,
                        "label_end_at": execution + pd.Timedelta(days=horizon + 2),
                        "forward_return": 0.0025 * residual + noise,
                    }
                )
    return pd.DataFrame(observations), pd.DataFrame(labels)


def _spec() -> FactorEvaluationSpec:
    return FactorEvaluationSpec(
        factor_name="synthetic_value",
        factor_family="fundamental",
        min_cross_section=20,
        min_ic_observations=20,
        min_evaluation_periods=26,
    )


def test_factor_evaluation_is_pit_neutralized_and_promotable():
    observations, labels = _factor_inputs()
    result = evaluate_single_factor(observations, labels, _spec())

    assert result.quality["promotion_passed"]
    assert result.quality["label_match_rate"] == 1.0
    assert len(result.ic_series) == 60
    assert (result.horizon_summary["mean_rank_ic"] > 0.9).all()
    assert (result.horizon_summary["mean_top_minus_bottom_return"] > 0).all()
    assert result.factor_exposures["exposure_value"].abs().max() < 1e-8
    target_sums = result.target_weights.groupby("decision_at")["target_weight"].sum()
    assert np.allclose(target_sums.to_numpy(), 0.98)
    assert len(result.distribution) == 30


def test_factor_timing_and_outcome_direction_fail_closed():
    observations, labels = _factor_inputs(periods=2)
    observations.loc[0, "available_at"] = observations.loc[0, "decision_at"] + pd.Timedelta(
        hours=1
    )
    with pytest.raises(FutureDataError):
        evaluate_single_factor(
            observations,
            labels,
            FactorEvaluationSpec(
                factor_name="bad_timing",
                factor_family="fundamental",
                min_cross_section=20,
                min_ic_observations=20,
                min_evaluation_periods=2,
            ),
        )

    observations, labels = _factor_inputs(periods=2)
    labels.loc[0, "label_start_at"] = labels.loc[0, "execution_at"] - pd.Timedelta(
        minutes=1
    )
    with pytest.raises(FactorEvaluationError, match="starts before"):
        evaluate_single_factor(
            observations,
            labels,
            FactorEvaluationSpec(
                factor_name="bad_label",
                factor_family="fundamental",
                min_cross_section=20,
                min_ic_observations=20,
                min_evaluation_periods=2,
            ),
        )


def test_factor_artifact_is_deterministic_and_report_verifies_hashes(tmp_path: Path):
    observations, labels = _factor_inputs()
    observations_path = tmp_path / "observations.parquet"
    labels_path = tmp_path / "labels.parquet"
    observations.to_parquet(observations_path, index=False)
    labels.to_parquet(labels_path, index=False)

    first = build_factor_evaluation_artifact(
        observations_path,
        labels_path,
        tmp_path / "curated",
        spec=_spec(),
    )
    second = build_factor_evaluation_artifact(
        observations_path,
        labels_path,
        tmp_path / "curated",
        spec=_spec(),
    )
    assert first == second
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["quality"]["promotion_passed"]
    report = generate_factor_evaluation_report(first, tmp_path / "report.md")
    assert "门禁通过不等于因子具有投资价值" in report.read_text(encoding="utf-8")

    (first / "ic_series.parquet").write_bytes(b"tampered")
    with pytest.raises(FactorEvaluationError, match="hash mismatch"):
        generate_factor_evaluation_report(first, tmp_path / "report-2.md")
