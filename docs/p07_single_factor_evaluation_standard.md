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
`label_start_at`、`label_end_at`、`outcome_observation_end_at` 和
`forward_return`。`outcome_observation_end_at` 是全文件唯一的冻结样本终点；
标签文件必须保留超出样本终点的结构性空标签行。系统强制要求：

```text
event_at <= available_at <= decision_at < execution_at
ingested_at <= decision_at
label_start_at >= execution_at
label_end_at > label_start_at
```

只有 `label_end_at > outcome_observation_end_at` 的空标签属于样本末端自然截断，
可以从该 horizon 的匹配率分母剔除。样本内部缺行或
`label_end_at <= outcome_observation_end_at` 但收益为空，均记为意外缺失。这样
长持有期不会因自然截断被误杀，整期数据缺口也不会被错当成结构性尾部。

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

机构周频档位默认每期不少于 200 个有效证券，IC 最少 100 只，每个 horizon
至少 104 期（约两年周频），且每期覆盖率不得低于 80%。测试或探索可以显式
降低这些值，但参数会进入产物身份，不能冒充正式档位。缺失值不做横截面均值
填充，也不使用未来值回填。

中性化的动态样本下限为：

```text
max(configured_min_cross_section, neutralization_industry_count + 3)
```

其中行业虚拟变量列数加一列对数市值，并额外保留两个残差自由度。这个下限
依赖当期实际行业数，因此在读取截面后执行，而不是由无法看到数据的 dataclass
`validate()` 猜测。

### 2. 稳健缩尾

默认采用逐期中位数绝对偏差：

```text
robust_sigma = 1.4826 * median(|x - median(x)|)
lower/upper = median(x) +/- 5 * robust_sigma
```

MAD 为零意味着该期因子退化，系统不切换到另一套隐式算法，而是记录失败。
选择 MAD 而不是固定全样本分位点，是为了降低极端值影响并避免跨期信息混用。

### 3. 稀疏行业与行业、市值中性化

默认要求每个中性化行业至少 5 只证券。低于门槛的原始行业先合并为
`__OTHER__`，同时保留原始 `industry_code` 供组合暴露审计。如果合并后的
`__OTHER__` 仍不足 5 只，该期直接失败。这样避免单成员行业的独热列把该证券
残差机械压成零并永久塞进中间组。

缩尾值先做截面标准化，再回归于完整行业虚拟变量和标准化对数市值：

```text
z_winsor = industry_dummies * beta_industry
          + z(log_market_cap) * beta_size + residual
```

默认使用 `sqrt_market_cap` 作为 WLS 观测权重，降低极小市值噪声对行业基准的
支配，同时保留对小市值证券的研究覆盖。也可显式冻结为等权。最终因子为
回归残差的加权标准分；预期方向为负的因子乘以 `-1`，使较高
`signal_score` 始终代表较高预期收益。

每个决策日保存逐行业加权残余均值和因子与对数市值的加权相关系数。它们是
WLS 一阶条件，只用于验证线性代数和数值实现，正式名称为
`full_cross_section_sanity`；绝对值超过 `1e-8` 说明实现或数值异常，但其为零
不代表尾部组合中性。

真正的风险暴露门禁逐 quantile 计算：

```text
industry_active_weight(q, j)
  = weight(q, industry_j) - weight(universe, industry_j)

size_active_z(q)
  = mean_q(z(log_market_cap)) - mean_universe(z(log_market_cap))
```

固定阈值会随股票池规模产生不同的抽样显著性，因此门禁使用噪声感知阈值：

```text
industry_se(q, j) = sqrt(p_j * (1 - p_j) / n_q)
industry_limit(q, j) = max(5%, 4 * industry_se(q, j))

size_se(q) = std_universe(z(log_market_cap)) / sqrt(n_q)
size_limit(q) = max(0.25, 4 * size_se(q))
```

其中 `p_j` 是全样本行业权重，`n_q` 是该 quantile 的证券数。默认使用 4 倍
抽样标准误；大股票池由固定下限约束，小股票池按自然抽样噪声放宽。每行同时
保存 `sampling_standard_error`、`exposure_limit` 和
`standardized_exposure`，避免把“样本小”和“真实风格下注”混成同一件事。
Top 组是送入 P0.6.3 的实际目标组合，因此不能用全截面均值正交替代组合
暴露审计。

## 评测方法

- Pearson IC：衡量线性关系；
- Rank IC：使用平均秩处理并列值，衡量排序关系；
- 分层收益：默认五组、组内等权；
- 多空分层差：最高组减最低组，仅作为诊断；
- 单调性：分组序号与分组平均收益的相关系数；
- 衰减：同一信号在不同 `horizon_sessions` 下分别统计；
- 换手：Top 组等权多头目标的单边权重变化；组合从 100% 现金开始，统一使用
  `0.5 * sum(abs(delta_weight))`，因此首次 98% 建仓的单边换手为 98%，后续
  也使用同一公式；
- 年度稳定性：逐自然年、逐持有期独立汇总；
- 频率校验：从相邻 `decision_at` 的中位日历间隔推断实际年频，与冻结的周频
  52 比较，默认相对偏差超过 15% 直接失败；
- 显著性：每个 horizon 自动使用
  `ceil(horizon_sessions / inferred_trading_sessions_per_period)` 阶
  Newey-West，且不少于冻结的最小阶数；
- ICIR：使用同一 Newey-West 长期方差计算 HAC 年化 ICIR，不再用朴素标准差
  和 `sqrt(52)` 混用方差假设。

默认主报告口径为“中性化因子对原始未来收益”，与常见因子研究口径一致；
同时对未来收益使用相同的行业/市值 WLS 残差化，并保存原始收益和残差收益的
IC、Rank IC、分层差。`return_basis` 可冻结为 `raw` 或 `residualized`，主表只
展示选定口径，禁止跨口径直接比较。

收益残差化按 `(decision_at, horizon_sessions)` 执行一次 WLS。若该标签截面因
缺失导致设计矩阵秩不足，系统保留原始收益评测，记录
`residualization_status`、`residualization_error` 并把当期口径标为
`raw_fallback`。主口径为 `raw` 时这只是诊断缺失；主口径明确要求
`residualized` 时，任何回退都会阻止晋级。500 周、3 个持有期对应约 1500 次
收益回归，首次全市场运行前需要记录墙钟时间和峰值内存，再决定是否缓存同一
决策日的加权投影矩阵。

P0.7 不以 IC、t 值或分层差是否为正作为工程晋级门禁。这样可以保证负面研究
结果不会因“没有 alpha”被删除。多因子批量比较时再使用实验登记中的
Benjamini-Hochberg FDR 控制。

## 不可变产物

`build-factor-evaluation` 按两个输入文件、冻结参数、实现文件、Git 状态和依赖
锁生成内容寻址的 `artifact_id`，输出：

- `factor_panel.parquet`：原始、缩尾、标准化、中性化和最终信号；
- `coverage.parquet`、`distribution.parquet`；
- `factor_exposures.parquet`；
- `label_coverage.parquet`：逐 horizon 的结构性尾部、内部缺失和匹配率；
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
- 任一 horizon 排除结构性尾部后的标签匹配率低于默认 95%；
- 任一持有期少于默认 104 个有效周频评测期；
- 任一期缺少最高或最低分组；
- WLS 一阶条件 sanity check 超过 `1e-8`；
- Top/Bottom 任一行业主动权重超过 `max(5%, 4 * sampling_se)`；
- Top/Bottom 对数市值标准分偏离超过 `max(0.25, 4 * sampling_se)`；
- 主收益口径要求残差收益但当期收益残差化失败；
- 实际决策频率与冻结年化频率不一致；
- 稀疏行业合并后仍不足最低成员数。

门禁只证明数据、时点、统计和工程过程达到标准，不证明因子未来有效。

## 与执行回测的边界

`target_weights.parquet` 采用 Top 组等权、多头、默认 98% 总权重，与 P0.6.3
默认 2% 现金缓冲一致。真实可实现收益必须继续经过 P0.5/P0.6.3 的停牌、
涨跌停、T+1、佣金、动态冲击、部分成交和容量约束。P0.7 的 IC 与分层收益
不能替代该执行回测。
