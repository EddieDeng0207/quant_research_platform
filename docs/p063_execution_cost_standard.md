# P0.6.3 波动率冲击、费用诊断与小额调仓标准

## 1. 目标与版本边界

P0.6.3 在 P0.6.2 的时点严格组合账本上升级交易成本。P0.6.2
历史产物保持不可变；任何使用本标准的运行必须生成新的实现树哈希、产物 ID
和 `p063_portfolio_backtest_v1` manifest。

本阶段解决三个问题：固定冲击系数不能区分股票波动率、价格冲击封顶可能
掩盖不可执行订单、最低佣金对小额再平衡缺少独立诊断。

## 2. 因果波动率输入

执行日 `T` 的波动率只能使用截至 `T-1` 收盘的数据：

```text
neutral_close[t] = raw_close[t] * adj_factor[t]
neutral_return[t] = log(neutral_close[t] / neutral_close[t-1])
volatility20_daily_lag1[T]
    = std(neutral_return[T-20:T-1], ddof=1)
```

调整因子只用于消除现金分红、送转等机械除权缺口，不用于成交或估值。
容量构建要求日线、日度指标和调整因子一对一覆盖；缺失、非正数、重复键均
失败。波动率与流动性统计统一在执行日 09:20 标记可得，且必须早于执行事件。

## 3. 波动率感知平方根冲击

```text
amount_participation
    = filled_notional / lagged_effective_liquidity_amount

impact_bps
    = Y * volatility20_daily_lag1 * 10,000
        * amount_participation ^ 0.5

slippage_bps = base_slippage_bps + impact_bps
```

基础情景 `Y=0.50`，保守情景 `Y=1.00`。`Y` 是待真实券商成交回报
校准的无量纲参数。冲击使用成交额参与率而不是当前交易日成交量；有效流动性
取滞后 ADV20、ADV60 和20日成交额中位数的最小值，并应用场景流动性折扣。

## 4. 冲击容忍度决定成交量

`max_executable_impact_bps=100` 是订单可执行性约束，不是成本截断器：

```text
impact_participation_limit
    = (max_executable_impact_bps
       / (Y * volatility20_daily_lag1 * 10,000)) ^ 2

effective_participation_limit
    = min(policy_max_participation,
          impact_participation_limit)
```

成交量还要同时满足滞后股数 ADV、滞后成交额、自由流通市值、压力退出天数、
交易手数和现金约束。超过任一约束的部分不成交，记录为 partial，并由组合层
下一交易日根据原目标与真实持仓重建。系统不使用 `min(impact, cap)` 隐藏成本。

基础最大参与率仍为1%，保守情景为0.5%。日线模型无法识别开盘集合竞价的
真实可用量，因此在没有分钟或逐笔成交量曲线前，不把默认参与率提高到5%至10%。

## 5. 最低佣金与小额订单

最低佣金临界成交额按冻结费率计算：

```text
break_even_notional
    = minimum_commission_cny / (commission_bps / 10,000)
```

当前 `5元 / 3bp = 16,666.67元`。基础情景保留所有合法订单以暴露完整成本；
`commission_aware_open` 对低于16,666.67元的常规再平衡订单不下单，并写入
独立 `suppressed_orders` 账本。

以下完整退出不受阈值阻止：

- 目标权重归零且卖出全部持仓；
- 股票退出目标组合产生的全部清仓；
- 未来接入的退市、合规和风险强制处置。

抑制记录至少包含订单 ID、证券、日期、方向、估算金额、目标权重偏差、阈值、
抑制原因和决策时间。被抑制订单视为有意接受的目标偏离，不进入未成交重试队列。

## 6. 默认情景

| 情景 | 参与率 | 流动性折扣 | 基础滑点 | Y | 小额阈值 |
|---|---:|---:|---:|---:|---:|
| `base_open` | 1.0% | 100% | 5bp | 0.50 | 0元 |
| `conservative_open` | 0.5% | 50% | 10bp | 1.00 | 0元 |
| `commission_aware_open` | 1.0% | 100% | 5bp | 0.50 | 16,666.67元 |
| `delay_one_session` | 1.0% | 100% | 5bp | 0.50 | 0元 |

四个情景使用独立现金、持仓、订单、成交和公司行为账本。

## 7. 强制报告指标

每个情景必须报告：

- 成交订单数、部分成交数和拒绝数；
- 总成交额、佣金、印花税、过户费和滑点成本；
- 最低佣金命中数量与命中率；
- 实际综合佣金率和全费用率；
- 成交金额 P10 与中位数；
- 抑制订单数量与估算金额；
- 最大成交额参与率、最大冲击和冲击约束违规数；
- 容量最小值、P10、P25和中位数。

## 8. 硬晋级门禁

以下任一非零均不得晋级：

- 已成交订单缺失 `volatility20_daily_lag1`；
- `impact_bps` 超过场景冲击容忍度；
- 成交额参与率超过场景参与率；
- 被抑制订单属于完整退出；
- 现金、持仓或 NAV 为负；
- NAV 会计恒等式不平；
- 自由流通市值或压力退出天数超限。

## 9. 解读边界

平方根律是日频代理模型，不是开盘盘口回放。`Y`、基础滑点和冲击容忍度在
导入足量真实成交回报前均属于冻结研究假设。容量表示给定历史流动性、波动率、
目标组合和执行规则下的诊断值，不是未来可成交资金保证。
