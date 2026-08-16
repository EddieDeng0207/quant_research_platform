# P0.8 财务数据 Point-in-Time 生产标准

## 目标与边界

P0.8 将 Tushare 三张财务报表和财务指标从单标的试用接口升级为可恢复的全市场
历史任务，并生成保留全部原始修订、公告知识时间和平台数据版本的不可变
curated artifact。本阶段不定义 BP、EP、ROE 等因子公式，也不使用财务结果产生
投资结论。

## 双时间模型

财务数据同时保留两条独立时间轴：

```text
历史信息时间：report_period -> announcement_at -> available_at -> decision_at
平台数据版本：source_ingested_at -> research_as_of_at
```

正式历史回放必须同时满足：

```text
report_period <= announcement_at <= available_at <= decision_at
source_ingested_at <= research_as_of_at
decision_at <= research_as_of_at
```

`report_period` 只表示会计期间，不能作为知识时间。`source_ingested_at` 是本平台实际
取得供应商快照的时间，也不能伪造成历史公告时间。历史数据在 2026 年回补后可以研究
2018 年决策，依据是供应商保留的公告/修订时间，而不是虚构
`source_ingested_at <= decision_at`。

供应商只提供公告日期而没有可靠的日内时刻时，平台把 `announcement_at` 保守设置为
公告日 23:59:59（Asia/Shanghai），并从下一个交易日 09:30 起设置
`available_at`。公告当日不允许进入信号。

## Raw 接入

`backfill-fundamentals` 冻结以下任务网格：

```text
(statement_type, symbol, requested_date_window)
```

默认报表族：

- `income`；
- `balance_sheet`；
- `cashflow`；
- `financial_indicators`。

未显式提供股票列表时，任务先冻结包含上市、退市、暂停上市等状态的证券主数据，再排除
无法由六位交易代码安全调用的 legacy instrument，并把排除数量写入运行报告。不能使用
今天的仅上市股票列表代替历史证券全集。

每个成功请求立即写入 Parquet 和 manifest SHA-256，然后原子更新 checkpoint。相同数据
身份续跑时跳过已完成任务。零行是合法且必须落盘的快照，用于区分“该证券没有报表”和
“请求未完成”。供应商异常、空对象、契约错误和达到已知返回上限使用不同状态。

可用 `--workers` 并发隐藏网络响应延迟；所有线程共享同一个线程安全限流器，因此并发数
不会放大每分钟调用额度。

Tushare 当前官方说明中，`income`、`balancesheet`、`cashflow` 的
`start_date/end_date` 是公告日期范围；`fina_indicator` 的相同参数是报告期范围。
后者单次最多返回 100 行。平台分别记录两种日期语义；财务指标返回行数达到 100 时
fail closed，要求缩短区间后重跑，不能把可能截断的数据标为完整。

正式速率默认 250 次/分钟。供应商适配器拥有唯一请求限流器，因此重试和未来分页调用
都计入同一容量，而不是只对外层任务计数。

## Raw 行身份与修订

raw 行增加：

- `statement_type`；
- `source_row_sha256`：对供应商规范化行内容计算；
- `source_row_occurrence`：区分供应商返回的完全重复行；
- `announcement_date`、`actual_announcement_date`；
- `available_date`：实际公告日优先，否则公告日；
- `source`、`ingested_at`。

raw 层不按 `update_flag` 删除记录，不覆盖原始版，不把今天的最终版本回填到历史。

## Curated artifact

`build-fundamentals-pit` 只接受状态为 `completed` 的 backfill run。它验证：

1. checkpoint 包含完整的报表 × 股票任务笛卡尔积；
2. 每个输入路径存在于 raw manifest；
3. 文件 SHA-256、行数、dataset 与任务身份一致；
4. 输入写入时间不晚于冻结的 `research_as_of_at`；
5. 交易日历覆盖所有公告日之后的下一交易日；
6. 每个 canonical `version_id` 唯一；
7. 公告时间不晚于系统可用时间。

产物按报表族分别保存宽表，同时生成统一的 `version_index.parquet`。不同公司类型的字段
不会被强行套用普通工商企业口径。银行、保险、证券和一般工商企业的会计检查与因子公式
必须在后续因子定义层分别实现。

证券默认身份为 `CN_EQ:{symbol}`。经过审阅的代码变更配置和北交所历史/当前代码映射会
把两个代码映射到同一个稳定 `instrument_id`；映射文件 SHA-256 进入产物身份。

## 决策时点选择

`select_fundamentals_as_of` 对给定 `decision_at` 和 `research_as_of_at` 执行：

1. 只保留 `available_at <= decision_at`；
2. 只保留 `source_ingested_at <= research_as_of_at`；
3. 默认只使用合并报表 `report_type=1`（数据集不存在该字段时不虚构过滤）；
4. 同一证券、报告期选择当时最新 `available_at`；
5. 同时刻存在 `update_flag=1` 时优先该版本；
6. 完全相同的供应商重复行只保留一个语义版本；
7. 同时刻仍有不同内容的多版本则报错，禁止任意挑选；
8. 默认再选择该证券当时最新报告期并记录 `report_age_days`。

正式 CLI 默认要求 clean Git，并把 commit、tree、dirty fingerprint（仅探索运行）、
`requirements.lock` SHA-256 和实现文件树 SHA-256 写入 artifact 身份。
`--allow-dirty-code` 只能生成探索产物。

## 研究就绪门禁

`audit-research-readiness` 同时检查：

- 目标区间每个开市日的 P0/P0.5 raw partition；
- 周频期数不少于 104；
- 财务 PIT artifact 通过且所有输出哈希有效；
- 财务和行业输入默认分别至少覆盖 200 个证券，试验性小股票池不能冒充正式就绪；
- 历史行业数据具有 `decision_at` 快照或 membership 生效区间；
- 历史行业覆盖目标研究区间。

行业数据未提供时，报告必须失败。当前行业成员反向填充历史不能通过时间契约。

## 当前尚未完成

- 尚未执行 2018 年以来的正式全市场下载；
- 尚未选定并接入历史申万行业成员数据；
- 尚未定义和生产首批 BP/EP/CFP/质量因子；
- 尚未把 P0.8 因子观测送入 P0.7/P0.6.3 正式运行。

## 供应商规则来源

- Tushare 利润表：<https://tushare.pro/document/2?doc_id=33>
- Tushare 资产负债表：<https://tushare.pro/document/2?doc_id=36>
- Tushare 现金流量表：<https://tushare.pro/document/2?doc_id=44>
- Tushare 财务指标：<https://tushare.pro/document/2?doc_id=79>
