"""Point-in-time safe single-factor preparation and diagnostic evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, Sequence

import numpy as np
import pandas as pd

from .timing import validate_factor_timing


class FactorEvaluationError(RuntimeError):
    """Raised when a factor evaluation cannot prove its research contract."""


@dataclass(frozen=True)
class FactorEvaluationSpec:
    """Frozen policies for one cross-sectional factor evaluation."""

    factor_name: str
    factor_family: str
    expected_direction: int = 1
    quantiles: int = 5
    winsor_mad_multiplier: float = 5.0
    min_cross_section: int = 200
    min_ic_observations: int = 100
    min_evaluation_periods: int = 104
    min_industry_members: int = 5
    minimum_coverage: float = 0.80
    minimum_label_match_rate: float = 0.95
    neutralization_weighting: str = "sqrt_market_cap"
    industry_active_weight_floor: float = 0.05
    log_market_cap_z_floor: float = 0.25
    exposure_sampling_sigma_multiplier: float = 4.0
    target_gross_weight: float = 0.98
    annualization_periods: int = 52
    annualization_frequency_tolerance: float = 0.15
    minimum_newey_west_lags: int = 1
    return_basis: str = "raw"
    version: str = "p07_single_factor_evaluation_v3"

    def validate(self) -> "FactorEvaluationSpec":
        if not self.factor_name.strip():
            raise ValueError("factor_name is required")
        if self.expected_direction not in {-1, 1}:
            raise ValueError("expected_direction must be -1 or 1")
        if self.quantiles < 2:
            raise ValueError("quantiles must be at least 2")
        if self.winsor_mad_multiplier <= 0:
            raise ValueError("winsor_mad_multiplier must be positive")
        if min(
            self.min_cross_section,
            self.min_ic_observations,
            self.min_evaluation_periods,
            self.min_industry_members,
            self.annualization_periods,
        ) < 2:
            raise ValueError("sample and annualization requirements must be at least 2")
        if self.min_cross_section < self.min_ic_observations:
            raise ValueError("min_cross_section must be at least min_ic_observations")
        if not 0 < self.minimum_coverage <= 1:
            raise ValueError("minimum_coverage must be in (0, 1]")
        if not 0 < self.minimum_label_match_rate <= 1:
            raise ValueError("minimum_label_match_rate must be in (0, 1]")
        if self.neutralization_weighting not in {"equal", "sqrt_market_cap"}:
            raise ValueError("unsupported neutralization_weighting")
        if not 0 <= self.industry_active_weight_floor <= 1:
            raise ValueError("industry_active_weight_floor must be in [0, 1]")
        if self.log_market_cap_z_floor < 0:
            raise ValueError("log_market_cap_z_floor must be non-negative")
        if self.exposure_sampling_sigma_multiplier <= 0:
            raise ValueError("exposure_sampling_sigma_multiplier must be positive")
        if not 0 < self.target_gross_weight <= 1:
            raise ValueError("target_gross_weight must be in (0, 1]")
        if not 0 <= self.annualization_frequency_tolerance <= 1:
            raise ValueError("annualization_frequency_tolerance must be in [0, 1]")
        if self.minimum_newey_west_lags < 0:
            raise ValueError("minimum_newey_west_lags must be non-negative")
        if self.return_basis not in {"raw", "residualized"}:
            raise ValueError("return_basis must be raw or residualized")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(asdict(self))


@dataclass
class FactorEvaluationResult:
    factor_panel: pd.DataFrame
    coverage: pd.DataFrame
    distribution: pd.DataFrame
    factor_exposures: pd.DataFrame
    label_coverage: pd.DataFrame
    ic_series: pd.DataFrame
    quantile_returns: pd.DataFrame
    turnover: pd.DataFrame
    target_weights: pd.DataFrame
    horizon_summary: pd.DataFrame
    annual_summary: pd.DataFrame
    quality: Dict[str, Any]


def evaluate_single_factor(
    observations: pd.DataFrame,
    forward_returns: pd.DataFrame,
    spec: FactorEvaluationSpec,
) -> FactorEvaluationResult:
    """Prepare and evaluate a factor without allowing outcome labels into features."""
    frozen = spec.validate()
    factor_panel, coverage, exposures = _prepare_factor_panel(observations, frozen)
    distribution = _distribution_summary(factor_panel)
    labels = _prepare_forward_returns(forward_returns)
    frequency = _infer_decision_frequency(factor_panel, frozen)
    evaluated, label_coverage = _join_outcome_labels(factor_panel, labels)
    ic_series, quantile_returns = _evaluate_periods(evaluated, frozen)
    targets = _build_long_only_targets(factor_panel, frozen)
    turnover = _target_turnover(targets, frozen.target_gross_weight)
    horizon_summary = _summarize_horizons(ic_series, frozen, frequency)
    annual_summary = _summarize_years(ic_series, frozen, frequency)
    quality = _quality_summary(
        factor_panel,
        coverage,
        exposures,
        ic_series,
        quantile_returns,
        label_coverage,
        sorted(int(value) for value in labels["horizon_sessions"].unique()),
        frequency,
        frozen,
    )
    return FactorEvaluationResult(
        factor_panel=factor_panel,
        coverage=coverage,
        distribution=distribution,
        factor_exposures=exposures,
        label_coverage=label_coverage,
        ic_series=ic_series,
        quantile_returns=quantile_returns,
        turnover=turnover,
        target_weights=targets,
        horizon_summary=horizon_summary,
        annual_summary=annual_summary,
        quality=quality,
    )


def _prepare_factor_panel(
    observations: pd.DataFrame,
    spec: FactorEvaluationSpec,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    required = {
        "instrument_id",
        "factor_value",
        "industry_code",
        "market_cap",
        "research_eligible",
        "decision_at",
        "execution_at",
    }
    missing = sorted(required - set(observations.columns))
    if missing:
        raise FactorEvaluationError(f"factor observations missing columns: {missing}")
    work = observations.copy()
    if work.empty:
        raise FactorEvaluationError("factor observations are empty")
    validate_factor_timing(work, spec.factor_family)
    work["instrument_id"] = work["instrument_id"].astype(str).str.strip()
    if (work["instrument_id"] == "").any():
        raise FactorEvaluationError("instrument_id cannot be blank")
    for column in ("decision_at", "execution_at"):
        work[column] = pd.to_datetime(work[column], utc=True, errors="coerce")
    if work[["decision_at", "execution_at"]].isna().any(axis=None):
        raise FactorEvaluationError("decision_at and execution_at must be valid timestamps")
    if work.duplicated(["instrument_id", "decision_at"]).any():
        raise FactorEvaluationError("duplicate instrument-decision factor observations")
    executions_per_decision = work.groupby("decision_at", observed=True)[
        "execution_at"
    ].nunique()
    if (executions_per_decision != 1).any():
        raise FactorEvaluationError("each decision_at must map to exactly one execution_at")
    if not (
        pd.api.types.is_bool_dtype(work["research_eligible"].dtype)
        or str(work["research_eligible"].dtype) == "boolean"
    ):
        raise FactorEvaluationError("research_eligible must use a boolean dtype")
    if work["research_eligible"].isna().any():
        raise FactorEvaluationError("research_eligible cannot be null")
    work["factor_value"] = pd.to_numeric(work["factor_value"], errors="coerce")
    work["market_cap"] = pd.to_numeric(work["market_cap"], errors="coerce")
    work["industry_code"] = work["industry_code"].astype("string").str.strip()
    work["decision_date"] = (
        work["decision_at"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    )

    panels: list[pd.DataFrame] = []
    coverage_rows: list[Dict[str, Any]] = []
    exposure_rows: list[Dict[str, Any]] = []
    for decision_at, cross_section in work.groupby("decision_at", sort=True, observed=True):
        eligible = cross_section.loc[cross_section["research_eligible"]].copy()
        finite_factor = np.isfinite(eligible["factor_value"])
        valid_cap = np.isfinite(eligible["market_cap"]) & (eligible["market_cap"] > 0)
        valid_industry = eligible["industry_code"].notna() & (
            eligible["industry_code"] != ""
        )
        usable = eligible.loc[finite_factor & valid_cap & valid_industry].copy()
        eligible_count = len(eligible)
        usable_count = len(usable)
        coverage_ratio = usable_count / eligible_count if eligible_count else 0.0
        coverage_row: Dict[str, Any] = {
            "decision_at": decision_at,
            "execution_at": cross_section["execution_at"].iloc[0],
            "decision_date": cross_section["decision_date"].iloc[0],
            "universe_rows": len(cross_section),
            "eligible_rows": eligible_count,
            "usable_rows": usable_count,
            "missing_factor_rows": int((~finite_factor).sum()),
            "invalid_market_cap_rows": int((~valid_cap).sum()),
            "missing_industry_rows": int((~valid_industry).sum()),
            "coverage_ratio": coverage_ratio,
            "status": "ok",
            "winsor_lower": np.nan,
            "winsor_upper": np.nan,
            "original_industries": 0,
            "neutralization_industries": 0,
            "sparse_industries_merged": 0,
            "minimum_original_industry_members": 0,
            "minimum_neutralization_industry_members": 0,
            "dynamic_required_cross_section": spec.min_cross_section,
        }
        original_counts = usable.groupby("industry_code", observed=True).size()
        sparse_industries = set(
            original_counts.loc[original_counts < spec.min_industry_members].index
        )
        usable["neutralization_industry_code"] = usable["industry_code"].where(
            ~usable["industry_code"].isin(sparse_industries), "__OTHER__"
        )
        neutralization_counts = usable.groupby(
            "neutralization_industry_code", observed=True
        ).size()
        industry_count = len(neutralization_counts)
        dynamic_required = max(spec.min_cross_section, industry_count + 3)
        coverage_row.update(
            {
                "original_industries": len(original_counts),
                "neutralization_industries": industry_count,
                "sparse_industries_merged": len(sparse_industries),
                "minimum_original_industry_members": (
                    int(original_counts.min()) if len(original_counts) else 0
                ),
                "minimum_neutralization_industry_members": (
                    int(neutralization_counts.min()) if len(neutralization_counts) else 0
                ),
                "dynamic_required_cross_section": dynamic_required,
            }
        )
        if usable_count < dynamic_required:
            coverage_row["status"] = "insufficient_dynamic_cross_section"
            coverage_rows.append(coverage_row)
            continue
        if (
            neutralization_counts.empty
            or neutralization_counts.min() < spec.min_industry_members
        ):
            coverage_row["status"] = "sparse_industry_after_other_merge"
            coverage_rows.append(coverage_row)
            continue
        values = usable["factor_value"].to_numpy(dtype=float)
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median)))
        robust_scale = 1.4826 * mad
        if not np.isfinite(robust_scale) or robust_scale <= 0:
            coverage_row["status"] = "constant_or_degenerate_factor"
            coverage_rows.append(coverage_row)
            continue
        lower = median - spec.winsor_mad_multiplier * robust_scale
        upper = median + spec.winsor_mad_multiplier * robust_scale
        winsorized = np.clip(values, lower, upper)
        pre_neutral = _standardize(winsorized)
        try:
            neutralized, regression_weights = _neutralize(
                pre_neutral,
                usable["neutralization_industry_code"],
                usable["market_cap"].to_numpy(dtype=float),
                spec.neutralization_weighting,
            )
        except FactorEvaluationError:
            coverage_row["status"] = "neutralization_rank_failure"
            coverage_rows.append(coverage_row)
            continue
        score = spec.expected_direction * neutralized
        usable["raw_factor"] = values
        usable["winsorized_factor"] = winsorized
        usable["pre_neutral_zscore"] = pre_neutral
        usable["neutralized_factor"] = neutralized
        usable["signal_score"] = score
        usable["log_market_cap_z"] = _standardize(
            np.log(usable["market_cap"].to_numpy(dtype=float))
        )
        percentile = pd.Series(score, index=usable.index).rank(method="average", pct=True)
        usable["quantile"] = np.ceil(percentile * spec.quantiles).clip(
            1, spec.quantiles
        ).astype(int)
        usable["factor_name"] = spec.factor_name
        usable["factor_family"] = spec.factor_family
        panels.append(
            usable[
                [
                    "factor_name",
                    "factor_family",
                    "instrument_id",
                    "decision_date",
                    "decision_at",
                    "execution_at",
                    "industry_code",
                    "neutralization_industry_code",
                    "market_cap",
                    "log_market_cap_z",
                    "raw_factor",
                    "winsorized_factor",
                    "pre_neutral_zscore",
                    "neutralized_factor",
                    "signal_score",
                    "quantile",
                ]
            ]
        )
        coverage_row["winsor_lower"] = lower
        coverage_row["winsor_upper"] = upper
        coverage_rows.append(coverage_row)
        exposure_rows.extend(
            _exposure_records(
                decision_at,
                usable,
                regression_weights,
                spec,
            )
        )
    if not panels:
        raise FactorEvaluationError("no cross-section passed factor preparation")
    panel = pd.concat(panels, ignore_index=True).sort_values(
        ["decision_at", "instrument_id"]
    ).reset_index(drop=True)
    coverage = pd.DataFrame(coverage_rows).sort_values("decision_at").reset_index(drop=True)
    exposures = pd.DataFrame(exposure_rows).sort_values(
        ["decision_at", "scope", "quantile", "exposure_type", "exposure_name"]
    ).reset_index(drop=True)
    return panel, coverage, exposures


def _prepare_forward_returns(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "instrument_id",
        "execution_at",
        "horizon_sessions",
        "label_start_at",
        "label_end_at",
        "outcome_observation_end_at",
        "forward_return",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FactorEvaluationError(f"forward returns missing columns: {missing}")
    labels = frame.copy()
    if labels.empty:
        raise FactorEvaluationError("forward returns are empty")
    labels["instrument_id"] = labels["instrument_id"].astype(str).str.strip()
    for column in (
        "execution_at",
        "label_start_at",
        "label_end_at",
        "outcome_observation_end_at",
    ):
        labels[column] = pd.to_datetime(labels[column], utc=True, errors="coerce")
    timestamp_columns = [
        "execution_at",
        "label_start_at",
        "label_end_at",
        "outcome_observation_end_at",
    ]
    if labels[timestamp_columns].isna().any(axis=None):
        raise FactorEvaluationError("forward-return timestamps must be valid")
    if labels["outcome_observation_end_at"].nunique() != 1:
        raise FactorEvaluationError("outcome_observation_end_at must be globally frozen")
    numeric_horizon = pd.to_numeric(labels["horizon_sessions"], errors="coerce")
    if numeric_horizon.isna().any() or (numeric_horizon <= 0).any() or (
        numeric_horizon % 1 != 0
    ).any():
        raise FactorEvaluationError("horizon_sessions must be positive integers")
    labels["horizon_sessions"] = numeric_horizon.astype(int)
    labels["forward_return"] = pd.to_numeric(labels["forward_return"], errors="coerce")
    finite_or_null = labels["forward_return"].isna() | np.isfinite(
        labels["forward_return"]
    )
    if (~finite_or_null).any():
        raise FactorEvaluationError("forward_return must be finite or structural null")
    if (labels["label_start_at"] < labels["execution_at"]).any():
        raise FactorEvaluationError("outcome label starts before factor execution")
    if (labels["label_end_at"] <= labels["label_start_at"]).any():
        raise FactorEvaluationError("outcome label must end after it starts")
    structural_tail = (
        labels["label_end_at"] > labels["outcome_observation_end_at"]
    )
    if (structural_tail & labels["forward_return"].notna()).any():
        raise FactorEvaluationError(
            "forward_return cannot use observations beyond outcome_observation_end_at"
        )
    keys = ["instrument_id", "execution_at", "horizon_sessions"]
    if labels.duplicated(keys).any():
        raise FactorEvaluationError("duplicate forward-return labels")
    return labels.sort_values(keys).reset_index(drop=True)


def _join_outcome_labels(
    panel: pd.DataFrame, labels: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    horizons = sorted(labels["horizon_sessions"].unique())
    expected = panel.assign(_join_key=1).merge(
        pd.DataFrame({"horizon_sessions": horizons, "_join_key": 1}), on="_join_key"
    ).drop(columns="_join_key")
    evaluated = expected.merge(
        labels,
        on=["instrument_id", "execution_at", "horizon_sessions"],
        how="left",
        validate="one_to_one",
    )
    sample_end = labels["outcome_observation_end_at"].iloc[0]
    evaluated["structural_tail"] = (
        evaluated["label_end_at"].notna()
        & (evaluated["label_end_at"] > evaluated["outcome_observation_end_at"])
    )
    evaluated["structurally_available"] = ~evaluated["structural_tail"]
    evaluated["label_matched"] = evaluated["forward_return"].notna()
    coverage_rows = []
    for horizon, group in evaluated.groupby(
        "horizon_sessions", sort=True, observed=True
    ):
        available = group["structurally_available"]
        matched = group["label_matched"]
        available_count = int(available.sum())
        matched_available = int((available & matched).sum())
        coverage_rows.append(
            {
                "horizon_sessions": int(horizon),
                "outcome_observation_end_at": sample_end,
                "expected_rows": len(group),
                "structural_tail_rows": int(group["structural_tail"].sum()),
                "structurally_available_rows": available_count,
                "matched_rows": matched_available,
                "unexpected_missing_rows": int((available & ~matched).sum()),
                "match_rate": (
                    matched_available / available_count if available_count else 0.0
                ),
            }
        )
    coverage = pd.DataFrame(coverage_rows).sort_values("horizon_sessions")
    usable = evaluated["structurally_available"] & evaluated["label_matched"]
    return evaluated.loc[usable].copy(), coverage.reset_index(drop=True)


def _evaluate_periods(
    evaluated: pd.DataFrame, spec: FactorEvaluationSpec
) -> tuple[pd.DataFrame, pd.DataFrame]:
    ic_rows: list[Dict[str, Any]] = []
    quantile_rows: list[Dict[str, Any]] = []
    keys = ["decision_at", "horizon_sessions"]
    for (decision_at, horizon), group in evaluated.groupby(keys, sort=True, observed=True):
        count = len(group)
        if count < spec.min_ic_observations:
            continue
        score = group["signal_score"].to_numpy(dtype=float)
        raw_returns = group["forward_return"].to_numpy(dtype=float)
        raw_metrics = _cross_section_return_metrics(
            score, raw_returns, group["quantile"], spec.quantiles
        )
        try:
            residual_returns, _ = _residualize(
                raw_returns,
                group["neutralization_industry_code"],
                group["market_cap"].to_numpy(dtype=float),
                spec.neutralization_weighting,
            )
            residual_metrics = _cross_section_return_metrics(
                score, residual_returns, group["quantile"], spec.quantiles
            )
            residualization_status = "ok"
            residualization_error = ""
        except FactorEvaluationError as error:
            residual_returns = np.full(count, np.nan)
            residual_metrics = {
                "pearson_ic": np.nan,
                "rank_ic": np.nan,
                "top_minus_bottom_return": np.nan,
                "quantile_monotonicity": np.nan,
            }
            residualization_status = f"failed:{type(error).__name__}"
            residualization_error = str(error)
        use_residualized = (
            spec.return_basis == "residualized" and residualization_status == "ok"
        )
        primary = residual_metrics if use_residualized else raw_metrics
        effective_return_basis = (
            "residualized"
            if use_residualized
            else "raw_fallback"
            if spec.return_basis == "residualized"
            else "raw"
        )
        ic_rows.append(
            {
                "decision_at": decision_at,
                "decision_date": group["decision_date"].iloc[0],
                "horizon_sessions": int(horizon),
                "observations": count,
                "configured_return_basis": spec.return_basis,
                "return_basis": effective_return_basis,
                "residualization_status": residualization_status,
                "residualization_error": residualization_error,
                "pearson_ic": primary["pearson_ic"],
                "rank_ic": primary["rank_ic"],
                "top_minus_bottom_return": primary["top_minus_bottom_return"],
                "quantile_monotonicity": primary["quantile_monotonicity"],
                "raw_pearson_ic": raw_metrics["pearson_ic"],
                "raw_rank_ic": raw_metrics["rank_ic"],
                "raw_top_minus_bottom_return": raw_metrics[
                    "top_minus_bottom_return"
                ],
                "residualized_pearson_ic": residual_metrics["pearson_ic"],
                "residualized_rank_ic": residual_metrics["rank_ic"],
                "residualized_top_minus_bottom_return": residual_metrics[
                    "top_minus_bottom_return"
                ],
            }
        )
        residual_by_index = pd.Series(residual_returns, index=group.index)
        for quantile, quantile_group in group.groupby(
            "quantile", sort=True, observed=True
        ):
            raw_mean = float(quantile_group["forward_return"].mean())
            residual_mean = float(residual_by_index.loc[quantile_group.index].mean())
            quantile_rows.append(
                {
                    "decision_at": decision_at,
                    "decision_date": group["decision_date"].iloc[0],
                    "horizon_sessions": int(horizon),
                    "quantile": int(quantile),
                    "observations": len(quantile_group),
                    "configured_return_basis": spec.return_basis,
                    "return_basis": effective_return_basis,
                    "residualization_status": residualization_status,
                    "residualization_error": residualization_error,
                    "equal_weight_return": (
                        residual_mean if use_residualized else raw_mean
                    ),
                    "equal_weight_raw_return": raw_mean,
                    "equal_weight_residualized_return": residual_mean,
                }
            )
    if not ic_rows:
        raise FactorEvaluationError("no period has enough observations for IC evaluation")
    return (
        pd.DataFrame(ic_rows).sort_values(keys).reset_index(drop=True),
        pd.DataFrame(quantile_rows)
        .sort_values([*keys, "quantile"])
        .reset_index(drop=True),
    )


def _cross_section_return_metrics(
    score: np.ndarray,
    returns: np.ndarray,
    quantiles: pd.Series,
    quantile_count: int,
) -> Dict[str, float]:
    rank_ic = _correlation(
        pd.Series(score).rank(method="average").to_numpy(dtype=float),
        pd.Series(returns).rank(method="average").to_numpy(dtype=float),
    )
    means = pd.DataFrame(
        {"quantile": quantiles.to_numpy(), "return": returns}
    ).groupby("quantile", observed=True)["return"].mean()
    top_bottom = (
        float(means.get(quantile_count, np.nan) - means.get(1, np.nan))
        if 1 in means.index and quantile_count in means.index
        else np.nan
    )
    return {
        "pearson_ic": _correlation(score, returns),
        "rank_ic": rank_ic,
        "top_minus_bottom_return": top_bottom,
        "quantile_monotonicity": _correlation(
            means.index.to_numpy(dtype=float), means.to_numpy(dtype=float)
        ),
    }


def _build_long_only_targets(
    panel: pd.DataFrame, spec: FactorEvaluationSpec
) -> pd.DataFrame:
    selected = panel.loc[panel["quantile"] == spec.quantiles].copy()
    counts = selected.groupby("decision_at", observed=True)["instrument_id"].transform("size")
    selected["target_weight"] = spec.target_gross_weight / counts
    selected["target_policy"] = "equal_weight_top_quantile_long_only"
    return selected[
        [
            "factor_name",
            "decision_date",
            "decision_at",
            "execution_at",
            "instrument_id",
            "signal_score",
            "target_weight",
            "target_policy",
        ]
    ].sort_values(["decision_at", "instrument_id"]).reset_index(drop=True)


def _target_turnover(targets: pd.DataFrame, gross_weight: float) -> pd.DataFrame:
    rows = []
    previous: Dict[str, float] = {"__CASH__": 1.0}
    for decision_at, group in targets.groupby("decision_at", sort=True, observed=True):
        current = dict(zip(group["instrument_id"], group["target_weight"]))
        current["__CASH__"] = 1.0 - sum(current.values())
        instruments = set(previous) | set(current)
        turnover = 0.5 * sum(
            abs(current.get(item, 0.0) - previous.get(item, 0.0))
            for item in instruments
        )
        rows.append(
            {
                "decision_at": decision_at,
                "decision_date": group["decision_date"].iloc[0],
                "constituents": len(current) - 1,
                "one_way_turnover": turnover,
                "target_gross_weight": gross_weight,
            }
        )
        previous = current
    return pd.DataFrame(rows)


def _infer_decision_frequency(
    panel: pd.DataFrame, spec: FactorEvaluationSpec
) -> Dict[str, float]:
    decisions = pd.DatetimeIndex(panel["decision_at"].drop_duplicates()).sort_values()
    if len(decisions) < 2:
        raise FactorEvaluationError("at least two decision dates are required")
    intervals = np.diff(decisions.asi8) / (24.0 * 60.0 * 60.0 * 1e9)
    if not np.isfinite(intervals).all() or (intervals <= 0).any():
        raise FactorEvaluationError("decision frequency contains invalid intervals")
    median_days = float(np.median(intervals))
    inferred_periods = 365.2425 / median_days
    relative_error = abs(inferred_periods - spec.annualization_periods) / float(
        spec.annualization_periods
    )
    if relative_error > spec.annualization_frequency_tolerance:
        raise FactorEvaluationError(
            "frozen annualization_periods disagrees with decision_at frequency: "
            f"configured={spec.annualization_periods}, inferred={inferred_periods:.4f}"
        )
    trading_sessions_per_period = float(max(1, round(252.0 / inferred_periods)))
    return {
        "median_calendar_days_per_period": median_days,
        "inferred_periods_per_year": inferred_periods,
        "configured_periods_per_year": float(spec.annualization_periods),
        "relative_frequency_error": relative_error,
        "trading_sessions_per_period": trading_sessions_per_period,
    }


def _summarize_horizons(
    ic_series: pd.DataFrame,
    spec: FactorEvaluationSpec,
    frequency: Dict[str, float],
) -> pd.DataFrame:
    rows = []
    for horizon, group in ic_series.groupby("horizon_sessions", sort=True, observed=True):
        rows.append(_summary_record(group, spec, int(horizon), frequency))
    return pd.DataFrame(rows)


def _summarize_years(
    ic_series: pd.DataFrame,
    spec: FactorEvaluationSpec,
    frequency: Dict[str, float],
) -> pd.DataFrame:
    work = ic_series.copy()
    work["year"] = pd.to_datetime(work["decision_date"]).dt.year
    rows = []
    for (year, horizon), group in work.groupby(
        ["year", "horizon_sessions"], sort=True, observed=True
    ):
        record = _summary_record(group, spec, int(horizon), frequency)
        record["year"] = int(year)
        rows.append(record)
    columns = ["year"] + [column for column in rows[0] if column != "year"]
    return pd.DataFrame(rows)[columns]


def _summary_record(
    group: pd.DataFrame,
    spec: FactorEvaluationSpec,
    horizon: int,
    frequency: Dict[str, float],
) -> Dict[str, Any]:
    rank_ic = group["rank_ic"].dropna().to_numpy(dtype=float)
    pearson = group["pearson_ic"].dropna().to_numpy(dtype=float)
    spread = group["top_minus_bottom_return"].dropna().to_numpy(dtype=float)
    rank_std = float(np.std(rank_ic, ddof=1)) if len(rank_ic) > 1 else np.nan
    nw_lags = max(
        spec.minimum_newey_west_lags,
        int(math.ceil(horizon / frequency["trading_sessions_per_period"])),
    )
    rank_long_run_variance = _newey_west_long_run_variance(rank_ic, nw_lags)
    return {
        "horizon_sessions": horizon,
        "periods": len(group),
        "mean_pearson_ic": _safe_mean(pearson),
        "mean_rank_ic": _safe_mean(rank_ic),
        "rank_ic_std": rank_std,
        "newey_west_lags": nw_lags,
        "inferred_periods_per_year": frequency["inferred_periods_per_year"],
        "rank_ic_long_run_std": (
            math.sqrt(rank_long_run_variance)
            if np.isfinite(rank_long_run_variance)
            else np.nan
        ),
        "annualized_rank_ic_ir": (
            float(
                np.mean(rank_ic)
                / math.sqrt(rank_long_run_variance)
                * math.sqrt(frequency["inferred_periods_per_year"])
            )
            if len(rank_ic) > 1 and rank_long_run_variance > 0
            else np.nan
        ),
        "rank_ic_positive_rate": (
            float(np.mean(rank_ic > 0)) if len(rank_ic) else np.nan
        ),
        "rank_ic_newey_west_t": _newey_west_mean_t(rank_ic, nw_lags),
        "mean_top_minus_bottom_return": _safe_mean(spread),
        "spread_newey_west_t": _newey_west_mean_t(spread, nw_lags),
        "mean_quantile_monotonicity": _safe_mean(
            group["quantile_monotonicity"].dropna().to_numpy(dtype=float)
        ),
    }


def _distribution_summary(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for decision_at, group in panel.groupby("decision_at", sort=True, observed=True):
        raw = group["raw_factor"].to_numpy(dtype=float)
        score = group["signal_score"].to_numpy(dtype=float)
        rows.append(
            {
                "decision_at": decision_at,
                "decision_date": group["decision_date"].iloc[0],
                "observations": len(group),
                "raw_min": float(np.min(raw)),
                "raw_p01": float(np.quantile(raw, 0.01)),
                "raw_p05": float(np.quantile(raw, 0.05)),
                "raw_median": float(np.median(raw)),
                "raw_p95": float(np.quantile(raw, 0.95)),
                "raw_p99": float(np.quantile(raw, 0.99)),
                "raw_max": float(np.max(raw)),
                "raw_mean": float(np.mean(raw)),
                "raw_std": float(np.std(raw, ddof=0)),
                "score_mean": float(np.mean(score)),
                "score_std": float(np.std(score, ddof=0)),
            }
        )
    return pd.DataFrame(rows)


def _quality_summary(
    factor_panel: pd.DataFrame,
    coverage: pd.DataFrame,
    exposures: pd.DataFrame,
    ic_series: pd.DataFrame,
    quantile_returns: pd.DataFrame,
    label_coverage: pd.DataFrame,
    expected_horizons: list[int],
    frequency: Dict[str, float],
    spec: FactorEvaluationSpec,
) -> Dict[str, Any]:
    valid_coverage = coverage.loc[coverage["status"] == "ok"]
    periods_by_horizon = (
        ic_series.groupby("horizon_sessions", observed=True)
        .size()
        .reindex(expected_horizons, fill_value=0)
    )
    period_index = pd.MultiIndex.from_frame(
        ic_series[["decision_at", "horizon_sessions"]].drop_duplicates()
    )
    endpoint_counts = quantile_returns.loc[
        quantile_returns["quantile"].isin([1, spec.quantiles])
    ].groupby(["decision_at", "horizon_sessions"], observed=True)["quantile"].nunique()
    endpoint_counts = endpoint_counts.reindex(period_index, fill_value=0)
    first_order_industry = exposures.loc[
        exposures["exposure_type"] == "first_order_industry_mean"
    ]
    first_order_size = exposures.loc[
        exposures["exposure_type"] == "first_order_log_market_cap_correlation"
    ]
    endpoint_exposures = exposures.loc[
        exposures["quantile"].isin([1, spec.quantiles])
    ]
    portfolio_industry = endpoint_exposures.loc[
        endpoint_exposures["exposure_type"] == "industry_active_weight"
    ]
    portfolio_size = endpoint_exposures.loc[
        endpoint_exposures["exposure_type"] == "log_market_cap_z_active"
    ]
    structurally_available = int(label_coverage["structurally_available_rows"].sum())
    matched = int(label_coverage["matched_rows"].sum())
    label_match_rate = matched / structurally_available if structurally_available else 0.0
    residualization_failures = int(
        (ic_series["residualization_status"] != "ok").sum()
    )
    hard_failures = {
        "failed_cross_section_dates": int((coverage["status"] != "ok").sum()),
        "coverage_below_threshold_dates": int(
            (valid_coverage["coverage_ratio"] < spec.minimum_coverage).sum()
        ),
        "horizon_label_match_rate_below_threshold": int(
            (label_coverage["match_rate"] + 1e-12 < spec.minimum_label_match_rate).sum()
        ),
        "horizons_below_minimum_periods": int(
            (periods_by_horizon < spec.min_evaluation_periods).sum()
        ),
        "quantile_endpoint_missing_periods": int((endpoint_counts < 2).sum()),
        "neutralization_first_order_industry_sanity_breach_rows": int(
            (first_order_industry["exposure_value"].abs() > 1e-8).sum()
        ),
        "neutralization_first_order_size_sanity_breach_rows": int(
            (first_order_size["exposure_value"].abs() > 1e-8).sum()
        ),
        "top_bottom_industry_active_weight_breach_rows": int(
            (
                portfolio_industry["exposure_value"].abs()
                > portfolio_industry["exposure_limit"] + 1e-12
            ).sum()
        ),
        "top_bottom_log_market_cap_z_breach_rows": int(
            (
                portfolio_size["exposure_value"].abs()
                > portfolio_size["exposure_limit"] + 1e-12
            ).sum()
        ),
        "requested_residualized_return_failure_periods": (
            residualization_failures if spec.return_basis == "residualized" else 0
        ),
    }
    return {
        "decision_dates": int(coverage["decision_at"].nunique()),
        "prepared_dates": int(factor_panel["decision_at"].nunique()),
        "prepared_rows": len(factor_panel),
        "horizons": sorted(int(value) for value in ic_series["horizon_sessions"].unique()),
        "median_coverage": float(valid_coverage["coverage_ratio"].median()),
        "minimum_coverage_observed": float(valid_coverage["coverage_ratio"].min()),
        "label_match_rate": label_match_rate,
        "minimum_horizon_label_match_rate": float(label_coverage["match_rate"].min()),
        "structural_tail_label_rows": int(label_coverage["structural_tail_rows"].sum()),
        "unexpected_missing_label_rows": int(
            label_coverage["unexpected_missing_rows"].sum()
        ),
        "residualization_failure_periods": residualization_failures,
        "maximum_top_bottom_industry_active_weight": float(
            portfolio_industry["exposure_value"].abs().max()
        ),
        "maximum_top_bottom_log_market_cap_z": float(
            portfolio_size["exposure_value"].abs().max()
        ),
        "maximum_top_bottom_industry_sampling_sigma": float(
            portfolio_industry["standardized_exposure"].max()
        ),
        "maximum_top_bottom_size_sampling_sigma": float(
            portfolio_size["standardized_exposure"].max()
        ),
        "decision_frequency": frequency,
        "hard_failures": hard_failures,
        "promotion_passed": all(value == 0 for value in hard_failures.values()),
    }


def _neutralize(
    values: np.ndarray,
    industry: pd.Series,
    market_cap: np.ndarray,
    weighting: str,
) -> tuple[np.ndarray, np.ndarray]:
    residual, weights = _residualize(values, industry, market_cap, weighting)
    return _weighted_standardize(residual, weights), weights


def _residualize(
    values: np.ndarray,
    industry: pd.Series,
    market_cap: np.ndarray,
    weighting: str,
) -> tuple[np.ndarray, np.ndarray]:
    dummies = pd.get_dummies(industry.astype(str), dtype=float).sort_index(axis=1)
    log_cap = np.log(market_cap)
    log_cap = _standardize(log_cap)
    design = np.column_stack([dummies.to_numpy(dtype=float), log_cap])
    if len(values) <= design.shape[1] + 1 or np.linalg.matrix_rank(design) < design.shape[1]:
        raise FactorEvaluationError("neutralization design is rank deficient")
    weights = np.ones(len(values), dtype=float)
    if weighting == "sqrt_market_cap":
        weights = np.sqrt(market_cap)
        weights = weights / float(np.mean(weights))
    whiten = np.sqrt(weights)
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        beta = np.linalg.lstsq(
            design * whiten[:, None], values * whiten, rcond=None
        )[0]
        residual = values - design @ beta
    if not np.isfinite(beta).all() or not np.isfinite(residual).all():
        raise FactorEvaluationError("neutralization produced non-finite coefficients")
    return residual, weights


def _exposure_records(
    decision_at: pd.Timestamp,
    frame: pd.DataFrame,
    weights: np.ndarray,
    spec: FactorEvaluationSpec,
) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    score = frame["signal_score"].to_numpy(dtype=float)
    neutralization_industry = frame["neutralization_industry_code"].astype(str).to_numpy()
    original_industry = frame["industry_code"].astype(str).to_numpy()
    log_cap = np.log(frame["market_cap"].to_numpy(dtype=float))
    for name in sorted(set(neutralization_industry)):
        mask = neutralization_industry == name
        rows.append(
            {
                "decision_at": decision_at,
                "scope": "full_cross_section_sanity",
                "quantile": 0,
                "exposure_type": "first_order_industry_mean",
                "exposure_name": name,
                "observations": int(mask.sum()),
                "benchmark_value": 0.0,
                "portfolio_value": float(
                    np.average(score[mask], weights=weights[mask])
                ),
                "exposure_value": float(np.average(score[mask], weights=weights[mask])),
                "sampling_standard_error": 0.0,
                "exposure_limit": 1e-8,
                "standardized_exposure": np.nan,
            }
        )
    rows.append(
        {
            "decision_at": decision_at,
            "scope": "full_cross_section_sanity",
            "quantile": 0,
            "exposure_type": "first_order_log_market_cap_correlation",
            "exposure_name": "log_market_cap",
            "observations": len(score),
            "benchmark_value": 0.0,
            "portfolio_value": _weighted_correlation(score, log_cap, weights),
            "exposure_value": _weighted_correlation(score, log_cap, weights),
            "sampling_standard_error": 0.0,
            "exposure_limit": 1e-8,
            "standardized_exposure": np.nan,
        }
    )
    benchmark_industry = pd.Series(original_industry).value_counts(normalize=True)
    benchmark_size = float(frame["log_market_cap_z"].mean())
    for quantile, group in frame.groupby("quantile", sort=True, observed=True):
        portfolio_industry = group["industry_code"].astype(str).value_counts(normalize=True)
        for name in sorted(set(benchmark_industry.index) | set(portfolio_industry.index)):
            benchmark = float(benchmark_industry.get(name, 0.0))
            portfolio = float(portfolio_industry.get(name, 0.0))
            active = portfolio - benchmark
            standard_error = math.sqrt(
                benchmark * (1.0 - benchmark) / len(group)
            )
            exposure_limit = max(
                spec.industry_active_weight_floor,
                spec.exposure_sampling_sigma_multiplier * standard_error,
            )
            rows.append(
                {
                    "decision_at": decision_at,
                    "scope": "quantile_portfolio",
                    "quantile": int(quantile),
                    "exposure_type": "industry_active_weight",
                    "exposure_name": name,
                    "observations": len(group),
                    "benchmark_value": benchmark,
                    "portfolio_value": portfolio,
                    "exposure_value": active,
                    "sampling_standard_error": standard_error,
                    "exposure_limit": exposure_limit,
                    "standardized_exposure": (
                        abs(active) / standard_error if standard_error > 0 else np.nan
                    ),
                }
            )
        portfolio_size = float(group["log_market_cap_z"].mean())
        size_active = portfolio_size - benchmark_size
        size_standard_error = float(frame["log_market_cap_z"].std(ddof=0)) / math.sqrt(
            len(group)
        )
        size_limit = max(
            spec.log_market_cap_z_floor,
            spec.exposure_sampling_sigma_multiplier * size_standard_error,
        )
        rows.append(
            {
                "decision_at": decision_at,
                "scope": "quantile_portfolio",
                "quantile": int(quantile),
                "exposure_type": "log_market_cap_z_active",
                "exposure_name": "log_market_cap_z",
                "observations": len(group),
                "benchmark_value": benchmark_size,
                "portfolio_value": portfolio_size,
                "exposure_value": size_active,
                "sampling_standard_error": size_standard_error,
                "exposure_limit": size_limit,
                "standardized_exposure": (
                    abs(size_active) / size_standard_error
                    if size_standard_error > 0
                    else np.nan
                ),
            }
        )
    return rows


def _standardize(values: Sequence[float]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    standard_deviation = float(np.std(array, ddof=0))
    if not np.isfinite(standard_deviation) or standard_deviation <= 0:
        raise FactorEvaluationError("cannot standardize a constant vector")
    return (array - float(np.mean(array))) / standard_deviation


def _weighted_standardize(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    normalized = weights / float(np.sum(weights))
    mean = float(np.sum(normalized * values))
    variance = float(np.sum(normalized * (values - mean) ** 2))
    if not np.isfinite(variance) or variance <= 0:
        raise FactorEvaluationError("cannot standardize a constant weighted vector")
    return (values - mean) / math.sqrt(variance)


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    if len(left) < 2 or np.std(left) <= 0 or np.std(right) <= 0:
        return np.nan
    return float(np.corrcoef(left, right)[0, 1])


def _weighted_correlation(left: np.ndarray, right: np.ndarray, weights: np.ndarray) -> float:
    weight = weights / float(np.sum(weights))
    left_centered = left - float(np.sum(weight * left))
    right_centered = right - float(np.sum(weight * right))
    covariance = float(np.sum(weight * left_centered * right_centered))
    variance = float(
        np.sum(weight * left_centered**2) * np.sum(weight * right_centered**2)
    )
    return covariance / math.sqrt(variance) if variance > 0 else np.nan


def _newey_west_mean_t(values: np.ndarray, lags: int) -> float:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    count = len(array)
    if count < 2:
        return np.nan
    long_run_variance = _newey_west_long_run_variance(array, lags)
    variance_of_mean = max(long_run_variance, 0.0) / count
    return (
        float(np.mean(array) / math.sqrt(variance_of_mean))
        if variance_of_mean > 0
        else np.nan
    )


def _newey_west_long_run_variance(values: np.ndarray, lags: int) -> float:
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    count = len(array)
    if count < 2:
        return np.nan
    centered = array - float(np.mean(array))
    lag_count = min(lags, count - 1)
    long_run_variance = float(np.dot(centered, centered) / count)
    for lag in range(1, lag_count + 1):
        covariance = float(np.dot(centered[lag:], centered[:-lag]) / count)
        long_run_variance += 2.0 * (1.0 - lag / (lag_count + 1.0)) * covariance
    return max(long_run_variance, 0.0)


def _safe_mean(values: np.ndarray) -> float:
    return float(np.mean(values)) if len(values) else np.nan


def _fingerprint(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
