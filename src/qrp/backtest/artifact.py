"""Immutable P0.6.3 portfolio-backtest artifact construction."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from qrp.execution.daily import ExecutionError, ExecutionSpec, FeePolicy
from qrp.execution.scenarios import DEFAULT_SCENARIOS
from qrp.versioning import (
    VersionControlError,
    environment_lock_identity,
    inspect_git_repository,
)

from .engine import (
    BacktestResult,
    BacktestSpec,
    build_stale_valuation_bounds,
    run_portfolio_backtest,
)


def build_backtest_artifact(
    tradability_artifact: Path,
    capacity_path: Path,
    targets_path: Path,
    output_root: Path,
    *,
    initial_cash: float,
    corporate_actions_path: Optional[Path] = None,
    initial_positions_path: Optional[Path] = None,
    backtest_spec: Optional[BacktestSpec] = None,
    execution_spec: Optional[ExecutionSpec] = None,
    fees: Optional[FeePolicy] = None,
    require_clean_git: bool = False,
) -> Path:
    """Run and freeze a promoted P0.5-backed multi-scenario portfolio backtest."""
    bt_spec = (backtest_spec or BacktestSpec()).validate()
    exec_spec = (execution_spec or ExecutionSpec()).validate()
    fee_policy = fees or FeePolicy()
    p05_root = Path(tradability_artifact)
    p05_manifest_path = p05_root / "manifest.json"
    p05_parquet_path = p05_root / "tradability.parquet"
    if not p05_manifest_path.exists() or not p05_parquet_path.exists():
        raise ExecutionError("P0.5 artifact is incomplete")
    p05_manifest = json.loads(p05_manifest_path.read_text(encoding="utf-8"))
    if not p05_manifest.get("quality", {}).get("promotion_passed", False):
        raise ExecutionError("P0.5 artifact did not pass promotion")
    p05_sha = _sha256(p05_parquet_path)
    if p05_sha != p05_manifest.get("output", {}).get("sha256"):
        raise ExecutionError("P0.5 Parquet SHA-256 does not match manifest")

    capacity_file = Path(capacity_path)
    targets_file = Path(targets_path)
    actions_file = Path(corporate_actions_path) if corporate_actions_path else None
    positions_file = Path(initial_positions_path) if initial_positions_path else None
    if require_clean_git and actions_file is None:
        raise ExecutionError(
            "formal raw-price backtests require a promoted corporate-action artifact"
        )
    corporate_action_identity = _validate_corporate_action_input(
        actions_file,
        p05_manifest,
        _sha256(p05_manifest_path),
        require_full_query_coverage=require_clean_git,
    )
    tradability = pd.read_parquet(p05_parquet_path)
    capacity = _read_frame(capacity_file)
    targets = _read_frame(targets_file)
    actions = _read_frame(actions_file) if actions_file else None
    initial_positions = _read_frame(positions_file) if positions_file else None
    result = run_portfolio_backtest(
        targets,
        tradability,
        capacity,
        initial_cash=initial_cash,
        corporate_actions=actions,
        initial_positions=initial_positions,
        backtest_spec=bt_spec,
        execution_spec=exec_spec,
        fees=fee_policy,
    )
    quality = backtest_quality_summary(result, bt_spec, exec_spec)
    implementation = _implementation_identity()
    code_identity = None
    environment_lock = None
    try:
        git = inspect_git_repository(
            Path(__file__),
            require_clean=require_clean_git,
        )
        code_identity = git.to_dict()
        environment_lock = environment_lock_identity(Path(git.repository_root))
    except VersionControlError:
        if require_clean_git:
            raise
    identity = {
        "p05_artifact_id": p05_manifest["artifact_id"],
        "p05_manifest_sha256": _sha256(p05_manifest_path),
        "p05_parquet_sha256": p05_sha,
        "capacity_sha256": _sha256(capacity_file),
        "targets_sha256": _sha256(targets_file),
        "corporate_actions_sha256": _optional_sha(actions_file),
        "corporate_action_artifact": corporate_action_identity,
        "initial_positions_sha256": _optional_sha(positions_file),
        "initial_cash": float(initial_cash),
        "backtest_spec_sha256": bt_spec.fingerprint,
        "execution_spec_sha256": exec_spec.fingerprint,
        "fee_policy_sha256": fee_policy.fingerprint,
        "scenario_policy_sha256": hashlib.sha256(
            json.dumps([asdict(item) for item in DEFAULT_SCENARIOS], sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest(),
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
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / "backtests" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    outputs: Dict[str, Dict[str, Any]] = {}
    frames = {
        "daily_nav": result.daily_nav,
        "daily_positions": result.daily_positions,
        "stale_valuation_bounds": result.stale_valuation_bounds,
        "target_weights": result.target_weights,
        "orders": result.orders,
        "suppressed_orders": result.suppressed_orders,
        "executions": result.executions,
        "corporate_action_ledger": result.corporate_action_ledger,
        "capacity_history": result.capacity_history,
        "scenario_summary": result.scenario_summary,
    }
    sort_policies = {
        "daily_nav": ["scenario", "trade_date"],
        "daily_positions": ["scenario", "valuation_date", "instrument_id"],
        "stale_valuation_bounds": [
            "scenario",
            "trade_date",
            "valuation_scenario",
        ],
        "target_weights": ["scenario", "trade_date", "instrument_id"],
        "orders": ["scenario", "trade_date", "order_id"],
        "suppressed_orders": ["scenario", "trade_date", "order_id"],
        "executions": ["scenario", "trade_date", "order_id"],
        "corporate_action_ledger": [
            "scenario",
            "effective_date",
            "action_id",
            "processing_stage",
        ],
        "capacity_history": ["scenario", "trade_date"],
        "scenario_summary": ["scenario"],
    }
    for name, frame in frames.items():
        path = destination / f"{name}.parquet"
        sort_columns = [column for column in sort_policies[name] if column in frame]
        logical_sha = _frame_fingerprint(frame, sort_columns)
        _write_immutable_parquet(frame, path, sort_columns, logical_sha)
        outputs[name] = {
            "path": str(path),
            "rows": len(frame),
            "logical_sha256": logical_sha,
            "sha256": _sha256(path),
        }
    performance_path = destination / "performance_summary.json"
    performance = _json_records(result.scenario_summary)
    _write_immutable_json(performance, performance_path)
    outputs["performance_summary"] = {
        "path": str(performance_path),
        "rows": len(result.scenario_summary),
        "sha256": _sha256(performance_path),
    }
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": "p063_portfolio_backtest_v2",
        "identity": identity,
        "backtest_spec": {**asdict(bt_spec), "sha256": bt_spec.fingerprint},
        "execution_spec": {**asdict(exec_spec), "sha256": exec_spec.fingerprint},
        "fee_policy": {**asdict(fee_policy), "sha256": fee_policy.fingerprint},
        "execution_scenarios": [asdict(item) for item in DEFAULT_SCENARIOS],
        "quality": quality,
        "inputs": {
            "p05_manifest": str(p05_manifest_path),
            "p05_tradability": str(p05_parquet_path),
            "capacity": str(capacity_file),
            "targets": str(targets_file),
            "corporate_actions": str(actions_file) if actions_file else None,
            "initial_positions": str(positions_file) if positions_file else None,
        },
        "outputs": outputs,
        "guardrails": {
            "chronological_event_clock": True,
            "target_decision_precedes_execution": True,
            "capacity_is_lagged": True,
            "volatility_is_corporate_action_neutral_and_lagged": True,
            "square_root_impact_is_volatility_scaled": True,
            "impact_limit_reduces_fill_instead_of_capping_cost": True,
            "routine_small_orders_are_audited": True,
            "unfilled_orders_cancelled_and_rebuilt": True,
            "raw_prices_for_execution_and_valuation": True,
            "stale_valuation_session_limit_remains_frozen": True,
            "stale_last_close_is_upper_valuation_bound": True,
            "stale_zero_is_conservative_lower_valuation_bound": True,
            "stale_valuation_two_sided_bound_required": True,
            "stale_valuation_bound_tolerance_blocks_promotion": True,
            "p05_delisting_zero_recovery_terminal_writeoff": True,
            "fractional_share_entitlements_use_conservative_floor": True,
            "cash_dividend_record_ex_pay_separation": True,
            "dividend_receivables_in_nav": True,
            "independent_scenario_ledgers": True,
            "p05_promotion_required": True,
            "no_short_selling": True,
            "formal_cli_requires_clean_git": True,
            "git_commit_bound": code_identity is not None,
            "environment_lock_bound": environment_lock is not None,
        },
        "implementation": implementation,
        "code_identity": code_identity,
        "environment_lock": environment_lock,
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    if not quality["promotion_passed"]:
        raise ExecutionError(
            f"P0.6.3 artifact failed promotion at {destination}: {quality['hard_failures']}"
        )
    return destination


def _validate_corporate_action_input(
    actions_file: Optional[Path],
    p05_manifest: Dict[str, Any],
    p05_manifest_sha256: str,
    *,
    require_full_query_coverage: bool,
) -> Optional[Dict[str, Any]]:
    """Bind formal raw-price accounting to its promoted full-universe action proof."""
    if actions_file is None:
        return None
    manifest_path = actions_file.parent / "manifest.json"
    if not manifest_path.exists():
        if require_full_query_coverage:
            raise ExecutionError(
                "formal corporate actions require a sibling immutable manifest"
            )
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if _sha256(actions_file) != manifest.get("output", {}).get("sha256"):
        raise ExecutionError("corporate-action Parquet SHA-256 does not match manifest")
    if not manifest.get("quality", {}).get("promotion_passed", False):
        raise ExecutionError("corporate-action artifact did not pass promotion")
    source = manifest.get("identity", {})
    if (
        source.get("p05_artifact_id") != p05_manifest.get("artifact_id")
        or source.get("p05_manifest_sha256") != p05_manifest_sha256
    ):
        raise ExecutionError("corporate-action artifact is bound to a different P0.5")
    if require_full_query_coverage:
        quality = manifest.get("quality", {})
        if (
            not manifest.get("guardrails", {}).get(
                "full_universe_query_coverage_proven", False
            )
            or quality.get("query_coverage") != 1.0
            or quality.get("hard_failures", {}).get("unqueried_p05_symbols", 0) != 0
        ):
            raise ExecutionError(
                "formal corporate actions lack full P0.5 query-coverage proof"
            )
    return {
        "artifact_id": manifest.get("artifact_id"),
        "manifest_sha256": _sha256(manifest_path),
        "query_coverage": manifest.get("quality", {}).get("query_coverage"),
        "full_universe_query_coverage_proven": manifest.get("guardrails", {}).get(
            "full_universe_query_coverage_proven", False
        ),
    }


def backtest_quality_summary(
    result: BacktestResult, backtest_spec: BacktestSpec, execution_spec: ExecutionSpec
) -> Dict[str, Any]:
    nav = result.daily_nav
    positions = result.daily_positions
    executions = result.executions
    target = result.target_weights
    duplicate_nav = nav.duplicated(["scenario", "trade_date"])
    if positions.empty:
        position_values = pd.DataFrame(
            columns=["scenario", "trade_date", "position_market_value"]
        )
    else:
        position_values = (
            positions.groupby(["scenario", "valuation_date"], observed=True)[
                "market_value"
            ]
            .sum()
            .rename("position_market_value")
            .reset_index()
            .rename(columns={"valuation_date": "trade_date"})
        )
    tie = nav.merge(position_values, on=["scenario", "trade_date"], how="left")
    tie["position_market_value"] = pd.to_numeric(
        tie["position_market_value"], errors="coerce"
    ).fillna(0.0)
    receivable = tie.get(
        "dividend_receivable", pd.Series(0.0, index=tie.index, dtype=float)
    ).fillna(0.0)
    nav_tie_error = (
        tie["nav"] - tie["cash"] - tie["position_market_value"] - receivable
    ).abs()
    target_sums = target.groupby(["scenario", "trade_date"], observed=True)[
        "target_weight"
    ].sum()
    stale_sessions = pd.to_numeric(
        positions.get("stale_sessions", pd.Series(dtype=float)), errors="coerce"
    )
    positive_quantity = pd.to_numeric(
        positions.get("total_quantity", pd.Series(dtype=float)), errors="coerce"
    ).gt(0)
    stale_breach = (
        stale_sessions > backtest_spec.max_stale_valuation_sessions
    ) & positive_quantity
    expected_stale_bounds = build_stale_valuation_bounds(
        nav,
        positions,
        backtest_spec,
    )
    stale_bound_not_reported = _stale_valuation_bound_not_reported(
        result.stale_valuation_bounds,
        expected_stale_bounds,
    )
    stale_bound_width = (
        expected_stale_bounds.loc[
            expected_stale_bounds["valuation_scenario"] == "stale_at_last_close"
        ]
        .loc[:, ["scenario", "trade_date", "bound_width_pp"]]
        .copy()
    )
    stale_bound_exceeds = int(
        (
            pd.to_numeric(stale_bound_width["bound_width_pp"], errors="coerce")
            > backtest_spec.max_stale_valuation_nav_bound_pp + 1e-12
        ).sum()
    )
    stale_bound_by_scenario = {
        str(name): float(value)
        for name, value in stale_bound_width.groupby("scenario", observed=True)[
            "bound_width_pp"
        ].max().items()
    }
    participation_limits = {
        scenario.name: (
            scenario.max_participation_rate
            if scenario.max_participation_rate is not None
            else execution_spec.max_participation_rate
        )
        for scenario in DEFAULT_SCENARIOS
    }
    execution_participation_limit = (
        executions["scenario"].map(participation_limits)
        if not executions.empty
        else pd.Series(dtype=float)
    )
    impact_limits = {
        scenario.name: (
            scenario.max_executable_impact_bps
            if scenario.max_executable_impact_bps is not None
            else execution_spec.max_executable_impact_bps
        )
        for scenario in DEFAULT_SCENARIOS
    }
    execution_impact_limit = (
        executions["scenario"].map(impact_limits)
        if not executions.empty
        else pd.Series(dtype=float)
    )
    hard_failures = {
        "duplicate_scenario_date_nav_rows": int(duplicate_nav.sum()),
        "unknown_execution_scenario_rows": int(
            execution_participation_limit.isna().sum()
        ),
        "negative_cash_rows": int((nav["cash"] < -1e-6).sum()),
        "negative_nav_rows": int((nav["nav"] < -1e-6).sum()),
        "nav_accounting_tie_failure_rows": int((nav_tie_error > 1e-6).sum()),
        "negative_position_rows": int(
            (
                positions.get("total_quantity", pd.Series(dtype=float)) < 0
            ).sum()
        ),
        "stale_valuation_bound_not_reported": stale_bound_not_reported,
        "stale_valuation_bound_exceeds_tolerance": stale_bound_exceeds,
        "target_cash_buffer_breach_rows": int(
            (
                target_sums
                > 1.0
                - backtest_spec.cash_buffer_fraction
                + backtest_spec.target_weight_tolerance
            ).sum()
        ),
        "execution_amount_participation_breach_rows": int(
            (
                executions.get("order_amount_participation_rate", pd.Series(dtype=float))
                > execution_participation_limit + 1e-7
            ).sum()
        ),
        "execution_free_float_breach_rows": int(
            (
                executions.get("projected_free_float_fraction", pd.Series(dtype=float))
                > execution_spec.max_position_free_float_fraction + 1e-7
            ).sum()
        ),
        "execution_stress_exit_breach_rows": int(
            (
                (executions.get("side", pd.Series(dtype=str)) == "buy")
                & executions.get("status", pd.Series(dtype=str)).isin(["filled", "partial"])
                & (
                    executions.get("stress_exit_days", pd.Series(dtype=float))
                    > execution_spec.max_stress_exit_days + 1e-7
                )
            ).sum()
        ),
        "execution_impact_tolerance_breach_rows": int(
            (
                executions.get("impact_bps", pd.Series(dtype=float))
                > execution_impact_limit + 1e-7
            ).sum()
        ),
        "execution_missing_volatility_rows": int(
            (
                executions.get(
                    "volatility20_daily_lag1", pd.Series(dtype=float)
                ).isna()
                & executions.get("status", pd.Series(dtype=str)).isin(
                    ["filled", "partial"]
                )
            ).sum()
        ),
        "suppressed_full_exit_rows": int(
            (
                result.suppressed_orders.get(
                    "order_reason", pd.Series(dtype=str)
                )
                == "full_exit"
            ).sum()
        ),
    }
    return {
        "scenarios": int(nav["scenario"].nunique()),
        "trading_sessions": int(nav["trade_date"].nunique()),
        "start_date": str(nav["trade_date"].min().date()),
        "end_date": str(nav["trade_date"].max().date()),
        "orders": len(result.orders),
        "executions": len(executions),
        "suppressed_orders": len(result.suppressed_orders),
        "corporate_action_events": len(result.corporate_action_ledger),
        "terminal_zero_recovery_writeoff_events": int(
            result.corporate_action_ledger.get(
                "action_type", pd.Series(dtype=str)
            ).eq("delisting_cash_settlement").sum()
        ),
        "terminal_zero_recovery_position_writeoffs": int(
            (
                result.corporate_action_ledger.get(
                    "action_type", pd.Series(dtype=str)
                ).eq("delisting_cash_settlement")
                & pd.to_numeric(
                    result.corporate_action_ledger.get(
                        "quantity_before", pd.Series(dtype=float)
                    ),
                    errors="coerce",
                ).gt(0)
            ).sum()
        ),
        "fractional_share_events": int(
            pd.to_numeric(
                result.corporate_action_ledger.get(
                    "fractional_total_discarded", pd.Series(dtype=float)
                ),
                errors="coerce",
            ).gt(0).sum()
        ),
        "fractional_shares_discarded": float(
            pd.to_numeric(
                result.corporate_action_ledger.get(
                    "fractional_total_discarded", pd.Series(dtype=float)
                ),
                errors="coerce",
            ).sum()
        ),
        "maximum_observed_stale_valuation_sessions": int(
            stale_sessions.max() if not stale_sessions.empty else 0
        ),
        "stale_valuation_breach_instruments": int(
            positions.loc[stale_breach, "instrument_id"].nunique()
            if not positions.empty and stale_breach.any()
            else 0
        ),
        "stale_valuation_breach_rows": int(stale_breach.sum()),
        "stale_valuation_nav_bound_pp": float(
            stale_bound_width["bound_width_pp"].max()
            if not stale_bound_width.empty
            else 0.0
        ),
        "stale_valuation_base_open_nav_bound_pp": float(
            stale_bound_by_scenario.get("base_open", 0.0)
        ),
        "stale_valuation_nav_bound_pp_by_scenario": stale_bound_by_scenario,
        "max_stale_valuation_nav_bound_pp": float(
            backtest_spec.max_stale_valuation_nav_bound_pp
        ),
        "hard_failures": hard_failures,
        "promotion_passed": all(value == 0 for value in hard_failures.values()),
    }


def _stale_valuation_bound_not_reported(
    actual: pd.DataFrame,
    expected: pd.DataFrame,
) -> int:
    """Count missing, extra, duplicate, or numerically inconsistent bound rows."""
    keys = ["scenario", "trade_date", "valuation_scenario"]
    values = [
        "bounded_nav",
        "stale_market_value_adjustment",
        "stale_market_value_at_last_close",
        "stale_position_rows",
        "stale_instruments",
        "bound_width_amount",
        "bound_width_pp",
    ]
    required = set(keys + values)
    if actual.empty or not required.issubset(actual.columns):
        return max(1, len(expected))

    observed = actual.loc[:, keys + values].copy()
    reference = expected.loc[:, keys + values].copy()
    observed["trade_date"] = pd.to_datetime(
        observed["trade_date"], errors="coerce"
    ).dt.normalize()
    reference["trade_date"] = pd.to_datetime(
        reference["trade_date"], errors="coerce"
    ).dt.normalize()
    duplicate_rows = int(observed.duplicated(keys).sum())
    observed_unique = observed.drop_duplicates(keys, keep="first")
    key_audit = reference.loc[:, keys].merge(
        observed_unique.loc[:, keys],
        on=keys,
        how="outer",
        indicator=True,
    )
    missing_or_extra = int(key_audit["_merge"].ne("both").sum())
    comparison = reference.merge(
        observed_unique,
        on=keys,
        how="inner",
        suffixes=("_expected", "_actual"),
        validate="one_to_one",
    )
    mismatch = pd.Series(False, index=comparison.index)
    for column in values:
        expected_value = pd.to_numeric(
            comparison[f"{column}_expected"], errors="coerce"
        )
        actual_value = pd.to_numeric(
            comparison[f"{column}_actual"], errors="coerce"
        )
        close = np.isclose(
            expected_value,
            actual_value,
            rtol=1e-12,
            atol=1e-8,
            equal_nan=False,
        )
        mismatch |= ~pd.Series(close, index=comparison.index)
    return duplicate_rows + missing_or_extra + int(mismatch.sum())


def _read_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise ExecutionError(f"input does not exist: {path}")
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ExecutionError(f"unsupported input format: {path}")


def _implementation_identity() -> Dict[str, Any]:
    here = Path(__file__)
    paths = [
        here,
        here.with_name("engine.py"),
        here.parents[1] / "execution" / "daily.py",
        here.parents[1] / "execution" / "capacity.py",
        here.parents[1] / "execution" / "portfolio.py",
        here.parents[1] / "execution" / "scenarios.py",
    ]
    root = here.resolve().parents[3]
    entries = []
    tree = hashlib.sha256()
    for path in sorted(paths):
        relative = str(path.resolve().relative_to(root))
        digest = _sha256(path)
        entries.append({"path": relative, "sha256": digest})
        tree.update(relative.encode("utf-8"))
        tree.update(digest.encode("ascii"))
    return {"tree_sha256": tree.hexdigest(), "files": entries}


def _frame_fingerprint(frame: pd.DataFrame, sort_columns: list[str]) -> str:
    normalized = frame.sort_values(sort_columns).reset_index(drop=True) if sort_columns else frame
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def _write_immutable_parquet(
    frame: pd.DataFrame, path: Path, sort_columns: list[str], logical_sha: str
) -> None:
    if path.exists():
        if _frame_fingerprint(pd.read_parquet(path), sort_columns) != logical_sha:
            raise ExecutionError(f"immutable backtest output conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, path)


def _write_immutable_json(payload: Any, path: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise ExecutionError(f"immutable backtest JSON conflict: {path}")
    if not path.exists():
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)


def _json_records(frame: pd.DataFrame) -> list[Dict[str, Any]]:
    records = []
    for row in frame.to_dict("records"):
        records.append(
            {
                key: (
                    value.isoformat()
                    if isinstance(value, pd.Timestamp)
                    else None
                    if isinstance(value, float) and (np.isnan(value) or np.isinf(value))
                    else value
                )
                for key, value in row.items()
            }
        )
    return records


def _optional_sha(path: Optional[Path]) -> Optional[str]:
    return _sha256(path) if path else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
