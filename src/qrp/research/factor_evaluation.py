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
    min_cross_section: int = 20
    min_ic_observations: int = 20
    min_evaluation_periods: int = 26
    minimum_coverage: float = 0.80
    minimum_label_match_rate: float = 0.95
    neutralization_weighting: str = "sqrt_market_cap"
    target_gross_weight: float = 0.98
    annualization_periods: int = 52
    newey_west_lags: int = 4
    version: str = "p07_single_factor_evaluation_v1"

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
            self.annualization_periods,
        ) < 2:
            raise ValueError("sample and annualization requirements must be at least 2")
        if not 0 < self.minimum_coverage <= 1:
            raise ValueError("minimum_coverage must be in (0, 1]")
        if not 0 < self.minimum_label_match_rate <= 1:
            raise ValueError("minimum_label_match_rate must be in (0, 1]")
        if self.neutralization_weighting not in {"equal", "sqrt_market_cap"}:
            raise ValueError("unsupported neutralization_weighting")
        if not 0 < self.target_gross_weight <= 1:
            raise ValueError("target_gross_weight must be in (0, 1]")
        if self.newey_west_lags < 0:
            raise ValueError("newey_west_lags must be non-negative")
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
    evaluated, label_match_rate = _join_outcome_labels(factor_panel, labels)
    ic_series, quantile_returns = _evaluate_periods(evaluated, frozen)
    targets = _build_long_only_targets(factor_panel, frozen)
    turnover = _target_turnover(targets, frozen.target_gross_weight)
    horizon_summary = _summarize_horizons(ic_series, frozen)
    annual_summary = _summarize_years(ic_series, frozen)
    quality = _quality_summary(
        factor_panel,
        coverage,
        exposures,
        ic_series,
        quantile_returns,
        label_match_rate,
        sorted(int(value) for value in labels["horizon_sessions"].unique()),
        frozen,
    )
    return FactorEvaluationResult(
        factor_panel=factor_panel,
        coverage=coverage,
        distribution=distribution,
        factor_exposures=exposures,
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
        }
        if usable_count < spec.min_cross_section:
            coverage_row["status"] = "insufficient_cross_section"
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
                usable["industry_code"],
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
                    "market_cap",
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
        log_cap = np.log(usable["market_cap"].to_numpy(dtype=float))
        exposure_rows.extend(
            _exposure_records(
                decision_at,
                usable["industry_code"].astype(str).to_numpy(),
                score,
                log_cap,
                regression_weights,
            )
        )
    if not panels:
        raise FactorEvaluationError("no cross-section passed factor preparation")
    panel = pd.concat(panels, ignore_index=True).sort_values(
        ["decision_at", "instrument_id"]
    ).reset_index(drop=True)
    coverage = pd.DataFrame(coverage_rows).sort_values("decision_at").reset_index(drop=True)
    exposures = pd.DataFrame(exposure_rows).sort_values(
        ["decision_at", "exposure_type", "exposure_name"]
    ).reset_index(drop=True)
    return panel, coverage, exposures


def _prepare_forward_returns(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "instrument_id",
        "execution_at",
        "horizon_sessions",
        "label_start_at",
        "label_end_at",
        "forward_return",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FactorEvaluationError(f"forward returns missing columns: {missing}")
    labels = frame.copy()
    if labels.empty:
        raise FactorEvaluationError("forward returns are empty")
    labels["instrument_id"] = labels["instrument_id"].astype(str).str.strip()
    for column in ("execution_at", "label_start_at", "label_end_at"):
        labels[column] = pd.to_datetime(labels[column], utc=True, errors="coerce")
    if labels[["execution_at", "label_start_at", "label_end_at"]].isna().any(axis=None):
        raise FactorEvaluationError("forward-return timestamps must be valid")
    numeric_horizon = pd.to_numeric(labels["horizon_sessions"], errors="coerce")
    if numeric_horizon.isna().any() or (numeric_horizon <= 0).any() or (
        numeric_horizon % 1 != 0
    ).any():
        raise FactorEvaluationError("horizon_sessions must be positive integers")
    labels["horizon_sessions"] = numeric_horizon.astype(int)
    labels["forward_return"] = pd.to_numeric(labels["forward_return"], errors="coerce")
    if (~np.isfinite(labels["forward_return"])).any():
        raise FactorEvaluationError("forward_return must be finite")
    if (labels["label_start_at"] < labels["execution_at"]).any():
        raise FactorEvaluationError("outcome label starts before factor execution")
    if (labels["label_end_at"] <= labels["label_start_at"]).any():
        raise FactorEvaluationError("outcome label must end after it starts")
    keys = ["instrument_id", "execution_at", "horizon_sessions"]
    if labels.duplicated(keys).any():
        raise FactorEvaluationError("duplicate forward-return labels")
    return labels.sort_values(keys).reset_index(drop=True)


def _join_outcome_labels(
    panel: pd.DataFrame, labels: pd.DataFrame
) -> tuple[pd.DataFrame, float]:
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
    matched = evaluated["forward_return"].notna()
    label_match_rate = float(matched.mean()) if len(evaluated) else 0.0
    return evaluated.loc[matched].copy(), label_match_rate


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
        returns = group["forward_return"].to_numpy(dtype=float)
        pearson = _correlation(score, returns)
        rank_ic = _correlation(
            pd.Series(score).rank(method="average").to_numpy(dtype=float),
            pd.Series(returns).rank(method="average").to_numpy(dtype=float),
        )
        means = group.groupby("quantile", observed=True)["forward_return"].mean()
        top_bottom = (
            float(means.get(spec.quantiles, np.nan) - means.get(1, np.nan))
            if 1 in means.index and spec.quantiles in means.index
            else np.nan
        )
        ic_rows.append(
            {
                "decision_at": decision_at,
                "decision_date": group["decision_date"].iloc[0],
                "horizon_sessions": int(horizon),
                "observations": count,
                "pearson_ic": pearson,
                "rank_ic": rank_ic,
                "top_minus_bottom_return": top_bottom,
                "quantile_monotonicity": _correlation(
                    means.index.to_numpy(dtype=float), means.to_numpy(dtype=float)
                ),
            }
        )
        for quantile, quantile_group in group.groupby("quantile", sort=True, observed=True):
            quantile_rows.append(
                {
                    "decision_at": decision_at,
                    "decision_date": group["decision_date"].iloc[0],
                    "horizon_sessions": int(horizon),
                    "quantile": int(quantile),
                    "observations": len(quantile_group),
                    "equal_weight_return": float(quantile_group["forward_return"].mean()),
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
    previous: Dict[str, float] = {}
    for decision_at, group in targets.groupby("decision_at", sort=True, observed=True):
        current = dict(zip(group["instrument_id"], group["target_weight"]))
        if not previous:
            turnover = float(sum(current.values()))
        else:
            instruments = set(previous) | set(current)
            turnover = 0.5 * sum(
                abs(current.get(item, 0.0) - previous.get(item, 0.0))
                for item in instruments
            )
            turnover += 0.5 * abs(
                (1.0 - sum(current.values())) - (1.0 - sum(previous.values()))
            )
        rows.append(
            {
                "decision_at": decision_at,
                "decision_date": group["decision_date"].iloc[0],
                "constituents": len(current),
                "one_way_turnover": turnover,
                "target_gross_weight": gross_weight,
            }
        )
        previous = current
    return pd.DataFrame(rows)


def _summarize_horizons(
    ic_series: pd.DataFrame, spec: FactorEvaluationSpec
) -> pd.DataFrame:
    rows = []
    for horizon, group in ic_series.groupby("horizon_sessions", sort=True, observed=True):
        rows.append(_summary_record(group, spec, int(horizon)))
    return pd.DataFrame(rows)


def _summarize_years(
    ic_series: pd.DataFrame, spec: FactorEvaluationSpec
) -> pd.DataFrame:
    work = ic_series.copy()
    work["year"] = pd.to_datetime(work["decision_date"]).dt.year
    rows = []
    for (year, horizon), group in work.groupby(
        ["year", "horizon_sessions"], sort=True, observed=True
    ):
        record = _summary_record(group, spec, int(horizon))
        record["year"] = int(year)
        rows.append(record)
    columns = ["year"] + [column for column in rows[0] if column != "year"]
    return pd.DataFrame(rows)[columns]


def _summary_record(
    group: pd.DataFrame, spec: FactorEvaluationSpec, horizon: int
) -> Dict[str, Any]:
    rank_ic = group["rank_ic"].dropna().to_numpy(dtype=float)
    pearson = group["pearson_ic"].dropna().to_numpy(dtype=float)
    spread = group["top_minus_bottom_return"].dropna().to_numpy(dtype=float)
    rank_std = float(np.std(rank_ic, ddof=1)) if len(rank_ic) > 1 else np.nan
    return {
        "horizon_sessions": horizon,
        "periods": len(group),
        "mean_pearson_ic": _safe_mean(pearson),
        "mean_rank_ic": _safe_mean(rank_ic),
        "rank_ic_std": rank_std,
        "annualized_rank_ic_ir": (
            float(np.mean(rank_ic) / rank_std * math.sqrt(spec.annualization_periods))
            if len(rank_ic) > 1 and rank_std > 0
            else np.nan
        ),
        "rank_ic_positive_rate": (
            float(np.mean(rank_ic > 0)) if len(rank_ic) else np.nan
        ),
        "rank_ic_newey_west_t": _newey_west_mean_t(rank_ic, spec.newey_west_lags),
        "mean_top_minus_bottom_return": _safe_mean(spread),
        "spread_newey_west_t": _newey_west_mean_t(spread, spec.newey_west_lags),
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
    label_match_rate: float,
    expected_horizons: list[int],
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
    industry = exposures.loc[exposures["exposure_type"] == "industry"]
    size = exposures.loc[exposures["exposure_type"] == "log_market_cap"]
    hard_failures = {
        "failed_cross_section_dates": int((coverage["status"] != "ok").sum()),
        "coverage_below_threshold_dates": int(
            (valid_coverage["coverage_ratio"] < spec.minimum_coverage).sum()
        ),
        "label_match_rate_below_threshold": int(
            label_match_rate + 1e-12 < spec.minimum_label_match_rate
        ),
        "horizons_below_minimum_periods": int(
            (periods_by_horizon < spec.min_evaluation_periods).sum()
        ),
        "quantile_endpoint_missing_periods": int((endpoint_counts < 2).sum()),
        "industry_neutralization_breach_rows": int(
            (industry["exposure_value"].abs() > 1e-8).sum()
        ),
        "size_neutralization_breach_rows": int(
            (size["exposure_value"].abs() > 1e-8).sum()
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
        "hard_failures": hard_failures,
        "promotion_passed": all(value == 0 for value in hard_failures.values()),
    }


def _neutralize(
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
    beta = np.linalg.lstsq(design * whiten[:, None], values * whiten, rcond=None)[0]
    residual = values - design @ beta
    return _weighted_standardize(residual, weights), weights


def _exposure_records(
    decision_at: pd.Timestamp,
    industry: np.ndarray,
    score: np.ndarray,
    log_cap: np.ndarray,
    weights: np.ndarray,
) -> list[Dict[str, Any]]:
    rows = []
    for name in sorted(set(industry)):
        mask = industry == name
        rows.append(
            {
                "decision_at": decision_at,
                "exposure_type": "industry",
                "exposure_name": name,
                "observations": int(mask.sum()),
                "exposure_value": float(np.average(score[mask], weights=weights[mask])),
            }
        )
    rows.append(
        {
            "decision_at": decision_at,
            "exposure_type": "log_market_cap",
            "exposure_name": "log_market_cap",
            "observations": len(score),
            "exposure_value": _weighted_correlation(score, log_cap, weights),
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
    centered = array - float(np.mean(array))
    lag_count = min(lags, count - 1)
    long_run_variance = float(np.dot(centered, centered) / count)
    for lag in range(1, lag_count + 1):
        covariance = float(np.dot(centered[lag:], centered[:-lag]) / count)
        long_run_variance += 2.0 * (1.0 - lag / (lag_count + 1.0)) * covariance
    variance_of_mean = max(long_run_variance, 0.0) / count
    return (
        float(np.mean(array) / math.sqrt(variance_of_mean))
        if variance_of_mean > 0
        else np.nan
    )


def _safe_mean(values: np.ndarray) -> float:
    return float(np.mean(values)) if len(values) else np.nan


def _fingerprint(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
