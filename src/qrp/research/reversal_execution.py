"""Content-addressed P0.7-to-P0.6.3 handoff for promoted factors."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Sequence

import numpy as np
import pandas as pd

from qrp.execution.capacity import build_lagged_capacity_panel
from qrp.versioning import (
    VersionControlError,
    environment_lock_identity,
    inspect_git_repository,
)

from .price_reversal import (
    _lake_manifest_entries,
    _load_latest_partitions,
    _resolve_vendor_field,
    _sha256,
    _utc_timestamp,
)


class FactorExecutionInputError(RuntimeError):
    """Raised when the P0.7-to-P0.6.3 handoff cannot be proven."""


@dataclass(frozen=True)
class FactorExecutionInputSpec:
    execution_year: int = 2023
    min_periods_20: int = 20
    min_periods_60: int = 60
    capacity_policy: str = "lagged_liquidity_volatility_v3"
    version: str = "factor_p063_execution_inputs_v2"

    def validate(self) -> "FactorExecutionInputSpec":
        if not 1990 <= self.execution_year <= 2100:
            raise ValueError("execution_year is outside the supported range")
        if not 1 <= self.min_periods_20 <= 20:
            raise ValueError("min_periods_20 must be within [1, 20]")
        if not 1 <= self.min_periods_60 <= 60:
            raise ValueError("min_periods_60 must be within [1, 60]")
        if self.capacity_policy != "lagged_liquidity_volatility_v3":
            raise ValueError("unsupported capacity_policy")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(asdict(self))


def build_factor_execution_input_artifact(
    *,
    factor_artifact: Path,
    warmup_tradability_artifacts: Sequence[Path],
    execution_tradability_artifact: Path,
    lake_root: Path,
    output_root: Path,
    research_as_of_at: str,
    spec: FactorExecutionInputSpec | None = None,
    require_clean_git: bool = False,
) -> Path:
    """Build immutable factor targets and lagged institutional capacity inputs."""
    frozen = (spec or FactorExecutionInputSpec()).validate()
    research_as_of = _utc_timestamp(research_as_of_at)
    targets, factor_identity = _load_factor_targets(Path(factor_artifact), frozen)
    market, p05_identities = _load_market_chain(
        [*warmup_tradability_artifacts, execution_tradability_artifact]
    )
    execution_identity = p05_identities[-1]
    if int(execution_identity["start_date"][:4]) != frozen.execution_year:
        raise FactorExecutionInputError("execution P0.5 artifact does not match execution_year")
    target_keys = targets[["trade_date", "instrument_id"]]
    market_keys = market[["trade_date", "instrument_id"]]
    missing_target_keys = target_keys.merge(
        market_keys,
        on=["trade_date", "instrument_id"],
        how="left",
        indicator=True,
    )
    if (missing_target_keys["_merge"] != "both").any():
        raise FactorExecutionInputError("P0.5 market does not cover every P0.7 target key")

    observed = market.loc[
        market["has_bar"]
        & market["close"].gt(0)
        & market["volume"].gt(0)
        & market["amount"].gt(0)
    ].copy()
    relevant_ids = set(targets["instrument_id"])
    observed = observed.loc[observed["instrument_id"].isin(relevant_ids)].copy()
    dates = set(pd.DatetimeIndex(observed["trade_date"]))
    lake = Path(lake_root)
    entries = _lake_manifest_entries(lake)
    adjustments, adjustment_entries = _load_latest_partitions(
        lake,
        entries,
        dataset="adjustment_factors",
        partition_dates=dates,
        columns=("symbol", "trade_date", "adj_factor", "ingested_at"),
        research_as_of=research_as_of,
    )
    indicators, indicator_entries = _load_latest_partitions(
        lake,
        entries,
        dataset="daily_indicators",
        partition_dates=dates,
        columns=("symbol", "trade_date", "circ_mv", "ingested_at"),
        research_as_of=research_as_of,
    )
    adjustments["trade_date"] = pd.to_datetime(adjustments["trade_date"]).dt.normalize()
    indicators["trade_date"] = pd.to_datetime(indicators["trade_date"]).dt.normalize()
    observed = _resolve_vendor_field(
        observed,
        adjustments,
        value_column="adj_factor",
        output_column="adj_factor",
        source_output_column="adjustment_source_symbol",
        candidate_values_must_match=False,
    )
    observed = _resolve_vendor_field(
        observed,
        indicators,
        value_column="circ_mv",
        output_column="circ_mv",
        source_output_column="capacity_market_value_source_symbol",
    )
    if observed[["adj_factor", "circ_mv"]].isna().any().any():
        raise FactorExecutionInputError(
            "adjustment factor or CNY free-float market value is missing on observed bars"
        )
    capacity = build_lagged_capacity_panel(
        observed[["instrument_id", "trade_date", "close", "volume", "amount"]],
        observed[["instrument_id", "trade_date", "circ_mv"]],
        observed[["instrument_id", "trade_date", "adj_factor"]],
        key_columns=("instrument_id",),
        min_periods_20=frozen.min_periods_20,
        min_periods_60=frozen.min_periods_60,
    )
    capacity = capacity.loc[
        capacity["trade_date"].dt.year == frozen.execution_year
    ].reset_index(drop=True)
    quality = _quality_summary(
        targets,
        capacity,
        market,
        frozen,
        expected_target_gross_weight=float(factor_identity["target_gross_weight"]),
    )
    implementation = _implementation_identity()
    code_identity = None
    environment_lock = None
    try:
        git = inspect_git_repository(Path(__file__), require_clean=require_clean_git)
        code_identity = git.to_dict()
        environment_lock = environment_lock_identity(Path(git.repository_root))
    except VersionControlError:
        if require_clean_git:
            raise
    identity = {
        "schema_version": frozen.version,
        "spec_sha256": frozen.fingerprint,
        "factor": factor_identity,
        "p05_chain": p05_identities,
        "adjustment_factors": _partition_identity(adjustment_entries),
        "daily_indicators": _partition_identity(indicator_entries),
        "research_as_of_at": research_as_of.isoformat(),
        "implementation_sha256": implementation["tree_sha256"],
        "git_commit": code_identity["commit"] if code_identity else None,
        "git_tree": code_identity["tree"] if code_identity else None,
        "git_dirty_state_sha256": (
            code_identity["dirty_state_sha256"] if code_identity else None
        ),
        "environment_lock_sha256": (
            environment_lock["sha256"] if environment_lock else None
        ),
    }
    artifact_id = _fingerprint(identity)[:20]
    destination = Path(output_root) / "factor_execution_inputs" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for name, frame, sort_columns in (
        ("targets", targets, ["trade_date", "instrument_id"]),
        ("capacity", capacity, ["trade_date", "instrument_id"]),
    ):
        path = destination / f"{name}.parquet"
        logical_sha = _frame_fingerprint(frame, sort_columns)
        _write_immutable_parquet(frame, path, sort_columns, logical_sha)
        outputs[name] = {
            "path": path.name,
            "rows": len(frame),
            "logical_sha256": logical_sha,
            "sha256": _sha256(path),
        }
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": frozen.version,
        "identity": identity,
        "spec": {**asdict(frozen), "sha256": frozen.fingerprint},
        "outputs": outputs,
        "quality": quality,
        "guardrails": {
            "targets_from_promoted_p07": True,
            "target_trade_dates_match_p05": True,
            "capacity_uses_observed_positive_raw_bars": True,
            "capacity_is_shifted_one_security_session": True,
            "volatility_uses_causal_adjusted_close": True,
            "free_float_market_cap_unit": "CNY",
            "suspension_capacity_is_not_imputed": True,
            "target_gross_weight_read_from_promoted_p07": True,
            "formal_cli_requires_clean_git": True,
            "git_commit_bound": code_identity is not None,
            "environment_lock_bound": environment_lock is not None,
            "investment_conclusion_allowed": False,
        },
        "implementation": implementation,
        "code_identity": code_identity,
        "environment_lock": environment_lock,
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    if not quality["promotion_passed"]:
        raise FactorExecutionInputError(
            f"factor execution inputs failed promotion: {quality['hard_failures']}"
        )
    return destination


# Backward-compatible names for existing rev20 scripts. New research should use
# the factor-generic API and CLI so the handoff is not coupled to one signal.
ReversalExecutionInputError = FactorExecutionInputError
ReversalExecutionInputSpec = FactorExecutionInputSpec
build_reversal_execution_input_artifact = build_factor_execution_input_artifact


def _load_factor_targets(
    artifact: Path, spec: FactorExecutionInputSpec
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("quality", {}).get("promotion_passed", False):
        raise FactorExecutionInputError("P0.7 factor artifact was not promoted")
    metadata = manifest["outputs"]["target_weights"]
    path = artifact / "target_weights.parquet"
    if _sha256(path) != metadata["sha256"]:
        raise FactorExecutionInputError("P0.7 target-weight hash mismatch")
    targets = pd.read_parquet(path)
    required = {
        "execution_at",
        "decision_at",
        "instrument_id",
        "target_weight",
        "factor_name",
    }
    missing = sorted(required - set(targets.columns))
    if missing:
        raise FactorExecutionInputError(f"P0.7 targets missing columns: {missing}")
    execution = pd.to_datetime(targets["execution_at"], utc=True)
    decision = pd.to_datetime(targets["decision_at"], utc=True)
    if execution.isna().any() or decision.isna().any() or (decision >= execution).any():
        raise FactorExecutionInputError("P0.7 targets violate decision-before-execution timing")
    targets["trade_date"] = (
        execution.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    )
    targets = targets.loc[targets["trade_date"].dt.year == spec.execution_year].copy()
    targets = targets[
        ["trade_date", "decision_at", "instrument_id", "target_weight", "factor_name"]
    ].sort_values(["trade_date", "instrument_id"]).reset_index(drop=True)
    if targets.empty:
        raise FactorExecutionInputError("P0.7 artifact has no targets in execution_year")
    if targets.duplicated(["trade_date", "instrument_id"]).any():
        raise FactorExecutionInputError("P0.7 targets contain duplicate execution keys")
    weights = pd.to_numeric(targets["target_weight"], errors="coerce")
    if (~np.isfinite(weights)).any() or (~weights.ge(0)).any():
        raise FactorExecutionInputError("P0.7 targets must contain finite nonnegative weights")
    factor_names = set(targets["factor_name"].astype("string"))
    expected_factor_name = str(manifest.get("factor_spec", {}).get("factor_name", ""))
    if factor_names != {expected_factor_name}:
        raise FactorExecutionInputError("P0.7 target factor identity does not match manifest")
    target_gross_weight = manifest.get("factor_spec", {}).get("target_gross_weight")
    if target_gross_weight is None:
        raise FactorExecutionInputError("P0.7 manifest has no frozen target_gross_weight")
    return targets, {
        "artifact_id": manifest["artifact_id"],
        "manifest_sha256": _sha256(manifest_path),
        "target_weights_sha256": metadata["sha256"],
        "git_commit": manifest["identity"].get("git_commit"),
        "factor_name": expected_factor_name,
        "factor_family": manifest.get("factor_spec", {}).get("factor_family"),
        "factor_spec_sha256": manifest.get("factor_spec", {}).get("sha256"),
        "target_gross_weight": float(target_gross_weight),
    }


def _load_market_chain(
    artifacts: Sequence[Path],
) -> tuple[pd.DataFrame, list[Dict[str, Any]]]:
    if len(artifacts) < 2:
        raise ValueError("capacity construction requires warmup and execution P0.5 artifacts")
    frames = []
    identities = []
    columns = [
        "instrument_id",
        "symbol",
        "source_bar_symbol",
        "trade_date",
        "close",
        "volume",
        "amount",
        "has_bar",
        "execution_event_at",
    ]
    for artifact in artifacts:
        root = Path(artifact)
        manifest_path = root / "manifest.json"
        parquet_path = root / "tradability.parquet"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not manifest.get("quality", {}).get("promotion_passed", False):
            raise FactorExecutionInputError(f"P0.5 artifact was not promoted: {root}")
        if _sha256(parquet_path) != manifest["output"]["sha256"]:
            raise FactorExecutionInputError(f"P0.5 hash mismatch: {root}")
        frames.append(pd.read_parquet(parquet_path, columns=columns))
        identities.append(
            {
                "artifact_id": manifest["artifact_id"],
                "manifest_sha256": _sha256(manifest_path),
                "parquet_sha256": manifest["output"]["sha256"],
                "start_date": manifest["identity"]["start_date"],
                "end_date": manifest["identity"]["end_date"],
            }
        )
    market = pd.concat(frames, ignore_index=True)
    market["trade_date"] = pd.to_datetime(market["trade_date"]).dt.normalize()
    for column in ("close", "volume", "amount"):
        market[column] = pd.to_numeric(market[column], errors="coerce")
    if market.duplicated(["instrument_id", "trade_date"]).any():
        raise FactorExecutionInputError("P0.5 capacity chain has duplicate keys")
    return market, identities


def _quality_summary(
    targets: pd.DataFrame,
    capacity: pd.DataFrame,
    market: pd.DataFrame,
    spec: FactorExecutionInputSpec,
    *,
    expected_target_gross_weight: float = 0.98,
) -> Dict[str, Any]:
    target_sums = targets.groupby("trade_date", observed=True)["target_weight"].sum()
    execution_market = market.loc[market["trade_date"].dt.year == spec.execution_year]
    target_market = targets.merge(
        execution_market[["trade_date", "instrument_id", "has_bar", "execution_event_at"]],
        on=["trade_date", "instrument_id"],
        how="left",
        validate="one_to_one",
    )
    target_capacity = target_market.merge(
        capacity[["trade_date", "instrument_id", "capacity_inputs_complete", "capacity_available_at"]],
        on=["trade_date", "instrument_id"],
        how="left",
        validate="one_to_one",
    )
    available = pd.to_datetime(target_capacity["capacity_available_at"], utc=True)
    execution = pd.to_datetime(target_capacity["execution_event_at"], utc=True)
    future_capacity = available.notna() & execution.notna() & (available > execution)
    observed_targets = target_capacity["has_bar"].astype("boolean").fillna(False)
    complete_targets = (
        target_capacity["capacity_inputs_complete"].astype("boolean").fillna(False)
    )
    hard_failures = {
        "target_weight_sum_breach_dates": int(
            ((target_sums - expected_target_gross_weight).abs() > 1e-9).sum()
        ),
        "target_market_key_missing_rows": int(target_capacity["has_bar"].isna().sum()),
        "incomplete_capacity_on_observed_target_rows": int(
            (observed_targets & ~complete_targets).sum()
        ),
        "capacity_available_after_execution_rows": int(future_capacity.sum()),
    }
    return {
        "target_rows": len(targets),
        "target_dates": int(targets["trade_date"].nunique()),
        "unique_target_instruments": int(targets["instrument_id"].nunique()),
        "target_weight_sum_min": float(target_sums.min()),
        "target_weight_sum_max": float(target_sums.max()),
        "expected_target_gross_weight": float(expected_target_gross_weight),
        "capacity_rows": len(capacity),
        "complete_capacity_rows": int(capacity["capacity_inputs_complete"].sum()),
        "observed_target_rows": int(observed_targets.sum()),
        "complete_observed_target_rows": int((observed_targets & complete_targets).sum()),
        "complete_observed_target_rate": float(
            (observed_targets & complete_targets).sum() / observed_targets.sum()
            if observed_targets.sum()
            else 0.0
        ),
        "hard_failures": hard_failures,
        "promotion_passed": all(value == 0 for value in hard_failures.values()),
    }


def _partition_identity(entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    references = [{"path": item["path"], "sha256": item["sha256"]} for item in entries]
    return {
        "partitions": len(references),
        "fingerprint": _fingerprint(references),
        "references": references,
    }


def _implementation_identity() -> Dict[str, Any]:
    here = Path(__file__)
    paths = [
        here,
        here.with_name("price_reversal.py"),
        here.parents[1] / "execution" / "capacity.py",
    ]
    root = here.resolve().parents[3]
    files = []
    for path in sorted(paths):
        files.append(
            {
                "path": str(path.resolve().relative_to(root)),
                "sha256": _sha256(path),
            }
        )
    return {"tree_sha256": _fingerprint(files), "files": files}


def _frame_fingerprint(frame: pd.DataFrame, sort_columns: Sequence[str]) -> str:
    normalized = frame.sort_values(list(sort_columns)).reset_index(drop=True)
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def _write_immutable_parquet(
    frame: pd.DataFrame, path: Path, sort_columns: Sequence[str], logical_sha: str
) -> None:
    if path.exists():
        if _frame_fingerprint(pd.read_parquet(path), sort_columns) != logical_sha:
            raise FactorExecutionInputError(f"immutable execution-input conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.sort_values(list(sort_columns)).to_parquet(temporary, index=False)
    os.replace(temporary, path)


def _write_immutable_json(payload: Dict[str, Any], path: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise FactorExecutionInputError(f"immutable execution-input manifest conflict: {path}")
    if not path.exists():
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
