# 数据隔离与复权规范 v1

## 适用范围

本规范是研究和回测的硬门槛，适用于日频数据、周频因子以及后续基本面、分析师预期和另类数据。任何模型不得直接读取供应商返回表，也不得在 raw 层改写价格。

## 三个时间与失败关闭

每条记录必须区分：

- `event_time`：经济事件或市场观测实际发生的时间，例如交易日、报告期；
- `available_at`：当时的研究者最早可以知道该值的保守时间；
- `ingested_at`：本平台实际获取这一数据版本的 UTC 时间。

因果查询必须同时满足 `available_at <= decision_time`。回放历史数据版本时还必须满足 `ingested_at <= as_of_ingested_at`。没有可用时间、时间无法解析、复权因子缺失或输入哈希不一致时一律报错，不自动猜测或填充。

当前保守规则：A 股日线在交易日北京时间 16:00 后可用；复权因子按供应商更新时间记为 09:20；日指标按 17:00；财报及其他可能盘中/盘后发布的数据，无可靠时分秒时统一延迟到公告日后的下一交易日 09:30。用收盘数据形成的信号最早只能在下一交易日开盘执行。

## 原始层、派生层与研究层

```text
raw immutable snapshots
  ├─ 未复权 OHLC / 原始成交量 / 原始成交额
  └─ 独立复权因子及抓取版本
          │ input file + SHA-256 + policy fingerprint
          ▼
curated immutable artifacts
  ├─ causal_returns（建模和回测默认）
  ├─ qfq_asof（指定知识截止日的临时研究视图）
  ├─ hfq（描述性历史视图）
  └─ total_return_index（固定基日指数）
          │ purge + embargo + physical sealing
          ▼
research dataset
  ├─ split=train/{features,labels}
  ├─ split=validation/{features,labels}
  └─ split=holdout/{features,labels}  ← 开发模式禁止读取
```

curated 产物身份由 raw 文件哈希、时间范围、抓取版本截止时间和处理策略共同计算。同一身份重复生成时会校验逻辑数据哈希；已有内容不同则报错，不能覆盖。

## A 股价格复权

记未复权价格为 `P_t`，复权因子为 `F_t`。

- 指定截止日 `T` 的前复权：`P_qfq(t; T) = P_t × F_t / F_T`；
- 后复权：`P_hfq(t) = P_t × F_t`；
- 因果总收益：`R_t = (P_t × F_t) / (P_(t-1) × F_(t-1)) - 1`；
- 固定基日 `B` 的总收益指数：`TRI_t = 100 × P_t × F_t / (P_B × F_B)`。

前复权的 `F_T` 随截止日变化，所以 `qfq_asof` 强制传入 `as_of_date`，并先删去其后的全部记录。跨时点训练默认使用不依赖未来锚点的 `causal_returns`。后复权和前复权用于画图或特征时必须保留模式、版本和锚点；实际成交撮合始终使用当时未复权价格。

OHLC 使用同一比例调整以保持价格区间一致。成交量和成交额不进行机械复权；市值、换手、流通股本等特征必须读取相应的历史字段，不能用复权价格反推。平台要求每个行情键都有正且有限的因子，禁止对因子做前向/后向填充。

## 样本隔离

研究集按真实交易日历定义 train、validation 和 holdout：

- `label_horizon_sessions` 决定每段末尾需要 purge 的交易日，防止未来收益标签穿越边界；
- 相邻阶段之间至少保留 `embargo_sessions` 个完全不用的交易日；
- features 与 labels 分文件保存，目标列不允许出现在特征文件；
- 开发模式只允许读取 train/validation；读取 holdout 会拒绝并写入带前序哈希的防篡改访问链；
- 最终评估模式需要与 `dataset_id`、manifest SHA-256 和用途完全匹配的授权文件。

当前访问闸门属于应用层控制，可防止研究脚本和 Agent 误用并提供审计证据；它不是操作系统级 ACL。多人正式生产环境还应把 holdout 放入独立对象存储权限域，由 CI 或研究负责人签发不可伪造的短期凭证。

## 回测上线前的额外硬门槛

当前价格层已经阻断复权前视，但策略回测尚不能仅凭日线和复权因子投入使用。下一阶段必须接入并校验：每日停复牌、涨跌停价、上市/退市与 ST 历史、公司行动明细、交易日历、历史指数成分、手续费/印花税和滑点。缺失行情必须区分“停牌/未上市”与“数据缺口”；买入涨停、卖出跌停和停牌成交不得假设成功。

## 可执行验证

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest

.venv/bin/qrp-data build-prices \
  --start 2024-01-02 --end 2024-01-05 \
  --mode causal_returns

.venv/bin/qrp-data build-prices \
  --start 2024-01-02 --end 2024-01-05 \
  --mode qfq_asof --as-of-date 2024-01-05
```

合成公司行动测试固定验证 1:2 除权场景；时点测试验证收盘前不可读取当日日线；隔离测试验证 purge、embargo、holdout 拒绝、授权匹配和访问留痕。
