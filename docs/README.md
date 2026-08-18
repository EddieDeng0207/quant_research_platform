# 文档中心

本目录将工程标准、建设过程和研究结论分开管理。根目录 `README.md` 是项目入口；本页用于回答“下一步应该读哪份文档”和“哪份报告代表当前正式口径”。

## 第一次阅读

| 顺序 | 文档 | 目的 |
|---:|---|---|
| 1 | [项目演进与关键决策](project_journey.md) | 理解平台为什么按当前顺序建设 |
| 2 | [复现与运行指南](getting_started.md) | 安装、测试并理解正式运行边界 |
| 3 | [可复现研究与交付标准](reproducibility_standard.md) | 理解 artifact、哈希与晋级含义 |
| 4 | [因子研究工程标准](factor_research_standard.md) | 理解因子分类、PIT 输入与统一链路 |
| 5 | [研究报告索引](research/README.md) | 区分正式结论、诊断报告和历史版本 |

## 数据与时点标准

| 文档 | 解决的问题 |
|---|---|
| [数据源决策](data_source_decisions.md) | 主数据源、校验源和使用边界 |
| [数据契约](data_contract.md) | 字段、单位、证券代码和时间语义 |
| [数据隔离与复权](data_isolation_and_adjustment.md) | 因果收益、冻结复权与防前视隔离 |
| [P0.8 财务 PIT](p08_fundamental_pit_standard.md) | 公告、修订、可用时间和决策时点版本 |
| [P0.8 历史行业](p08_historical_industry_standard.md) | 历史分类版本、成员区间和体系切换 |

## 交易与组合标准

| 文档 | 解决的问题 |
|---|---|
| [P0.5 可交易性](p05_tradability_standard.md) | 停牌、涨跌停、ST、上市与退市状态 |
| [P0.6 执行](p06_execution_standard.md) | T+1、申报数量、成交、费用和冲击 |
| [P0.6.1 机构控制](p061_institutional_controls.md) | 因果容量、组合会计与真实成交校准 |
| [P0.6.2 组合回测](p062_portfolio_backtest_standard.md) | 事件时钟、公司行为、逐日账本和估值边界 |
| [P0.6.3 执行成本](p063_execution_cost_standard.md) | 波动率冲击、最低佣金和容量门禁 |

## 因子与研究治理

| 文档 | 解决的问题 |
|---|---|
| [P0.7 单因子评测](p07_single_factor_evaluation_standard.md) | 缩尾、中性化、IC、分层、HAC 与真实组合暴露 |
| [因子研究工程标准](factor_research_standard.md) | 原始信号、选股合成与组合交易分层 |
| [版本控制与发布](version_control_and_release_standard.md) | clean Git、环境锁、schema 和 release manifest |
| [可复现研究与交付](reproducibility_standard.md) | 交付等级和正式研究证据链 |

## 当前正式研究结论

1. [SP 2016–2026 单因子评测](research/sp_ttm_factor_evaluation_2016_2026.md)
2. [SP 2023 P0.6.3 成交回测](research/sp_ttm_p063_2023.md)
3. [SP 与 rev20_skip1 净经济性对照](research/sp_vs_rev20_net_economics_2023.md)
4. [rev20_skip1 长停牌估值有界回测](research/rev20_skip1_p063_2023_bounded_v3.md)

带 `_diagnostic`、`_v2` 或更早结论版本的文件为审计保留，不代表当前正式口径。详细状态见[研究报告索引](research/README.md)。

## 阅读规则

- 工程标准回答“如何计算以及什么情况下失败”；
- 正式报告回答“冻结输入下得到了什么结果”；
- release manifest 回答“该结论绑定了哪份代码、依赖和 artifact”；
- 诊断报告用于保留问题发现过程，不应覆盖后续正式结论；
- 工程门禁通过不等于具备投资价值或实盘可行性。
