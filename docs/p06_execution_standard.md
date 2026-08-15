# P0.6 A 股日频执行与交易成本标准 v2

> P0.6.3 已用波动率感知平方根冲击取代本文件中的固定冲击系数。最新标准见
> `docs/p063_execution_cost_standard.md`；本文件仅用于解释 P0.6/P0.6.1
> 历史产物，不得作为新回测的参数来源。

## 范围

P0.6 v1 在已晋级的 P0.5 可交易性矩阵上模拟现金 A 股开盘订单，覆盖订单手数、T+1、现金与持仓前端控制、涨跌停方向限制、滞后流动性容量、部分成交、滑点、佣金、证券交易印花税和过户费。

本层是可审计的日频执行近似，不是逐笔撮合引擎。日线无法观察盘口队列、撤单、集合竞价申报和逐笔冲击，因此 `filled` 表示在冻结假设下允许模拟成交，不代表实盘必然成交。

## 输入契约

订单文件必须是 CSV 或 Parquet，并包含：

| 字段 | 含义 |
|---|---|
| `order_id` | 全局唯一订单标识 |
| `trade_date` | 计划执行交易日 |
| `instrument_id` | 跨代码变更稳定证券身份 |
| `symbol` | 该交易日真实有效代码，必须与 P0.5 相同 |
| `side` | `buy` 或 `sell` |
| `quantity` | 正整数目标股数 |
| `adv20_shares_lag1` | 严格滞后一日的 20 日平均成交股数 |
| `adv20_amount_lag1` | 严格滞后一日的 20 日平均成交额 |
| `adv60_amount_lag1` | 严格滞后一日的 60 日平均成交额 |
| `median_amount20_lag1` | 严格滞后一日的 20 日成交额中位数 |
| `free_float_market_cap_lag1` | 严格滞后一日的自由流通市值代理 |
| `limit_price` | 可选限价；空值表示按模型化开盘执行 |

严禁用当日最终成交量、成交额或收盘后的市值决定当日开盘容量。任一机构容量字段缺失或非正值默认拒单。`adv_shares_lag1` 仅作为旧接口兼容字段，不足以通过 v2 默认门禁。

P0.5 产物必须 `promotion_passed=true`，且其 Parquet SHA-256 必须与 manifest 一致。订单的 `instrument_id/symbol/trade_date` 必须精确匹配 P0.5 行。

## 申报数量

- 上交所、深交所：买入按 100 股整数倍；目标数量向下取整，取整事实写入 `execution_notes`。
- 北交所：最低 100 股，超过最低数量后可以 1 股递增。
- 沪深持仓存在零股时，只有清仓路径允许一次性卖出非整百余额；普通部分卖出仍按整百处理。
- 卖出数量不得超过总持仓和 T+1 可卖持仓；不允许裸卖空。

## T+1 与账本

买入成交立即增加总持仓并扣减现金，但当日新增数量不增加 `sellable_quantity`。进入下一实际处理交易日时，已有总持仓转为可卖。卖出收入扣费后当日进入可用现金，可以继续买入；取现结算不属于策略回测现金账本范围。

每笔结果保存交易前后：

- `cash_before/cash_after`；
- `position_before/position_after`；
- `sellable_before/sellable_after`。

同日买入后卖出会以 `t_plus_one_no_sellable_shares` 拒绝，并完整保留在订单审计轨迹中。

## P0.5 约束继承

执行前依次检查：数据完整、存在行情、非停牌、标准研究股票池准入、对应方向 `can_buy_at_open/can_sell_at_open`。P0.5 的拒单原因原样进入 P0.6，例如开盘涨停买入拒绝或开盘跌停卖出拒绝。

默认要求 `standard_research_eligible=true`。研究 ST 等非标准股票时必须显式使用 `--allow-nonstandard-universe`；该开关只改变股票池政策，不会绕过停牌、涨跌停、数据质量和 T+1。

## 容量与部分成交

默认最大参与率为前一日可知流动性的 1%。可成交数量同时受股数、三种成交额口径、自由流通盘和压力退出天数限制：

```text
liquidity_amount = min(ADV20金额, ADV60金额, 20日成交额中位数)
capacity_shares = min(
    ADV20股数 × 1%,
    liquidity_amount × 1% / 参考价,
    自由流通市值 × 0.1% / 参考价 - 当前持仓,
    liquidity_amount × 2% × 3天 / 参考价 - 当前持仓
)
filled_quantity = min(submitted_quantity, capacity_shares, cash_capacity)
```

若容量小于订单且允许部分成交，状态为 `partial`；禁止部分成交时整笔拒绝。容量计算不使用当日 volume，也不把被拒订单虚构为零成本成交。

每笔订单保存金额参与率、预计自由流通盘占比、按 5% ADV 正常退出天数、按 2% ADV 压力退出天数和最终绑定约束。10% 不再是主回测默认值。

## 滑点与价格冲击

默认模型：

```text
participation = filled_quantity / adv_shares_lag1
slippage_bps = min(
    max_slippage_bps,
    base_slippage_bps
    + impact_coefficient_bps × participation ^ impact_exponent
)
```

基准参数为基础滑点 5 bps、冲击系数 50 bps、指数 0.5、最大滑点 200 bps。买入向上、卖出向下调整开盘价；买入价格向上取到 0.01 元报价单位，卖出向下取整。价格不得突破 P0.5 官方涨跌停边界。保守情景使用 0.5% 最大参与率、50% 流动性折扣、10 bps 基础滑点和 75 bps 冲击系数；另保存延迟一个交易日执行的结果。

限价订单若模型成交价不满足限价，以 `order_limit_not_marketable` 拒绝，同时保存参考价和候选执行价。

## 费用政策

默认 `a_share_cash_fee_v1_20230828`：

- 券商佣金：试运行假设 3 bps、每笔最低 5 元；这是可配置的客户合同参数，不是永久市场规则。
- 证券交易印花税：仅卖出收取；2023-08-28 前 10 bps，自该日起 5 bps。
- 股票交易过户费：双向收取；2022-04-29 起统一为 0.01‰，即 0.1 bps。更早日期按沪深 0.02‰、北京市场 0.025‰处理。
- `commission_bps` 视为不含印花税和过户费的全口径券商佣金，避免另行重复叠加经手费。

费用按实际成交金额计算，拒单不收费。买入现金容量同时考虑最低佣金和过户费。

## 不可变产物

`build-execution` 生成：

```text
data/curated/execution/artifact_id=<sha>/
├── executions.parquet
├── ending_positions.parquet
├── scenario_executions.parquet
├── scenario_summary.parquet
└── manifest.json
```

artifact ID 绑定：P0.5 artifact 及哈希、订单哈希、初始现金、执行规范、费用政策和实现代码树。结果记录每笔成交/部分成交/拒绝、原因、价格、数量、参与率、滑点、费用以及账本前后状态。

结构晋级门禁包括：现金不得为负、持仓不得为负、费用不得为负、成交量不得超过提交量、期末持仓不得为负。正常的涨跌停、T+1 或限价拒单是策略执行结果，不是数据质量失败。

## 使用

```bash
.venv/bin/qrp-data build-execution \
  --tradability-artifact data/curated/tradability/artifact_id=<P0.5_ID> \
  --orders artifacts/pilots/orders.csv \
  --initial-cash 1000000
```

佣金、参与率和冲击参数必须在正式研究配置中显式冻结，并做敏感性测试。

## 官方依据

- 财政部、税务总局：2023-08-28 起证券交易印花税减半征收。
- 中国结算公开通知：2022-04-29 起股票交易过户费统一调整为成交金额 0.01‰、双向收取。
- 上交所、深交所交易规则：买入股票按 100 股或整数倍，零股卖出受前端控制。
- 北交所规则：最低申报 100 股、之后可按 1 股递增；买入证券当日不得卖出。

具体链接保存在 P0.6 试运行报告中。
