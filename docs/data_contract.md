# 数据契约 v0.1

## 时间字段

- `trade_date`：行情或横截面指标所属交易日。
- `report_period`：财务数据对应的报告期，不能作为信号可用时间。
- `announcement_date`：供应商提供的公告日期。
- `actual_announcement_date`：供应商提供的实际公告日期。
- `available_date`：研究系统允许使用该记录的最早日期；当前规则为实际公告日优先，否则使用公告日。
- `available_at`：把日期级公告按冻结交易日历映射后的最早系统可用时刻；财报默认下一交易日 09:30。
- `ingested_at`：平台获取数据的 UTC 时间，不等于历史可用时间。
- `research_as_of_at`：一次研究冻结的数据快照截止时间，要求 `ingested_at <= research_as_of_at`。
- `realtime_start/realtime_end`：FRED/ALFRED 中一个观测版本成立的实时区间。

## 行情单位

- 价格：CNY/股。
- `volume`：股。AKShare 与 Tushare 的“手”统一乘以 100。
- `amount`：CNY。Tushare 的千元统一乘以 1000；AKShare/东财按元保留。
- `adjustment`：`raw`、`qfq` 或 `hfq`。核心研究默认保存 `raw`。

## 证券代码

内部统一为 `000001.SZ`、`600000.SH`、`830799.BJ`。供应商原代码在供应商支持时保留为 `source_symbol`。

证券主数据允许保留 Tushare 的 legacy identifier，例如 `T600018.SH`。该前缀用于区分后来复用同一六位代码的证券身份，不得移除；行情和交易层仍只接受标准六位可交易代码。主数据通过 `instrument_kind=legacy_stock` 标记此类记录。

代码变更采用三字段身份模型：`instrument_id` 是跨代码变更稳定的证券身份，`symbol` 是交易日当时有效的历史交易代码，`source_*_symbol` 保留供应商返回的原代码。北交所 `920xxx` 回溯代码必须通过冻结的 `security_code_mappings` 和版本化生效政策还原，不能直接覆盖 raw 数据。

## P0.5 可交易性输入

- `historical_instruments`：交易日历史股票池，必须含 `list_date`。优先使用 `bak_basic`；供应商返回空快照时，允许用同次冻结、同时包含 L/D/P 状态的证券主表按 `[list_date, delist_date]` 有效区间重建。重建只决定当日是否已上市，退市边界不得作为研究特征暴露。
- `daily_limits`：盘前涨停价、跌停价及可选 `pre_close`；显式的 `99999.99/0` 无涨跌停哨兵值原样保留。
- 历史 `stk_limit` 的 `pre_close=0` 是缺失哨兵，规范化为 `NA` 并记录计数；上下限价格不得由此推导或覆盖。
- `daily_suspensions`：`S` 停牌、`R` 复牌事件；零行是合法且有覆盖的事件快照，必须保存规范空表，不能与“请求缺失”混淆。
- `stock_status`：风险警示状态；供应商权威历史起点为 2016-01-01，早于此日期禁止无替代源构建。
- `security_code_mappings`：证券旧代码、新代码、名称与上市日；属于不可变全量快照。

可交易性矩阵是执行观察量，固定 `execution_only=true`、`research_feature_allowed=false`。当日收盘后才可用于成交审计和下一期状态，不得作为当日开盘前的研究特征。

## P0.6 订单与成交

订单必须提供唯一 `order_id`、`trade_date`、稳定 `instrument_id`、历史 `symbol`、方向、目标股数和 `adv_shares_lag1`。容量字段必须是交易前已知的滞后值，禁止用当日最终成交量决定当日订单。

成交结果同时保留请求数量、规范申报数量、实际成交数量、未成交数量、参考价、执行价、参与率、滑点、各项费用、拒单原因以及现金/总持仓/可卖持仓的前后状态。拒单属于正式研究记录，不得从回测样本中删除。

## 不可变与可复现

raw 文件只追加、不覆盖。每个文件记录来源、查询参数、转换、写入时间和 SHA-256。清洗、复权、周频聚合及 Point-in-Time 去重属于后续 curated 层。
