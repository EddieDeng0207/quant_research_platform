"""Reproducible preflight coverage audit for the first formal P0.7 factor."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from .catalog import load_latest_snapshot, load_partitioned_snapshot
from .fundamentals import _load_aliases


class FactorCoverageError(RuntimeError):
    """Raised when the coverage audit cannot prove its input identity."""


_COMBINATIONS = {
    0: "usable",
    1: "industry_only",
    2: "market_cap_only",
    3: "market_cap_and_industry",
    4: "factor_only",
    5: "factor_and_industry",
    6: "factor_and_market_cap",
    7: "all_three",
}


def build_inverse_pb_coverage_audit(
    *,
    tradability_artifacts: Sequence[Path],
    lake_root: Path,
    industry_artifact: Path,
    output_root: Path,
    start_date: str,
    end_date: str,
    minimum_coverage: float = 0.80,
    aliases_path: Optional[Path] = None,
) -> Path:
    """Audit BP=1/PB, total market cap and historical-industry joint coverage.

    This is a feature-only preflight. It never reads forward returns and therefore
    must not be interpreted as evidence that BP has investment value.
    """
    if not 0 < minimum_coverage <= 1:
        raise ValueError("minimum_coverage must be in (0, 1]")
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError("start_date must be on or before end_date")

    if not tradability_artifacts:
        raise ValueError("at least one tradability artifact is required")
    tradability_frames = []
    tradability_identities = []
    for artifact in tradability_artifacts:
        frame, identity = _load_artifact_parquet(
            artifact,
            manifest_output_key="output",
            columns=(
                "instrument_id",
                "trade_date",
                "standard_research_eligible",
            ),
        )
        tradability_frames.append(frame)
        tradability_identities.append(identity)
    tradability = pd.concat(tradability_frames, ignore_index=True)
    required_tradability = {
        "instrument_id",
        "trade_date",
        "standard_research_eligible",
    }
    missing = sorted(required_tradability - set(tradability.columns))
    if missing:
        raise FactorCoverageError(f"tradability artifact missing columns: {missing}")
    tradability["trade_date"] = pd.to_datetime(
        tradability["trade_date"], errors="coerce"
    ).dt.normalize()
    if tradability["trade_date"].isna().any():
        raise FactorCoverageError("tradability artifact has invalid trade dates")
    tradability = tradability.loc[
        (tradability["trade_date"] >= start) & (tradability["trade_date"] <= end)
    ].copy()
    if tradability.empty:
        raise FactorCoverageError("tradability artifact has no rows in requested window")
    if not pd.api.types.is_bool_dtype(tradability["standard_research_eligible"].dtype):
        raise FactorCoverageError("standard_research_eligible must be boolean")
    eligible = tradability.loc[
        tradability["standard_research_eligible"],
        ["instrument_id", "trade_date"],
    ].copy()
    if eligible.duplicated(["instrument_id", "trade_date"]).any():
        raise FactorCoverageError("tradability artifact has duplicate instrument dates")

    indicators = load_partitioned_snapshot(
        Path(lake_root),
        "tushare",
        "daily_indicators",
        str(start.date()),
        str(end.date()),
        columns=("symbol", "trade_date", "pb", "total_mv", "ingested_at"),
    )
    aliases_file = aliases_path or (
        Path(__file__).resolve().parents[3] / "configs" / "instrument_aliases.json"
    )
    mappings = load_latest_snapshot(Path(lake_root), "tushare", "security_code_mappings")
    aliases, alias_identity = _stable_id_aliases(aliases_file, mappings.frame)
    indicator_frame = indicators.frame.copy()
    indicator_frame["trade_date"] = pd.to_datetime(
        indicator_frame["trade_date"], errors="coerce"
    ).dt.normalize()
    indicator_frame["instrument_id"] = indicator_frame["symbol"].map(aliases)
    indicator_frame["instrument_id"] = indicator_frame["instrument_id"].fillna(
        "CN_EQ:" + indicator_frame["symbol"].astype(str)
    )
    indicator_frame["pb"] = pd.to_numeric(indicator_frame["pb"], errors="coerce")
    indicator_frame["total_mv"] = pd.to_numeric(indicator_frame["total_mv"], errors="coerce")
    indicator_frame = _deduplicate_indicators(indicator_frame)
    panel = eligible.merge(
        indicator_frame[["instrument_id", "trade_date", "pb", "total_mv"]],
        on=["instrument_id", "trade_date"],
        how="left",
        validate="one_to_one",
    )

    membership, industry_identity = _load_artifact_parquet(
        industry_artifact,
        manifest_output_key="outputs.membership",
        columns=(
            "instrument_id",
            "industry_code",
            "membership_start",
            "membership_end",
        ),
    )
    membership["membership_start"] = pd.to_datetime(
        membership["membership_start"], errors="coerce"
    ).dt.normalize()
    membership["membership_end"] = pd.to_datetime(
        membership["membership_end"], errors="coerce"
    ).dt.normalize()
    if membership[["membership_start", "membership_end"]].isna().any(axis=None):
        raise FactorCoverageError("industry artifact has null or invalid intervals")
    panel = pd.merge_asof(
        panel.sort_values(["trade_date", "instrument_id"]),
        membership.sort_values(["membership_start", "instrument_id"]),
        by="instrument_id",
        left_on="trade_date",
        right_on="membership_start",
        direction="backward",
        allow_exact_matches=True,
    )
    outside_interval = panel["trade_date"] > panel["membership_end"]
    panel.loc[outside_interval, "industry_code"] = pd.NA

    panel["missing_factor"] = ~(np.isfinite(panel["pb"]) & (panel["pb"] > 0))
    panel["invalid_market_cap"] = ~(np.isfinite(panel["total_mv"]) & (panel["total_mv"] > 0))
    industry = panel["industry_code"].astype("string").str.strip()
    panel["missing_industry"] = industry.isna() | industry.eq("")
    panel["missing_code"] = (
        panel["missing_factor"].astype("uint8") * 4
        + panel["invalid_market_cap"].astype("uint8") * 2
        + panel["missing_industry"].astype("uint8")
    )

    coverage, joint = _daily_coverage(panel, minimum_coverage)
    weekly_dates = _weekly_decision_dates(coverage["decision_date"])
    weekly = coverage.loc[coverage["decision_date"].isin(weekly_dates)].copy()
    yearly = _yearly_summary(weekly)
    summary = _summary(coverage, weekly, joint, minimum_coverage)
    summary["interpretation"] = {
        "factor": "inverse_pb",
        "factor_formula": "1 / pb; rows with null, non-finite or pb <= 0 are missing",
        "market_cap": "total_mv; rows with null, non-finite or total_mv <= 0 are invalid",
        "industry": "historical Shenwan L1 effective on the decision date",
        "denominator": "P0.5 standard_research_eligible rows",
        "weekly_policy": "last open session of each W-FRI week",
        "outcome_labels_read": False,
        "investment_conclusion_allowed": False,
    }
    failed_p05 = [item for item in tradability_identities if not item["promotion_passed"]]
    summary["upstream_p05"] = {
        "artifacts": len(tradability_identities),
        "promoted_artifacts": len(tradability_identities) - len(failed_p05),
        "failed_artifacts": len(failed_p05),
        "failed_years": [item["start_date"][:4] for item in failed_p05],
        "formal_p07_release_allowed": not failed_p05,
    }

    identity = {
        "schema_version": "p07_joint_coverage_preflight_v1",
        "start_date": str(start.date()),
        "end_date": str(end.date()),
        "minimum_coverage": minimum_coverage,
        "factor_policy": "inverse_positive_pb_v1",
        "tradability": tradability_identities,
        "daily_indicators_fingerprint": indicators.fingerprint,
        "daily_indicator_inputs": [
            {"path": item["path"], "sha256": item["sha256"]} for item in indicators.manifest_entries
        ],
        "industry": industry_identity,
        "aliases": alias_identity,
        "security_code_mappings_fingerprint": mappings.fingerprint,
        "implementation_sha256": _implementation_sha256(),
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for name, frame in {
        "coverage": coverage,
        "weekly_coverage": weekly,
        "joint_distribution": joint,
        "yearly_summary": yearly,
    }.items():
        path = destination / f"{name}.parquet"
        _write_immutable_parquet(frame, path)
        outputs[name] = {"path": path.name, "rows": len(frame), "sha256": _sha256(path)}
    _write_immutable_json(summary, destination / "summary.json")
    outputs["summary"] = {
        "path": "summary.json",
        "sha256": _sha256(destination / "summary.json"),
    }
    manifest = {
        "artifact_id": artifact_id,
        "identity": identity,
        "outputs": outputs,
        "summary": summary,
        "guardrails": {
            "p05_standard_research_eligible_denominator": True,
            "all_p05_artifacts_promoted": not failed_p05,
            "stable_instrument_identity": True,
            "historical_industry_intervals": True,
            "overlap_counted_once_in_coverage": True,
            "eight_mutually_exclusive_missingness_combinations": True,
            "outcome_labels_not_read": True,
        },
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    return destination


def _daily_coverage(
    panel: pd.DataFrame, minimum_coverage: float
) -> tuple[pd.DataFrame, pd.DataFrame]:
    counts = (
        panel.groupby(["trade_date", "missing_code"], observed=True)
        .size()
        .unstack(fill_value=0)
        .reindex(columns=range(8), fill_value=0)
    )
    counts.columns = [_COMBINATIONS[int(value)] for value in counts.columns]
    counts = counts.reset_index().rename(columns={"trade_date": "decision_date"})
    coverage = counts.copy()
    coverage["eligible_rows"] = coverage[list(_COMBINATIONS.values())].sum(axis=1)
    coverage["usable_rows"] = coverage["usable"]
    coverage["missing_factor_rows"] = coverage[
        ["factor_only", "factor_and_industry", "factor_and_market_cap", "all_three"]
    ].sum(axis=1)
    coverage["invalid_market_cap_rows"] = coverage[
        ["market_cap_only", "market_cap_and_industry", "factor_and_market_cap", "all_three"]
    ].sum(axis=1)
    coverage["missing_industry_rows"] = coverage[
        ["industry_only", "market_cap_and_industry", "factor_and_industry", "all_three"]
    ].sum(axis=1)
    denominator = coverage["eligible_rows"].replace(0, np.nan)
    coverage["coverage_ratio"] = coverage["usable_rows"] / denominator
    coverage["missing_factor_rate"] = coverage["missing_factor_rows"] / denominator
    coverage["invalid_market_cap_rate"] = coverage["invalid_market_cap_rows"] / denominator
    coverage["missing_industry_rate"] = coverage["missing_industry_rows"] / denominator
    coverage["union_missing_rows"] = coverage["eligible_rows"] - coverage["usable_rows"]
    coverage["union_missing_rate"] = coverage["union_missing_rows"] / denominator
    coverage["coverage_if_factor_fixed"] = (
        coverage["usable"] + coverage["factor_only"]
    ) / denominator
    coverage["coverage_if_market_cap_fixed"] = (
        coverage["usable"] + coverage["market_cap_only"]
    ) / denominator
    coverage["coverage_if_industry_fixed"] = (
        coverage["usable"] + coverage["industry_only"]
    ) / denominator
    coverage["below_minimum_coverage"] = coverage["coverage_ratio"] < minimum_coverage
    long = counts.melt(
        id_vars="decision_date",
        value_vars=list(_COMBINATIONS.values()),
        var_name="missingness_combination",
        value_name="rows",
    )
    long = long.merge(coverage[["decision_date", "eligible_rows"]], on="decision_date", how="left")
    long["rate"] = long["rows"] / long["eligible_rows"].replace(0, np.nan)
    return coverage.sort_values("decision_date").reset_index(drop=True), long


def _weekly_decision_dates(dates: pd.Series) -> pd.DatetimeIndex:
    values = pd.Series(pd.to_datetime(dates).dropna().sort_values().unique())
    if values.empty:
        return pd.DatetimeIndex([])
    periods = values.dt.to_period("W-FRI")
    return pd.DatetimeIndex(values.groupby(periods).max().tolist())


def _yearly_summary(weekly: pd.DataFrame) -> pd.DataFrame:
    work = weekly.copy()
    work["year"] = work["decision_date"].dt.year
    return (
        work.groupby("year", observed=True)
        .agg(
            decision_dates=("decision_date", "size"),
            median_coverage=("coverage_ratio", "median"),
            minimum_coverage=("coverage_ratio", "min"),
            p05_coverage=("coverage_ratio", lambda values: values.quantile(0.05)),
            below_minimum_dates=("below_minimum_coverage", "sum"),
            median_factor_missing=("missing_factor_rate", "median"),
            median_invalid_market_cap=("invalid_market_cap_rate", "median"),
            median_missing_industry=("missing_industry_rate", "median"),
        )
        .reset_index()
    )


def _summary(
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    joint: pd.DataFrame,
    minimum_coverage: float,
) -> Dict[str, Any]:
    def describe(frame: pd.DataFrame) -> Dict[str, Any]:
        return {
            "dates": len(frame),
            "coverage": {
                "median": float(frame["coverage_ratio"].median()),
                "minimum": float(frame["coverage_ratio"].min()),
                "p05": float(frame["coverage_ratio"].quantile(0.05)),
            },
            "median_component_rates": {
                "missing_factor": float(frame["missing_factor_rate"].median()),
                "invalid_market_cap": float(frame["invalid_market_cap_rate"].median()),
                "missing_industry": float(frame["missing_industry_rate"].median()),
            },
            "below_minimum_dates": int(frame["below_minimum_coverage"].sum()),
            "minimum_coverage_threshold": minimum_coverage,
            "single_fix_scenarios_below_minimum_dates": {
                "factor": int((frame["coverage_if_factor_fixed"] < minimum_coverage).sum()),
                "market_cap": int((frame["coverage_if_market_cap_fixed"] < minimum_coverage).sum()),
                "industry": int((frame["coverage_if_industry_fixed"] < minimum_coverage).sum()),
            },
        }

    aggregate_joint = joint.groupby("missingness_combination", observed=True)["rows"].sum()
    total = int(aggregate_joint.sum())
    return {
        "daily": describe(daily),
        "weekly": describe(weekly),
        "aggregate_joint_distribution": {
            key: {
                "rows": int(value),
                "rate": float(value / total) if total else None,
            }
            for key, value in aggregate_joint.items()
        },
    }


def _deduplicate_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    keys = ["instrument_id", "trade_date"]
    duplicates = frame.loc[frame.duplicated(keys, keep=False)].copy()
    if not duplicates.empty:
        disagreement = duplicates.groupby(keys, observed=True)[["pb", "total_mv"]].nunique(
            dropna=True
        )
        if (disagreement > 1).any(axis=None):
            sample = disagreement.loc[(disagreement > 1).any(axis=1)].head(10)
            raise FactorCoverageError(
                f"aliased daily indicators disagree for stable identities: {sample.index.tolist()}"
            )
        collapsed = []
        for _, group in duplicates.groupby(keys, observed=True, sort=False):
            row = group.sort_values("symbol").iloc[0].copy()
            for column in ("pb", "total_mv"):
                non_null = group[column].dropna()
                if pd.isna(row[column]) and not non_null.empty:
                    row[column] = non_null.iloc[0]
            collapsed.append(row)
        unique = frame.loc[~frame.duplicated(keys, keep=False)]
        frame = pd.concat([unique, pd.DataFrame(collapsed)], ignore_index=True)
    return frame.sort_values(keys + ["symbol"]).reset_index(drop=True)


def _stable_id_aliases(
    aliases_path: Path, mappings: pd.DataFrame
) -> tuple[Dict[str, str], Dict[str, Any]]:
    path = Path(aliases_path)
    aliases, reviewed_alias_identity = _load_aliases(path, None)
    required = {"historical_symbol", "current_symbol"}
    missing = sorted(required - set(mappings.columns))
    if missing:
        raise FactorCoverageError(f"security-code mappings missing columns: {missing}")
    for row in mappings.to_dict("records"):
        current = str(row["current_symbol"])
        stable = f"CN_EQ:BSE:{current}"
        aliases[str(row["historical_symbol"])] = stable
        aliases[current] = stable
    return aliases, {
        "reviewed_aliases": reviewed_alias_identity["reviewed_aliases"],
        "security_code_mapping_rows": len(mappings),
    }


def _implementation_sha256() -> str:
    digest = hashlib.sha256()
    for path in sorted(
        [
            Path(__file__),
            Path(__file__).with_name("catalog.py"),
            Path(__file__).with_name("fundamentals.py"),
        ]
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_artifact_parquet(
    artifact: Path,
    *,
    manifest_output_key: str,
    columns: tuple[str, ...],
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    root = Path(artifact)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise FactorCoverageError(f"artifact manifest does not exist: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output: Mapping[str, Any] = manifest
    for key in manifest_output_key.split("."):
        value = output.get(key)
        if not isinstance(value, Mapping):
            raise FactorCoverageError(f"artifact manifest lacks mapping {manifest_output_key}")
        output = value
    path = root / Path(str(output["path"])).name
    if not path.exists() or _sha256(path) != output.get("sha256"):
        raise FactorCoverageError(f"artifact output hash mismatch: {path}")
    return pd.read_parquet(path, columns=list(columns)), {
        "artifact_id": manifest.get("artifact_id"),
        "manifest_sha256": _sha256(manifest_path),
        "output_sha256": output.get("sha256"),
        "start_date": manifest.get("quality", {}).get("start_date"),
        "end_date": manifest.get("quality", {}).get("end_date"),
        "promotion_passed": bool(manifest.get("quality", {}).get("promotion_passed", False)),
        "hard_failures": manifest.get("quality", {}).get("hard_failures", {}),
    }


def _write_immutable_parquet(frame: pd.DataFrame, path: Path) -> None:
    if path.exists():
        existing = pd.read_parquet(path)
        if not existing.equals(frame):
            raise FactorCoverageError(f"immutable output conflict: {path}")
        return
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    frame.to_parquet(temporary, index=False, engine="pyarrow")
    os.replace(temporary, path)


def _write_immutable_json(payload: Mapping[str, Any], path: Path) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise FactorCoverageError(f"immutable output conflict: {path}")
        return
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
