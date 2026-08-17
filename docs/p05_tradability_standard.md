# P0.5 A 股日频可交易性标准 v1

## 目的与边界

P0.5 把“存在一条日线”与“策略能够成交”严格分开。产物用于回测执行约束、持仓估值和数据质量审计，不是因子；所有行固定 `execution_only=true`、`research_feature_allowed=false`。

日线数据无法观测委托队列、盘中逐笔路径和真实冲击成本。因此本层只回答保守的方向性资格，不能把 `can_buy_*` 或 `can_sell_*` 解释为一定成交。成交价格、延迟、费用、滑点与容量属于后续执行模型。

## 冻结输入

| 数据集 | 语义 | 缺失处理 |
|---|---|---|
| `historical_instruments` | 当日历史股票池及上市日；`bak_basic` 空快照时允许全状态主表有效区间重建 | 缺任一交易日则拒绝构建 |
| `daily_bars` | 未复权 OHLCV/金额 | 仅有显式停牌事件时允许缺失 |
| `daily_limits` | 当日官方涨跌停边界 | 有行情却缺记录则晋级失败 |
| `daily_suspensions` | 当日停牌/复牌事件集 | 规范零行快照表示“当日无事件” |
| `stock_status` | 当日 ST/风险警示集合 | 2016 年以前无权威覆盖则拒绝构建 |
| `instruments` | 当前证券主数据 | 用于身份和元数据核验，不定义历史股票池 |
| `security_code_mappings` | 北交所新旧代码对照 | 缺失则不能生成正式 P0.5 产物 |

每个输入引用精确 raw Parquet、查询、摄取时间、行数和 SHA-256。构建允许指定 `as_of_ingested_at`，保证只能看到截止当时已经进入数据湖的快照。

## 状态机与默认拒绝

1. 历史股票池有证券、日线缺失、且存在 `S` 停牌事件：认定停牌；禁止买卖，估值方法为前收盘价延续。
2. 历史股票池有证券、日线缺失、但无 `S` 事件：认定无法解释的数据缺口；禁止买卖并使产物晋级失败，绝不自动猜成停牌。
3. 有日线但无涨跌停记录：禁止买卖并使产物晋级失败。
4. 日内最低价低于官方跌停价或最高价高于官方涨停价（容差 0.0051 元）：使产物晋级失败。
5. `S` 与日线同时存在：标记部分/盘中停牌，按保守策略禁止当日交易；行情仍可用于收盘估值。
6. `R` 仅保留复牌事件事实；是否可交易仍由行情、涨跌停和质量门禁共同决定。

## 涨跌停与方向约束

- 开盘价触及涨停：`can_buy_at_open=false`，卖出方向不因此禁止。
- 开盘价触及跌停：`can_sell_at_open=false`，买入方向不因此禁止。
- 最低价仍在涨停价：一字涨停，`can_buy_during_day=false`。
- 最高价仍在跌停价：一字跌停，`can_sell_during_day=false`。
- 供应商明确给出 `up_limit>=99999` 且 `down_limit=0`：标记无界制度，不拿哨兵值做价格比较；其他质量条件仍须满足。
- 供应商历史响应中的 `pre_close=0` 规范化为缺失值并记录行数；因该字段仅用于交叉核验，不据此重算上下限。
- 日线 `pre_close` 与涨跌停表 `pre_close` 不一致只作为潜在除权除息/口径警告。实际价格没有越界时不单独阻止晋级。

ST 是研究股票池政策，不等价于法律上的绝对不可交易：矩阵保留买卖资格判断，但默认 `standard_research_eligible=false`。策略若研究 ST，必须显式建立独立股票池规范。

## 证券身份与代码变更

每行同时保存：

- `instrument_id`：跨代码变更稳定身份；
- `symbol`：该交易日真实有效代码；
- `source_universe_symbol/source_bar_symbol/source_limit_symbol` 等：供应商原始代码。

北交所 920 代码迁移政策版本为 `bse_920_transition_v1`：2024-04-22 后新上市证券从上市日使用 920 代码；6 只存量试点证券自 2025-05-06 切换；其余存量证券自 2025-10-09 切换。此前日期将供应商回溯的 `920xxx.BJ` 恢复为映射表中的旧代码。其他公司代码变更必须进入人工审阅的 `configs/instrument_aliases.json`。别名合并时，若新旧代码同日经济字段不完全一致，立即失败，禁止静默择一。

## 晋级硬门禁

以下任一计数非零，正式构建命令失败但保留诊断产物：

- `unexplained_missing_bar_rows`；
- `bar_without_limit_rows`；
- `price_below_down_limit_rows`；
- `price_above_up_limit_rows`。

正式产物清单还记录规范 SHA-256、实现代码树 SHA-256、别名配置 SHA-256、输入指纹、逻辑数据哈希和物理 Parquet 哈希。同一冻结输入与实现复跑必须得到相同 artifact ID。

## 使用

```bash
.venv/bin/qrp-data backfill-p05 --start 2024-01-02 --end 2024-01-05
.venv/bin/qrp-data build-tradability --start 2024-01-02 --end 2024-01-05
```

只有定位数据问题时才可使用 `--allow-failed-promotion`；该选项生成的产物不得进入正式回测。

## 官方语义依据

- Tushare `daily`：停牌期间不提供日线，约 15:00–16:00 更新。
- Tushare `stk_limit`：盘前约 08:40 更新，单次上限 5,800，需要分页覆盖全市场。
- Tushare `suspend_d`：区分 `S/R` 与停牌时段，更新时间不定。
- Tushare `stock_st`：约 09:20 更新，权威历史自 2016 年开始。
- Tushare `bak_basic`：按交易日提供历史股票列表与上市日。
- Tushare `bse_mapping` 与北交所公告：定义代码对照及分阶段切换政策。

上述页面链接和最终实证结果记录在 `artifacts/reports/p05_tradability_20240102_20240105.md`。
