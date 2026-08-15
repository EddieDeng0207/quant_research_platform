"""Immutable P0.6 execution artifact construction."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from .daily import ExecutionError, ExecutionSpec, FeePolicy, simulate_orders
from .scenarios import DEFAULT_SCENARIOS, simulate_execution_scenarios


def build_execution_artifact(
    tradability_artifact: Path,
    orders_path: Path,
    output_root: Path,
    initial_cash: float,
    spec: Optional[ExecutionSpec] = None,
    fees: Optional[FeePolicy] = None,
) -> Path:
    """Execute a frozen order blotter against a promoted P0.5 artifact."""
    spec = (spec or ExecutionSpec()).validate()
    fees = fees or FeePolicy()
    p05_root = Path(tradability_artifact)
    p05_manifest_path = p05_root / "manifest.json"
    if not p05_manifest_path.exists():
        raise ExecutionError(f"P0.5 manifest does not exist: {p05_manifest_path}")
    p05_manifest = json.loads(p05_manifest_path.read_text(encoding="utf-8"))
    if not p05_manifest.get("quality", {}).get("promotion_passed", False):
        raise ExecutionError("P0.5 artifact did not pass promotion")
    p05_parquet = p05_root / "tradability.parquet"
    expected_p05_sha = p05_manifest.get("output", {}).get("sha256")
    actual_p05_sha = _sha256(p05_parquet)
    if actual_p05_sha != expected_p05_sha:
        raise ExecutionError("P0.5 Parquet SHA-256 does not match its manifest")

    order_file = Path(orders_path)
    orders = _read_orders(order_file)
    tradability = pd.read_parquet(p05_parquet)
    executions, ledger = simulate_orders(
        orders,
        tradability,
        initial_cash=initial_cash,
        spec=spec,
        fees=fees,
    )
    scenario_executions, scenario_summary = simulate_execution_scenarios(
        orders,
        tradability,
        initial_cash=initial_cash,
        spec=spec,
        fees=fees,
    )
    positions = ledger.snapshot()
    quality = execution_quality_summary(
        executions,
        positions,
        ledger.cash,
        spec=spec,
        fees=fees,
    )
    implementation = _implementation_identity()
    identity = {
        "p05_artifact_id": p05_manifest["artifact_id"],
        "p05_manifest_sha256": _sha256(p05_manifest_path),
        "p05_parquet_sha256": actual_p05_sha,
        "orders_sha256": _sha256(order_file),
        "initial_cash": float(initial_cash),
        "execution_spec_sha256": spec.fingerprint,
        "fee_policy_sha256": fees.fingerprint,
        "scenario_policy_sha256": hashlib.sha256(
            json.dumps([asdict(item) for item in DEFAULT_SCENARIOS], sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest(),
        "implementation_sha256": implementation["tree_sha256"],
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / "execution" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    executions_path = destination / "executions.parquet"
    positions_path = destination / "ending_positions.parquet"
    scenario_executions_path = destination / "scenario_executions.parquet"
    scenario_summary_path = destination / "scenario_summary.parquet"
    execution_logical_sha = _frame_fingerprint(executions, ["trade_date", "order_id"])
    position_logical_sha = _frame_fingerprint(
        positions, ["instrument_id"] if not positions.empty else []
    )
    _write_immutable_parquet(executions, executions_path, execution_logical_sha)
    _write_immutable_parquet(positions, positions_path, position_logical_sha)
    scenario_execution_sha = _frame_fingerprint(
        scenario_executions, ["scenario", "trade_date", "order_id"]
    )
    scenario_summary_sha = _frame_fingerprint(scenario_summary, ["scenario"])
    _write_immutable_parquet(
        scenario_executions, scenario_executions_path, scenario_execution_sha
    )
    _write_immutable_parquet(scenario_summary, scenario_summary_path, scenario_summary_sha)
    manifest = {
        "artifact_id": artifact_id,
        "schema_version": "p063_execution_v1",
        "identity": identity,
        "execution_spec": {**spec.__dict__, "sha256": spec.fingerprint},
        "fee_policy": {**fees.__dict__, "sha256": fees.fingerprint},
        "execution_scenarios": [asdict(item) for item in DEFAULT_SCENARIOS],
        "quality": quality,
        "inputs": {
            "p05_manifest": {
                "path": str(p05_manifest_path),
                "sha256": identity["p05_manifest_sha256"],
            },
            "p05_tradability": {
                "path": str(p05_parquet),
                "sha256": actual_p05_sha,
            },
            "orders": {
                "path": str(order_file),
                "sha256": identity["orders_sha256"],
                "rows": len(orders),
            },
        },
        "outputs": {
            "executions": {
                "path": str(executions_path),
                "rows": len(executions),
                "logical_sha256": execution_logical_sha,
                "sha256": _sha256(executions_path),
            },
            "ending_positions": {
                "path": str(positions_path),
                "rows": len(positions),
                "logical_sha256": position_logical_sha,
                "sha256": _sha256(positions_path),
            },
            "scenario_executions": {
                "path": str(scenario_executions_path),
                "rows": len(scenario_executions),
                "logical_sha256": scenario_execution_sha,
                "sha256": _sha256(scenario_executions_path),
            },
            "scenario_summary": {
                "path": str(scenario_summary_path),
                "rows": len(scenario_summary),
                "logical_sha256": scenario_summary_sha,
                "sha256": _sha256(scenario_summary_path),
            },
        },
        "guardrails": {
            "p05_promotion_required": True,
            "t_plus_one_enforced": spec.enforce_t_plus_one,
            "liquidity_must_be_lagged": spec.require_lagged_liquidity,
            "current_day_volume_for_order_sizing_forbidden": True,
            "institutional_capacity_inputs_required": spec.require_institutional_capacity_inputs,
            "lagged_volatility_required": True,
            "volatility_scaled_square_root_impact": True,
            "impact_tolerance_reduces_fill": True,
            "default_participation_rate": spec.max_participation_rate,
            "free_float_and_exit_day_constraints": True,
            "stress_and_delay_scenarios_retained": True,
            "no_short_selling": True,
            "rejections_are_retained": True,
        },
        "implementation": implementation,
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    if not quality["promotion_passed"]:
        raise ExecutionError(
            f"P0.6 execution artifact failed structural promotion at {destination}: "
            f"{quality['hard_failures']}"
        )
    return destination


def execution_quality_summary(
    executions: pd.DataFrame,
    positions: pd.DataFrame,
    ending_cash: float,
    spec: Optional[ExecutionSpec] = None,
    fees: Optional[FeePolicy] = None,
) -> Dict[str, Any]:
    spec = spec or ExecutionSpec()
    fees = fees or FeePolicy()
    hard_failures = {
        "negative_cash_rows": int((executions["cash_after"] < -1e-6).sum()),
        "negative_position_rows": int((executions["position_after"] < 0).sum()),
        "negative_fee_rows": int((executions["total_fees"] < 0).sum()),
        "filled_quantity_exceeds_submitted_rows": int(
            (executions["filled_quantity"] > executions["submitted_quantity"]).sum()
        ),
        "ending_negative_positions": int(
            (positions.get("total_quantity", pd.Series(dtype=float)) < 0).sum()
        ),
        "participation_above_policy_rows": int(
            (
                executions["order_amount_participation_rate"]
                > spec.max_participation_rate + 1e-7
            ).sum()
        ),
        "free_float_above_policy_rows": int(
            (
                executions["projected_free_float_fraction"]
                > spec.max_position_free_float_fraction + 1e-7
            ).sum()
        ),
        "stress_exit_days_above_policy_rows": int(
            (
                (executions["side"] == "buy")
                & executions["status"].isin(["filled", "partial"])
                & (executions["stress_exit_days"] > spec.max_stress_exit_days + 1e-7)
            ).sum()
        ),
        "impact_above_tolerance_rows": int(
            (
                executions["impact_bps"]
                > spec.max_executable_impact_bps + 1e-7
            ).sum()
        ),
        "filled_missing_volatility_rows": int(
            (
                executions["status"].isin(["filled", "partial"])
                & executions["volatility20_daily_lag1"].isna()
            ).sum()
        ),
    }
    rejected = executions.loc[executions["status"] == "rejected", "block_reason"]
    filled = executions.loc[
        executions["status"].isin(["filled", "partial"])
        & (executions["notional"] > 0)
    ]
    turnover = float(filled["notional"].sum())
    commission = float(filled["commission"].sum())
    minimum_commission_orders = int(
        (filled["commission"] <= fees.minimum_commission_cny + 1e-9).sum()
    )
    return {
        "orders": len(executions),
        "filled_orders": int((executions["status"] == "filled").sum()),
        "partial_orders": int((executions["status"] == "partial").sum()),
        "rejected_orders": int((executions["status"] == "rejected").sum()),
        "rejection_reasons": rejected.value_counts().sort_index().to_dict(),
        "requested_shares": int(executions["quantity"].sum()),
        "filled_shares": int(executions["filled_quantity"].sum()),
        "turnover_notional": float(executions["notional"].sum()),
        "total_fees": float(executions["total_fees"].sum()),
        "total_slippage_cost": float(executions["slippage_cost"].sum()),
        "minimum_commission_orders": minimum_commission_orders,
        "minimum_commission_hit_rate": (
            minimum_commission_orders / len(filled) if len(filled) else None
        ),
        "effective_commission_bps": (
            commission / turnover * 10_000.0 if turnover > 0 else None
        ),
        "max_amount_participation_rate": float(
            executions["order_amount_participation_rate"].max(skipna=True)
        ),
        "max_projected_free_float_fraction": float(
            executions["projected_free_float_fraction"].max(skipna=True)
        ),
        "max_stress_exit_days": float(executions["stress_exit_days"].max(skipna=True)),
        "ending_cash": float(ending_cash),
        "ending_positions": len(positions),
        "hard_failures": hard_failures,
        "promotion_passed": all(value == 0 for value in hard_failures.values()),
    }


def _read_orders(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise ExecutionError(f"Order blotter does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    raise ExecutionError("Order blotter must be CSV or Parquet")


def _frame_fingerprint(frame: pd.DataFrame, sort_columns: list[str]) -> str:
    normalized = frame.sort_values(sort_columns).reset_index(drop=True) if sort_columns else frame
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def _implementation_identity() -> Dict[str, Any]:
    paths = [
        Path(__file__),
        Path(__file__).with_name("daily.py"),
        Path(__file__).with_name("capacity.py"),
        Path(__file__).with_name("portfolio.py"),
        Path(__file__).with_name("scenarios.py"),
    ]
    root = Path(__file__).resolve().parents[3]
    entries = []
    tree = hashlib.sha256()
    for path in sorted(paths):
        relative = str(path.resolve().relative_to(root))
        digest = _sha256(path)
        entries.append({"path": relative, "sha256": digest})
        tree.update(relative.encode("utf-8"))
        tree.update(digest.encode("ascii"))
    return {"tree_sha256": tree.hexdigest(), "files": entries}


def _write_immutable_parquet(frame: pd.DataFrame, path: Path, logical_sha: str) -> None:
    if path.exists():
        existing = pd.read_parquet(path)
        existing_sha = _frame_fingerprint(
            existing,
            [
                column
                for column in ["scenario", "trade_date", "order_id", "instrument_id"]
                if column in existing
            ],
        )
        if existing_sha != logical_sha:
            raise ExecutionError(f"Immutable execution output conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, path)


def _write_immutable_json(payload: Dict[str, Any], path: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ExecutionError(f"Immutable execution manifest conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
