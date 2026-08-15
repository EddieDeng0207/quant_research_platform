# P0.6.2 组合回测报告

## 结论

产物 `cf5d9716b4015b480c71` 通过 P0.6.2 硬晋级门禁。

## 冻结身份

- P0.5 artifact：`e37326cee45d351375da`
- P0.5 Parquet SHA-256：`200ea0eeca1e8402428d229a37f772aabe836a2b8f632afd2e61d502d46171f6`
- 目标权重 SHA-256：`edc75eca0ce787234cd717f91aaf05458170f477d71bae6e8aad01472e7d3112`
- 容量面板 SHA-256：`9a79ed6b7ffa2d3fbf421299b02b1716c00eedd271987e65d4298946fd66149d`
- 公司行为 SHA-256：`0d816be1876f8851d6282672f9de23a443b554891abeaed06ffafdd4648206c6`
- 实现树 SHA-256：`950079c01164f329adbc9e1ac9a9fc79f7b6b74b5b9902f0f957410780b7b580`

## 覆盖与质量

- 区间：2023-01-03 至 2023-12-29
- 交易日：242
- 情景：3
- 订单/执行记录：216/216
- 公司行为事件：126

## 情景结果

| 情景 | 总收益 | 年化收益 | 年化波动 | 最大回撤 | 费用 | 滑点成本 | 现金分红 | 容量 P10 | 容量中位数 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base_open | 2.86% | 2.98% | 13.50% | -14.07% | 717.82 | 1,281.00 | 41,941.30 | 74,047,219 | 96,566,077 |
| conservative_open | 2.82% | 2.94% | 13.51% | -14.07% | 717.90 | 1,724.00 | 41,941.30 | 37,023,610 | 48,283,038 |
| delay_one_session | 2.26% | 2.36% | 13.40% | -14.03% | 697.80 | 1,271.00 | 41,625.30 | 74,047,219 | 96,088,931 |

## 硬门禁

| 检查 | 失败行数 |
|---|---:|
| `duplicate_scenario_date_nav_rows` | 0 |
| `execution_amount_participation_breach_rows` | 0 |
| `execution_free_float_breach_rows` | 0 |
| `execution_stress_exit_breach_rows` | 0 |
| `nav_accounting_tie_failure_rows` | 0 |
| `negative_cash_rows` | 0 |
| `negative_nav_rows` | 0 |
| `negative_position_rows` | 0 |
| `target_cash_buffer_breach_rows` | 0 |
| `unknown_execution_scenario_rows` | 0 |

## 解读边界

- 本报告由冻结产物自动生成，没有手工输入绩效数字。
- 执行使用日线可证明的保守约束，不代表逐笔排队还原。
- 在导入真实券商回报完成校准前，滑点和冲击仍是冻结研究假设。
- 策略表现与容量应一起阅读；任何单一最优情景不得被单独选择展示。
