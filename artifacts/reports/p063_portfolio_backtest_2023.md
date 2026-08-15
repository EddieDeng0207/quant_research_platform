# P0.6.3 组合回测报告

## 结论

产物 `f7b1779cf5e1c2a189fb` 通过 P0.6.3 硬晋级门禁。

## 冻结身份

- P0.5 artifact：`e37326cee45d351375da`
- P0.5 Parquet SHA-256：`200ea0eeca1e8402428d229a37f772aabe836a2b8f632afd2e61d502d46171f6`
- 目标权重 SHA-256：`edc75eca0ce787234cd717f91aaf05458170f477d71bae6e8aad01472e7d3112`
- 容量面板 SHA-256：`21b79eb649bccbc2b75c796c98d9c37e862ea67915c40a8690f8eaf76d408240`
- 公司行为 SHA-256：`0d816be1876f8851d6282672f9de23a443b554891abeaed06ffafdd4648206c6`
- 实现树 SHA-256：`8ee3f30d24deb30959535438f8ee5d43d2194515a14d755e8feafa946be2fd94`

## 覆盖与质量

- 区间：2023-01-03 至 2023-12-29
- 交易日：242
- 情景：4
- 订单/执行记录：229/229
- 公司行为事件：168

## 情景结果

| 情景 | 总收益 | 年化收益 | 年化波动 | 最大回撤 | 费用 | 滑点成本 | 现金分红 | 容量 P10 | 容量中位数 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base_open | 2.86% | 2.98% | 13.50% | -14.07% | 717.84 | 1,325.00 | 41,941.30 | 74,047,219 | 96,566,077 |
| commission_aware_open | 3.34% | 3.48% | 13.15% | -13.46% | 343.93 | 1,074.00 | 40,994.85 | 74,047,219 | 96,566,077 |
| conservative_open | 2.80% | 2.92% | 13.51% | -14.08% | 717.95 | 1,928.00 | 41,941.30 | 37,023,610 | 48,283,038 |
| delay_one_session | 2.26% | 2.36% | 13.40% | -14.03% | 697.81 | 1,289.00 | 41,625.30 | 74,047,219 | 96,088,931 |

## 最低佣金与小额订单

| 情景 | 最低佣金订单 | 命中率 | 实际佣金率 | 全费用率 | 成交额中位数 | 成交额 P10 | 抑制订单 | 抑制金额 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| base_open | 63 | 86.30% | 4.88bp | 6.00bp | 4,558 | 1,622 | 0 | 0 |
| commission_aware_open | 0 | 0.00% | 3.00bp | 3.55bp | 96,486 | 20,057 | 76 | 505,005 |
| conservative_open | 63 | 86.30% | 4.88bp | 6.00bp | 4,560 | 1,622 | 0 | 0 |
| delay_one_session | 60 | 85.71% | 4.83bp | 5.93bp | 4,482 | 1,369 | 0 | 0 |

## 硬门禁

| 检查 | 失败行数 |
|---|---:|
| `duplicate_scenario_date_nav_rows` | 0 |
| `execution_amount_participation_breach_rows` | 0 |
| `execution_free_float_breach_rows` | 0 |
| `execution_impact_tolerance_breach_rows` | 0 |
| `execution_missing_volatility_rows` | 0 |
| `execution_stress_exit_breach_rows` | 0 |
| `nav_accounting_tie_failure_rows` | 0 |
| `negative_cash_rows` | 0 |
| `negative_nav_rows` | 0 |
| `negative_position_rows` | 0 |
| `suppressed_full_exit_rows` | 0 |
| `target_cash_buffer_breach_rows` | 0 |
| `unknown_execution_scenario_rows` | 0 |

## 解读边界

- 本报告由冻结产物自动生成，没有手工输入绩效数字。
- 执行使用日线可证明的保守约束，不代表逐笔排队还原。
- 在导入真实券商回报完成校准前，滑点和冲击仍是冻结研究假设。
- 冲击按滞后20日个股波动率与成交额参与率的平方根计算；超过冲击容忍度时缩减成交量，不截断成本。
- 小额订单抑制只作用于常规再平衡，完整退出仍强制保留。
- 策略表现与容量应一起阅读；任何单一最优情景不得被单独选择展示。
