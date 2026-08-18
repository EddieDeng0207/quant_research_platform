"""Human-readable reports derived only from frozen P0.6.3 artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from qrp.execution.daily import ExecutionError


def generate_backtest_report(artifact: Path, output: Path) -> Path:
    root = Path(artifact)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise ExecutionError(f"backtest manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in {
        "p063_portfolio_backtest_v1",
        "p063_portfolio_backtest_v2",
        "p063_portfolio_backtest_v3_capacity_diagnostics",
    }:
        raise ExecutionError("report input is not a P0.6.3 artifact")
    for name, metadata in manifest["outputs"].items():
        path = Path(metadata["path"])
        if not path.is_absolute():
            path = root / path.name
        if not path.exists() or _sha256(path) != metadata["sha256"]:
            raise ExecutionError(f"backtest output hash mismatch: {name}")
    summary = pd.read_parquet(root / "scenario_summary.parquet").sort_values("scenario")
    quality = manifest["quality"]
    identity = manifest["identity"]
    lines = [
        "# P0.6.3 组合回测报告",
        "",
        "## 结论",
        "",
        (
            f"产物 `{manifest['artifact_id']}` "
            + ("通过" if quality["promotion_passed"] else "未通过")
            + " P0.6.3 硬晋级门禁。"
        ),
        "",
        "## 冻结身份",
        "",
        f"- P0.5 artifact：`{identity['p05_artifact_id']}`",
        f"- P0.5 Parquet SHA-256：`{identity['p05_parquet_sha256']}`",
        f"- 目标权重 SHA-256：`{identity['targets_sha256']}`",
        f"- 容量面板 SHA-256：`{identity['capacity_sha256']}`",
        f"- 公司行为 SHA-256：`{identity['corporate_actions_sha256']}`",
        f"- 实现树 SHA-256：`{identity['implementation_sha256']}`",
        "",
        "## 覆盖与质量",
        "",
        f"- 区间：{quality['start_date']} 至 {quality['end_date']}",
        f"- 交易日：{quality['trading_sessions']}",
        f"- 情景：{quality['scenarios']}",
        f"- 订单/执行记录：{quality['orders']}/{quality['executions']}",
        f"- 公司行为事件：{quality['corporate_action_events']}",
        (
            "- 退市终止事件/实际有持仓写销："
            f"{quality.get('terminal_zero_recovery_writeoff_events', 0)}/"
            f"{quality.get('terminal_zero_recovery_position_writeoffs', 0)}"
        ),
        (
            "- 零碎股保守舍弃事件/股数："
            f"{quality.get('fractional_share_events', 0)}/"
            f"{quality.get('fractional_shares_discarded', 0):.4f}"
        ),
        (
            "- 最大估值陈旧交易日/超限证券数："
            f"{quality.get('maximum_observed_stale_valuation_sessions', 0)}/"
            f"{quality.get('stale_valuation_breach_instruments', 0)}"
        ),
        (
            "- 长停牌估值界限（base/全情景最坏/冻结容忍度）："
            f"{quality.get('stale_valuation_base_open_nav_bound_pp', 0):.4f}pp/"
            f"{quality.get('stale_valuation_nav_bound_pp', 0):.4f}pp/"
            f"{quality.get('max_stale_valuation_nav_bound_pp', 0):.4f}pp"
        ),
        "",
        "## 情景结果",
        "",
        "| 情景 | 总收益 | 年化收益 | 年化波动 | 最大回撤 | 费用 | 滑点成本 | 现金分红 | 容量 P10 | 容量中位数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.to_dict("records"):
        lines.append(
            f"| {row['scenario']} | {_pct(row['total_return'])} | "
            f"{_pct(row['annualized_return'])} | "
            f"{_pct(row['annualized_volatility'])} | {_pct(row['max_drawdown'])} | "
            f"{row['total_fees']:,.2f} | {row['total_slippage_cost']:,.2f} | "
            f"{row['total_cash_dividends']:,.2f} | "
            f"{_money(row['capacity_p10'])} | {_money(row['capacity_median'])} |"
        )
    if "conditional_positive_capacity_p10" in summary.columns:
        lines.extend(
            [
                "",
                "## 容量完整性与条件诊断",
                "",
                "| 情景 | 观测日 | 严格零容量日 | 正容量日占比 | 条件 P10 | 条件中位数 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in summary.to_dict("records"):
            lines.append(
                f"| {row['scenario']} | {int(row['capacity_observation_dates'])} | "
                f"{int(row['zero_capacity_dates'])} | "
                f"{_pct(row['positive_capacity_date_rate'])} | "
                f"{_money(row['conditional_positive_capacity_p10'])} | "
                f"{_money(row['conditional_positive_capacity_median'])} |"
            )
        lines.extend(
            [
                "",
                "严格容量将任一目标证券的必要容量输入缺失记为零，因此它是 fail-closed 下界。条件 P10/中位数仅描述正容量日，不得替代严格值或用于忽略停牌/缺失容量风险。",
            ]
        )
    if "stale_valuation_bounds" in manifest["outputs"]:
        lines.extend(
            [
                "",
                "## 长停牌估值上下界",
                "",
                "| 执行情景 | 最大界限宽度 | 容忍度 | 结果 |",
                "|---|---:|---:|---|",
            ]
        )
        tolerance = quality.get("max_stale_valuation_nav_bound_pp", 0.0)
        for scenario, width in quality.get(
            "stale_valuation_nav_bound_pp_by_scenario", {}
        ).items():
            result = "通过" if width <= tolerance + 1e-12 else "失败"
            lines.append(
                f"| {scenario} | {width:.4f}pp | {tolerance:.4f}pp | {result} |"
            )
    lines.extend(
        [
            "",
            "## 最低佣金与小额订单",
            "",
            "| 情景 | 最低佣金订单 | 命中率 | 实际佣金率 | 全费用率 | 成交额中位数 | 成交额 P10 | 抑制订单 | 抑制金额 | 最大权重偏离 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summary.to_dict("records"):
        lines.append(
            f"| {row['scenario']} | {int(row['minimum_commission_orders'])} | "
            f"{_pct(row['minimum_commission_hit_rate'])} | "
            f"{_bps(row['effective_commission_bps'])} | "
            f"{_bps(row['all_in_fee_bps'])} | "
            f"{_money(row['median_filled_order_notional'])} | "
            f"{_money(row['p10_filled_order_notional'])} | "
            f"{int(row['suppressed_orders'])} | "
            f"{_money(row['suppressed_notional'])} | "
            f"{_pct(row['suppressed_abs_delta_weight_max'])} |"
        )
    lines.extend(
        [
            "",
            "## 硬门禁",
            "",
            "| 检查 | 失败行数 |",
            "|---|---:|",
        ]
    )
    for name, value in quality["hard_failures"].items():
        lines.append(f"| `{name}` | {value} |")
    lines.extend(
        [
            "",
            "## 解读边界",
            "",
            "- 本报告由冻结产物自动生成，没有手工输入绩效数字。",
            "- 执行使用日线可证明的保守约束，不代表逐笔排队还原。",
            "- 在导入真实券商回报完成校准前，滑点和冲击仍是冻结研究假设。",
            "- 冲击按滞后20日个股波动率与成交额参与率的平方根计算；超过冲击容忍度时缩减成交量，不截断成本。",
            "- 小额订单抑制只作用于常规再平衡，完整退出仍强制保留。",
            "- 超过冻结上限的正持仓同时报告末价上界与零价下界；两者只是同一交易账本的估值覆盖层，不会改变订单、成交或现金。",
            "- 上下界缺一或全情景最坏宽度超过冻结容忍度仍会阻止晋级；通过只证明估值不确定性有界，不代表末价或零价是公允价值。",
            "- 持仓跨越 P0.5 退市日时按零回收保守写销；未来接入实际三板回收数据必须生成新版本。",
            "- 中国结算的零碎股按全市场账户排序分配，单账户回测无法还原随机次序；本版本向下取整且不给现金替代，属于保守下界。",
            "- 策略表现与容量应一起阅读；任何单一最优情景不得被单独选择展示。",
            "",
        ]
    )
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    if destination.exists() and destination.read_text(encoding="utf-8") != text:
        raise ExecutionError(f"immutable report conflict: {destination}")
    if not destination.exists():
        destination.write_text(text, encoding="utf-8")
    return destination


def _pct(value: float) -> str:
    return "—" if pd.isna(value) else f"{value:.2%}"


def _money(value: float) -> str:
    return "—" if pd.isna(value) else f"{value:,.0f}"


def _bps(value: float) -> str:
    return "—" if pd.isna(value) else f"{value:.2f}bp"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
