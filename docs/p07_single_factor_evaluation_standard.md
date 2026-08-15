# P0.7 时点严格的单因子研究与评测标准

## 目标与范围

P0.7 将冻结的因子观测转换为可审计的截面信号，并完成覆盖率、分布、
IC/Rank IC、分层收益、单调性、衰减、换手、年度稳定性与行业/市值暴露评测。
它输出可直接交给 P0.6.3 组合执行回测的多头目标权重，但不把分层多空收益
冒充为 A 股现金账户可实现收益。

本阶段不负责定义具体基本面、分析师预期、另类或量价因子的经济公式，也不
负责多因子合成、组合优化和参数搜索。因子库与多因子模型在后续阶段接入本
标准。

## 物理隔离的输入契约

因子观测与事后收益标签必须是两个独立文件，并分别进入产物身份哈希。

因子观测至少包含：

- `instrument_id`：稳定证券身份，不使用可能发生复用的展示代码作为主键；
- `factor_value`：在决策时点可知的原始因子值；
- `industry_code`、`market_cap`：同一决策时点可知的历史行业与正市值；
- `research_eligible`：由历史股票池和 P0.5 约束产生的布尔资格；
- `decision_at`、`execution_at`：决策和最早执行时点；
- 因子族对应的事件时间、`available_at` 与 `ingested_at`；
- 分析师预期额外要求冻结的 `estimate_vintage_at`。

未来收益标签至少包含 `instrument_id`、`execution_at`、`horizon_sessions`、
`label_start_at`、`label_end_at` 和 `forward_return`。系统强制要求：

```text
event_at <= available_at <= decision_at < execution_at
ingested_at <= decision_at
label_start_at >= execution_at
label_end_at > label_start_at
```

收益标签只能参与事后评测，不能进入缩尾、中性化、排序或目标权重构建。
因子观测按 `(instrument_id, decision_at)` 唯一，收益标签按
`(instrument_id, execution_at, horizon_sessions)` 唯一；重复键直接失败。

## 截面处理

所有统计只在单个 `decision_at` 的横截面内计算，不允许跨期拟合缩尾边界或
标准化参数。

### 1. 资格与覆盖

先应用冻结的 `research_eligible`，再要求因子有限、市值为正、行业非空。
覆盖率定义为：

```text
usable eligible observations / all eligible observations
```

默认每期不少于 20 个有效证券，且每期覆盖率不得低于 80%。缺失值不做横截
面均值填充，也不使用未来值回填。

### 2. 稳健缩尾

默认采用逐期中位数绝对偏差：

```text
robust_sigma = 1.4826 * median(|x - median(x)|)
lower/upper = median(x) +/- 5 * robust_sigma
```

MAD 为零意味着该期因子退化，系统不切换到另一套隐式算法，而是记录失败。
选择 MAD 而不是固定全样本分位点，是为了降低极端值影响并避免跨期信息混用。

### 3. 行业和市值中性化

缩尾值先做截面标准化，再回归于完整行业虚拟变量和标准化对数市值：

```text
z_winsor = industry_dummies * beta_industry
          + z(log_market_cap) * beta_size + residual
```

默认使用 `sqrt_market_cap` 作为 WLS 观测权重，降低极小市值噪声对行业基准的
支配，同时保留对小市值证券的研究覆盖。也可显式冻结为等权。最终因子为
回归残差的加权标准分；预期方向为负的因子乘以 `-1`，使较高
`signal_score` 始终代表较高预期收益。

每个决策日保存逐行业加权残余均值和因子与对数市值的加权相关系数，绝对值
超过 `1e-8` 即视为实现或数值异常，不允许晋级。

## 评测方法

- Pearson IC：衡量线性关系；
- Rank IC：使用平均秩处理并列值，衡量排序关系；
- 分层收益：默认五组、组内等权；
- 多空分层差：最高组减最低组，仅作为诊断；
- 单调性：分组序号与分组平均收益的相关系数；
- 衰减：同一信号在不同 `horizon_sessions` 下分别统计；
- 换手：Top 组等权多头目标的单边权重变化，首次建仓按总投入权重计算；
- 年度稳定性：逐自然年、逐持有期独立汇总；
- 显著性：IC 与分层差使用默认 4 阶 Newey-West 长期方差修正；
- ICIR：按显式冻结的观测频率年化，周频默认 52。

P0.7 不以 IC、t 值或分层差是否为正作为工程晋级门禁。这样可以保证负面研究
结果不会因“没有 alpha”被删除。多因子批量比较时再使用实验登记中的
Benjamini-Hochberg FDR 控制。

## 不可变产物

`build-factor-evaluation` 按两个输入文件、冻结参数、实现文件、Git 状态和依赖
锁生成内容寻址的 `artifact_id`，输出：

- `factor_panel.parquet`：原始、缩尾、标准化、中性化和最终信号；
- `coverage.parquet`、`distribution.parquet`；
- `factor_exposures.parquet`；
- `ic_series.parquet`、`quantile_returns.parquet`；
- `turnover.parquet`、`target_weights.parquet`；
- `horizon_summary.parquet`、`annual_summary.parquet`；
- `manifest.json`。

正式 CLI 运行要求 clean Git；`--allow-dirty-code` 只允许探索产物。报告生成器
会先复核 manifest 中每个文件的 SHA-256，任何文件被改写后都拒绝生成报告。

## 硬晋级门禁

以下任一项非零即不允许晋级：

- 截面样本不足、因子退化或中性化矩阵秩不足；
- 任一成功处理期覆盖率低于冻结阈值；
- 未来收益标签匹配率低于默认 95%；
- 任一持有期少于默认 26 个有效评测期；
- 任一期缺少最高或最低分组；
- 行业残余均值或市值相关超过数值容忍度。

门禁只证明数据、时点、统计和工程过程达到标准，不证明因子未来有效。

## 与执行回测的边界

`target_weights.parquet` 采用 Top 组等权、多头、默认 98% 总权重，与 P0.6.3
默认 2% 现金缓冲一致。真实可实现收益必须继续经过 P0.5/P0.6.3 的停牌、
涨跌停、T+1、佣金、动态冲击、部分成交和容量约束。P0.7 的 IC 与分层收益
不能替代该执行回测。
