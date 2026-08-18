import json
from dataclasses import replace
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
            raw_factor = residual + 1.5 * (security % 3) + 0.8 * np.log(market_cap / 1e9)
            observations.append(
                {
                    "instrument_id": instrument_id,
                    "factor_value": raw_factor,
                    "industry_code": industry,
                    "market_cap": market_cap,
                    "research_eligible": True,
                    "announcement_at": decision - pd.Timedelta(days=4),
                    "available_at": decision - pd.Timedelta(days=3),
                    "ingested_at": decision + pd.Timedelta(days=30),
                    "research_as_of_at": pd.Timestamp("2026-08-15", tz="UTC"),
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
    observation_frame = pd.DataFrame(observations)
    label_frame = pd.DataFrame(labels)
    label_frame["outcome_observation_end_at"] = label_frame["label_end_at"].max()
    return observation_frame, label_frame


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
    sanity = result.factor_exposures.loc[
        result.factor_exposures["scope"] == "full_cross_section_sanity"
    ]
    assert sanity["exposure_value"].abs().max() < 1e-8
    target_sums = result.target_weights.groupby("decision_at")["target_weight"].sum()
    assert np.allclose(target_sums.to_numpy(), 0.98)
    assert len(result.distribution) == 30
    nw_lags = result.horizon_summary.set_index("horizon_sessions")["newey_west_lags"]
    assert nw_lags.to_dict() == {5: 1, 20: 4}
    assert result.quality["decision_frequency"]["inferred_periods_per_year"] == pytest.approx(
        52.1775, rel=1e-4
    )
    assert result.turnover.iloc[0]["one_way_turnover"] == pytest.approx(0.98)
    assert {
        "raw_rank_ic",
        "residualized_rank_ic",
        "raw_top_minus_bottom_return",
        "residualized_top_minus_bottom_return",
    }.issubset(result.ic_series.columns)


def test_missing_factor_rows_may_lack_event_time_but_finite_rows_may_not():
    observations, labels = _factor_inputs()
    missing_index = observations.index[0]
    observations.loc[missing_index, "factor_value"] = np.nan
    observations.loc[missing_index, ["announcement_at", "available_at", "ingested_at"]] = pd.NaT

    result = evaluate_single_factor(observations, labels, _spec())
    assert result.quality["promotion_passed"]

    observations.loc[missing_index, "factor_value"] = 1.0
    with pytest.raises(FutureDataError, match="timing contract failed"):
        evaluate_single_factor(observations, labels, _spec())


def test_declared_scope_has_separate_retention_and_factor_coverage_gates():
    observations, labels = _factor_inputs()
    security_number = observations["instrument_id"].str.removeprefix("CN").astype(int)
    observations["evaluation_eligible"] = security_number % 10 < 8
    observations["universe_in_scope"] = observations["evaluation_eligible"]
    observations["factor_applicable"] = True
    observations["scope_exclusion_reason"] = observations["evaluation_eligible"].map(
        {True: "", False: "market_segment_out_of_scope"}
    )
    observations["research_universe_sha256"] = "frozen-universe"
    observations["research_universe_minimum_retention"] = 0.80
    spec = replace(
        _spec(),
        eligibility_column="evaluation_eligible",
        minimum_scope_retention=0.80,
    )

    result = evaluate_single_factor(observations, labels, spec)

    assert result.quality["promotion_passed"]
    assert result.quality["median_scope_retention"] == pytest.approx(0.80)
    assert result.coverage["base_eligible_rows"].eq(30).all()
    assert result.coverage["eligible_rows"].eq(24).all()
    assert result.quality["hard_failures"]["scope_retention_below_threshold_dates"] == 0

    observations.loc[security_number % 10 == 7, "evaluation_eligible"] = False
    failed = evaluate_single_factor(observations, labels, spec)
    assert not failed.quality["promotion_passed"]
    assert failed.quality["hard_failures"]["scope_retention_below_threshold_dates"] == 30


def test_quantile_portfolio_exposure_gate_catches_tail_concentration():
    observations, labels = _factor_inputs(periods=3, securities=240)
    security_number = observations["instrument_id"].str.removeprefix("CN").astype(int)
    industry_number = security_number % 6
    small = (security_number % 7) < 2
    within_industry = (security_number // 6) % 40 - 19.5
    scale = np.where(industry_number == 5, 50.0, 1.0) * np.where(small, 20.0, 1.0)
    observations["industry_code"] = "I" + industry_number.astype(str)
    observations["market_cap"] = np.where(small, 3e8, 3e9).astype(float)
    observations["factor_value"] = within_industry * scale
    spec = FactorEvaluationSpec(
        factor_name="heteroskedastic_tail",
        factor_family="fundamental",
        min_cross_section=200,
        min_ic_observations=100,
        min_evaluation_periods=2,
    )

    result = evaluate_single_factor(observations, labels, spec)

    assert not result.quality["promotion_passed"]
    failures = result.quality["hard_failures"]
    assert failures["top_bottom_industry_active_weight_breach_rows"] > 0
    assert failures["top_bottom_log_market_cap_z_breach_rows"] > 0
    assert failures["neutralization_first_order_industry_sanity_breach_rows"] == 0
    assert failures["neutralization_first_order_size_sanity_breach_rows"] == 0

    stratified = evaluate_single_factor(
        observations,
        labels,
        replace(
            spec,
            quantile_assignment="industry_size_stratified",
            size_strata=5,
        ),
    )
    assert stratified.quality["promotion_passed"]
    assert stratified.quality["hard_failures"]["top_bottom_industry_active_weight_breach_rows"] == 0
    assert stratified.quality["hard_failures"]["top_bottom_log_market_cap_z_breach_rows"] == 0
    assert stratified.factor_panel["size_stratum"].between(1, 5).all()
    assert stratified.factor_panel["quantile_stratum"].str.contains(r"\|S").all()


def test_noise_aware_exposure_threshold_accepts_small_universe_random_factor():
    observations, labels = _factor_inputs(periods=10, securities=250)
    security_number = observations["instrument_id"].str.removeprefix("CN").astype(int)
    observations["industry_code"] = "I" + (security_number % 8).astype(str)
    observations["factor_value"] = np.random.default_rng(20260815).normal(size=len(observations))
    spec = FactorEvaluationSpec(
        factor_name="independent_random",
        factor_family="fundamental",
        min_cross_section=200,
        min_ic_observations=100,
        min_evaluation_periods=2,
    )

    result = evaluate_single_factor(observations, labels, spec)

    assert result.quality["hard_failures"]["top_bottom_industry_active_weight_breach_rows"] == 0
    assert result.quality["hard_failures"]["top_bottom_log_market_cap_z_breach_rows"] == 0
    portfolio_exposures = result.factor_exposures.loc[
        result.factor_exposures["scope"] == "quantile_portfolio"
    ]
    assert (
        portfolio_exposures["exposure_limit"]
        >= np.where(
            portfolio_exposures["exposure_type"] == "industry_active_weight",
            spec.industry_active_weight_floor,
            spec.log_market_cap_z_floor,
        )
    ).all()


def test_return_residualization_failure_falls_back_to_raw_with_diagnostics():
    observations, labels = _factor_inputs(periods=3, securities=200)
    security_number = observations["instrument_id"].str.removeprefix("CN").astype(int)
    observations["industry_code"] = "I" + (security_number % 4).astype(str)
    observations["market_cap"] = np.where(
        security_number < 100,
        1e9,
        1e9 + security_number * 1e7,
    )
    observations["factor_value"] = np.random.default_rng(17).normal(size=len(observations))
    missing = labels["instrument_id"].str.removeprefix("CN").astype(int) >= 100
    labels.loc[missing, "forward_return"] = np.nan
    spec = FactorEvaluationSpec(
        factor_name="residualization_fallback",
        factor_family="fundamental",
        min_cross_section=200,
        min_ic_observations=100,
        min_evaluation_periods=2,
        minimum_label_match_rate=0.40,
        return_basis="residualized",
    )

    result = evaluate_single_factor(observations, labels, spec)

    assert (result.ic_series["return_basis"] == "raw_fallback").all()
    assert result.ic_series["residualization_error"].str.contains("constant|rank deficient").all()
    assert result.quality["residualization_failure_periods"] == len(result.ic_series)
    assert result.quality["hard_failures"]["requested_residualized_return_failure_periods"] == len(
        result.ic_series
    )
    assert not result.quality["promotion_passed"]


def test_label_coverage_excludes_structural_tail_by_horizon():
    observations, labels = _factor_inputs()
    horizon20_ends = sorted(labels.loc[labels["horizon_sessions"] == 20, "label_end_at"].unique())
    sample_end = horizon20_ends[-4]
    labels["outcome_observation_end_at"] = sample_end
    structural_tail = labels["label_end_at"] > sample_end
    labels.loc[structural_tail, "forward_return"] = np.nan

    result = evaluate_single_factor(observations, labels, _spec())

    horizon20 = result.label_coverage.set_index("horizon_sessions").loc[20]
    assert horizon20["structural_tail_rows"] == 90
    assert horizon20["unexpected_missing_rows"] == 0
    assert horizon20["match_rate"] == 1.0
    assert result.quality["promotion_passed"]

    internally_missing = labels.drop(labels.index[0]).copy()
    strict_spec = replace(_spec(), minimum_label_match_rate=0.9999)
    missing_result = evaluate_single_factor(observations, internally_missing, strict_spec)
    assert missing_result.quality["unexpected_missing_label_rows"] == 1
    assert missing_result.quality["hard_failures"]["horizon_label_match_rate_below_threshold"] == 1


def test_frequency_mismatch_and_single_member_industry_fail_closed():
    observations, labels = _factor_inputs()
    with pytest.raises(FactorEvaluationError, match="annualization_periods disagrees"):
        evaluate_single_factor(
            observations,
            labels,
            replace(_spec(), annualization_periods=12),
        )

    for decision_at in observations["decision_at"].unique():
        first = observations.index[observations["decision_at"] == decision_at][0]
        observations.loc[first, "industry_code"] = "SINGLE_MEMBER"
    with pytest.raises(FactorEvaluationError, match="no cross-section passed"):
        evaluate_single_factor(
            observations,
            labels,
            replace(_spec(), min_industry_members=2),
        )


def test_institutional_defaults_and_declared_outcome_cutoff_are_enforced():
    defaults = FactorEvaluationSpec(factor_name="default_profile", factor_family="fundamental")
    assert defaults.min_cross_section == 200
    assert defaults.min_ic_observations == 100
    assert defaults.min_evaluation_periods == 104
    assert defaults.min_industry_members == 5
    assert defaults.quantile_assignment == "global"
    with pytest.raises(ValueError, match="unsupported quantile_assignment"):
        replace(defaults, quantile_assignment="unfrozen_method").validate()

    observations, labels = _factor_inputs(periods=2)
    labels["outcome_observation_end_at"] = labels["label_end_at"].min()
    with pytest.raises(FactorEvaluationError, match="beyond outcome_observation_end_at"):
        evaluate_single_factor(
            observations,
            labels,
            replace(_spec(), min_evaluation_periods=2),
        )


def test_factor_timing_and_outcome_direction_fail_closed():
    observations, labels = _factor_inputs(periods=2)
    observations.loc[0, "available_at"] = observations.loc[0, "decision_at"] + pd.Timedelta(hours=1)
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
    labels.loc[0, "label_start_at"] = labels.loc[0, "execution_at"] - pd.Timedelta(minutes=1)
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
    assert manifest["schema_version"] == "p07_single_factor_evaluation_v6"
    assert manifest["quality"]["promotion_passed"]
    assert "label_coverage" in manifest["outputs"]
    report = generate_factor_evaluation_report(first, tmp_path / "report.md")
    assert "门禁通过不等于因子具有投资价值" in report.read_text(encoding="utf-8")

    (first / "ic_series.parquet").write_bytes(b"tampered")
    with pytest.raises(FactorEvaluationError, match="hash mismatch"):
        generate_factor_evaluation_report(first, tmp_path / "report-2.md")
