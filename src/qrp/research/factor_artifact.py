"""Immutable artifacts and verified reports for P0.7 factor evaluation."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from qrp.versioning import (
    VersionControlError,
    environment_lock_identity,
    inspect_git_repository,
)

from .factor_evaluation import (
    FactorEvaluationError,
    FactorEvaluationSpec,
    evaluate_single_factor,
)


def build_factor_evaluation_artifact(
    observations_path: Path,
    forward_returns_path: Path,
    output_root: Path,
    *,
    spec: FactorEvaluationSpec,
    require_clean_git: bool = False,
    strict: bool = True,
) -> Path:
    """Build a content-addressed, immutable single-factor research artifact."""
    frozen = spec.validate()
    observation_file = Path(observations_path)
    return_file = Path(forward_returns_path)
    observations = _read_frame(observation_file)
    forward_returns = _read_frame(return_file)
    result = evaluate_single_factor(observations, forward_returns, frozen)
    implementation = _implementation_identity()
    code_identity: Optional[Dict[str, Any]] = None
    environment_lock: Optional[Dict[str, Any]] = None
    try:
        git = inspect_git_repository(Path(__file__), require_clean=require_clean_git)
        code_identity = git.to_dict()
        environment_lock = environment_lock_identity(Path(git.repository_root))
    except VersionControlError:
        if require_clean_git:
            raise
    identity = {
        "observations_sha256": _sha256(observation_file),
        "forward_returns_sha256": _sha256(return_file),
        "spec_sha256": frozen.fingerprint,
        "implementation_sha256": implementation["tree_sha256"],
        "git_commit": code_identity["commit"] if code_identity else None,
        "git_tree": code_identity["tree"] if code_identity else None,
        "git_dirty_state_sha256": (code_identity["dirty_state_sha256"] if code_identity else None),
        "environment_lock_sha256": (environment_lock["sha256"] if environment_lock else None),
    }
    artifact_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    destination = Path(output_root) / "factor_evaluations" / f"artifact_id={artifact_id}"
    destination.mkdir(parents=True, exist_ok=True)
    frames = {
        "factor_panel": result.factor_panel,
        "coverage": result.coverage,
        "distribution": result.distribution,
        "factor_exposures": result.factor_exposures,
        "label_coverage": result.label_coverage,
        "ic_series": result.ic_series,
        "quantile_returns": result.quantile_returns,
        "turnover": result.turnover,
        "target_weights": result.target_weights,
        "horizon_summary": result.horizon_summary,
        "annual_summary": result.annual_summary,
    }
    sort_policies = {
        "factor_panel": ["decision_at", "instrument_id"],
        "coverage": ["decision_at"],
        "distribution": ["decision_at"],
        "factor_exposures": [
            "decision_at",
            "scope",
            "quantile",
            "exposure_type",
            "exposure_name",
        ],
        "label_coverage": ["horizon_sessions"],
        "ic_series": ["decision_at", "horizon_sessions"],
        "quantile_returns": ["decision_at", "horizon_sessions", "quantile"],
        "turnover": ["decision_at"],
        "target_weights": ["decision_at", "instrument_id"],
        "horizon_summary": ["horizon_sessions"],
        "annual_summary": ["year", "horizon_sessions"],
    }
    outputs: Dict[str, Dict[str, Any]] = {}
    for name, frame in frames.items():
        path = destination / f"{name}.parquet"
        logical_sha = _frame_fingerprint(frame, sort_policies[name])
        _write_immutable_parquet(frame, path, sort_policies[name], logical_sha)
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
        "factor_spec": {**asdict(frozen), "sha256": frozen.fingerprint},
        "inputs": {
            "observations": str(observation_file.resolve()),
            "forward_returns": str(return_file.resolve()),
        },
        "outputs": outputs,
        "quality": result.quality,
        "guardrails": {
            "factor_and_outcome_inputs_separated": True,
            "point_in_time_contract_validated": True,
            "decision_precedes_execution": True,
            "outcome_starts_no_earlier_than_execution": True,
            "cross_sections_processed_independently": True,
            "mad_winsorization": True,
            "industry_and_log_size_neutralization": True,
            "first_order_conditions_are_numerical_sanity_checks_only": True,
            "top_bottom_portfolio_exposure_limits": True,
            "sampling_noise_aware_portfolio_exposure_limits": True,
            "quantile_assignment": frozen.quantile_assignment,
            "industry_size_strata": (
                frozen.size_strata
                if frozen.quantile_assignment == "industry_size_stratified"
                else None
            ),
            "structural_label_tail_excluded_by_horizon": True,
            "decision_frequency_inferred_and_validated": True,
            "horizon_specific_newey_west_lags": True,
            "hac_adjusted_annualized_icir": True,
            "raw_and_residualized_return_diagnostics": True,
            "long_only_targets_are_diagnostic_until_p063_backtest": True,
            "formal_cli_requires_clean_git": True,
            "git_commit_bound": code_identity is not None,
            "environment_lock_bound": environment_lock is not None,
        },
        "implementation": implementation,
        "code_identity": code_identity,
        "environment_lock": environment_lock,
    }
    _write_immutable_json(manifest, destination / "manifest.json")
    if strict and not result.quality["promotion_passed"]:
        raise FactorEvaluationError(
            f"P0.7 artifact failed promotion at {destination}: {result.quality['hard_failures']}"
        )
    return destination


def generate_factor_evaluation_report(artifact: Path, output: Path) -> Path:
    """Generate a Markdown report only after verifying every frozen output hash."""
    root = Path(artifact)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise FactorEvaluationError(f"factor manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in {
        "p07_single_factor_evaluation_v3",
        "p07_single_factor_evaluation_v4",
        "p07_single_factor_evaluation_v5",
    }:
        raise FactorEvaluationError("report input is not a P0.7 factor artifact")
    for name, metadata in manifest["outputs"].items():
        path = root / metadata["path"]
        if not path.exists() or _sha256(path) != metadata["sha256"]:
            raise FactorEvaluationError(f"factor output hash mismatch: {name}")
    horizons = pd.read_parquet(root / "horizon_summary.parquet").sort_values("horizon_sessions")
    annual = pd.read_parquet(root / "annual_summary.parquet").sort_values(
        ["year", "horizon_sessions"]
    )
    turnover = pd.read_parquet(root / "turnover.parquet")
    label_coverage = pd.read_parquet(root / "label_coverage.parquet").sort_values(
        "horizon_sessions"
    )
    quality = manifest["quality"]
    spec = manifest["factor_spec"]
    identity = manifest["identity"]
    lines = [
        f"# P0.7 单因子评测报告：{spec['factor_name']}",
        "",
        "## 结论",
        "",
        (
            f"产物 `{manifest['artifact_id']}` "
            + ("通过" if quality["promotion_passed"] else "未通过")
            + " P0.7 数据与工程门禁。门禁通过不等于因子具有投资价值。"
        ),
        "",
        "## 冻结身份",
        "",
        f"- 因子族：`{spec['factor_family']}`",
        f"- 方向：`{spec['expected_direction']}`",
        f"- 分组方法：`{spec.get('quantile_assignment', 'global')}`",
        f"- 市值分层数：`{spec.get('size_strata', '不适用')}`",
        f"- 主报告收益口径：`{spec['return_basis']}`",
        f"- 参数 SHA-256：`{identity['spec_sha256']}`",
        f"- 观测输入 SHA-256：`{identity['observations_sha256']}`",
        f"- 收益标签 SHA-256：`{identity['forward_returns_sha256']}`",
        f"- Git commit：`{identity['git_commit']}`",
        f"- Git tree：`{identity['git_tree']}`",
        f"- 依赖锁 SHA-256：`{identity['environment_lock_sha256']}`",
        "",
        "## 数据质量",
        "",
        f"- 决策日/成功处理日：{quality['decision_dates']}/{quality['prepared_dates']}",
        f"- 有效因子行：{quality['prepared_rows']}",
        f"- 覆盖率中位数/最小值：{_pct(quality['median_coverage'])}/{_pct(quality['minimum_coverage_observed'])}",
        f"- 非结构性未来收益标签匹配率：{_pct(quality['label_match_rate'])}",
        f"- 结构性样本末端标签行：{quality['structural_tail_label_rows']}",
        f"- 样本内部意外缺失标签行：{quality['unexpected_missing_label_rows']}",
        f"- 推断年频/冻结年频：{quality['decision_frequency']['inferred_periods_per_year']:.2f}/{spec['annualization_periods']}",
        f"- 推断每次决策间隔交易日：{quality['decision_frequency']['trading_sessions_per_period']:.0f}",
        f"- Top/Bottom 最大行业主动权重：{_pct(quality['maximum_top_bottom_industry_active_weight'])}",
        f"- Top/Bottom 最大对数市值标准分偏离：{_number(quality['maximum_top_bottom_log_market_cap_z'])}",
        f"- Top/Bottom 最大行业抽样标准误倍数：{_number(quality['maximum_top_bottom_industry_sampling_sigma'])}",
        f"- Top/Bottom 最大市值抽样标准误倍数：{_number(quality['maximum_top_bottom_size_sampling_sigma'])}",
        f"- 暴露阈值：行业下限 {_pct(spec['industry_active_weight_floor'])}、市值标准分下限 {_number(spec['log_market_cap_z_floor'])}、抽样误差 {spec['exposure_sampling_sigma_multiplier']:.1f}σ",
        f"- 收益残差化失败期：{quality['residualization_failure_periods']}",
        "",
        "## 分持有期标签覆盖",
        "",
        "| 持有期 | 理论行 | 结构性尾部 | 可评测行 | 成功匹配 | 内部缺失 | 匹配率 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in label_coverage.to_dict("records"):
        lines.append(
            f"| {int(row['horizon_sessions'])} | {int(row['expected_rows'])} | "
            f"{int(row['structural_tail_rows'])} | "
            f"{int(row['structurally_available_rows'])} | {int(row['matched_rows'])} | "
            f"{int(row['unexpected_missing_rows'])} | {_pct(row['match_rate'])} |"
        )
    lines.extend(
        [
            "",
            "## 不同持有期结果",
            "",
            "| 持有期(交易日) | NW阶数 | 期数 | Pearson IC | Rank IC | HAC年化ICIR | Rank IC NW t | IC胜率 | 多空分层差 | 分层差 NW t | 单调性 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in horizons.to_dict("records"):
        lines.append(
            f"| {int(row['horizon_sessions'])} | {int(row['newey_west_lags'])} | "
            f"{int(row['periods'])} | "
            f"{_number(row['mean_pearson_ic'])} | {_number(row['mean_rank_ic'])} | "
            f"{_number(row['annualized_rank_ic_ir'])} | "
            f"{_number(row['rank_ic_newey_west_t'])} | "
            f"{_pct(row['rank_ic_positive_rate'])} | "
            f"{_pct(row['mean_top_minus_bottom_return'])} | "
            f"{_number(row['spread_newey_west_t'])} | "
            f"{_number(row['mean_quantile_monotonicity'])} |"
        )
    lines.extend(
        [
            "",
            "## 年度稳定性",
            "",
            "| 年份 | 持有期 | 期数 | Rank IC | IC 胜率 | 多空分层差 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in annual.to_dict("records"):
        lines.append(
            f"| {int(row['year'])} | {int(row['horizon_sessions'])} | "
            f"{int(row['periods'])} | {_number(row['mean_rank_ic'])} | "
            f"{_pct(row['rank_ic_positive_rate'])} | "
            f"{_pct(row['mean_top_minus_bottom_return'])} |"
        )
    lines.extend(
        [
            "",
            "## 换手与硬门禁",
            "",
            f"- Top 组目标组合单边换手中位数：{_pct(turnover['one_way_turnover'].median())}",
            f"- Top 组目标组合单边换手 P90：{_pct(turnover['one_way_turnover'].quantile(0.90))}",
            "",
            "| 检查 | 失败数 |",
            "|---|---:|",
        ]
    )
    for name, value in quality["hard_failures"].items():
        lines.append(f"| `{name}` | {value} |")
    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "- 因子观测和未来收益标签物理分表；未来收益仅用于事后评测。",
            "- IC、分层差和 Newey-West t 值是研究诊断，不是可成交收益承诺。",
            f"- 主表使用 `{spec['return_basis']}` 收益口径；产物同时保存原始收益和行业/市值残差收益诊断，禁止跨口径直接比较。",
            "- 全截面回归一阶条件仅作为线性代数 sanity check；真正的风格门禁作用于 Top/Bottom 组合的行业主动权重和市值标准分偏离。",
            "- Top/Bottom 暴露阈值取固定下限与抽样标准误倍数的较大值，避免小股票池中的自然抽样波动被误判为风格下注。",
            "- 收益残差化失败时保留 raw 口径和失败诊断；若正式主口径配置为 residualized，则该期回退会阻止晋级。",
            "- 分层多空差仅用于辨别因子排序能力；A 股现金多头的可实现表现必须把 `target_weights.parquet` 送入 P0.6.3 执行回测。",
            "- P0.7 不以 IC 正负作为数据质量门禁，负面结果必须和正面结果一样保留。",
            "- 多因子合成、正交化顺序选择和组合优化不属于本产物。",
            "",
        ]
    )
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = "\n".join(lines)
    if destination.exists() and destination.read_text(encoding="utf-8") != encoded:
        raise FactorEvaluationError(f"immutable factor report conflict: {destination}")
    if not destination.exists():
        destination.write_text(encoded, encoding="utf-8")
    return destination


def _read_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FactorEvaluationError(f"input does not exist: {path}")
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise FactorEvaluationError(f"unsupported input format: {path}")


def _implementation_identity() -> Dict[str, Any]:
    here = Path(__file__)
    paths = [here, here.with_name("factor_evaluation.py"), here.with_name("timing.py")]
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
    normalized = frame.sort_values(sort_columns).reset_index(drop=True)
    return hashlib.sha256(
        pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    ).hexdigest()


def _write_immutable_parquet(
    frame: pd.DataFrame, path: Path, sort_columns: list[str], logical_sha: str
) -> None:
    if path.exists():
        if _frame_fingerprint(pd.read_parquet(path), sort_columns) != logical_sha:
            raise FactorEvaluationError(f"immutable factor output conflict: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, path)


def _write_immutable_json(payload: Any, path: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise FactorEvaluationError(f"immutable factor JSON conflict: {path}")
    if not path.exists():
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)


def _pct(value: float) -> str:
    return "—" if pd.isna(value) else f"{value:.2%}"


def _number(value: float) -> str:
    return "—" if pd.isna(value) else f"{value:.4f}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
