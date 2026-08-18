# Quant Research Platform

面向中低频、周频调仓研究的数据接入底座。当前版本优先解决：统一字段、单位、复权口径、公告可用时间和数据来源追踪。

## 首批数据源

| 数据域 | 主来源 | 备用/校验 | 当前状态 |
|---|---|---|---|
| A 股证券主数据、交易日历 | Tushare Pro | AKShare 当前列表 | 已接入 |
| 历史股票池、北交所新旧代码映射 | Tushare Pro | 北交所/巨潮公告人工复核 | 已接入 |
| A 股未复权日线 | Tushare Pro | AKShare | 已接入 |
| 涨跌停价、停复牌、ST 状态 | Tushare Pro | 交易所公告抽查 | 已接入（P0.5） |
| A 股复权因子 | Tushare Pro | 后续增加公司行为校验 | 已接入 |
| 现金分红、送转实施记录 | Tushare Pro | 交易所/巨潮公告抽查 | 已接入（P0.6.2） |
| 估值、市值和换手 | Tushare Pro `daily_basic` | 后续增加校验源 | 已接入 |
| 三张财务报表、财务指标 | Tushare Pro | 后续增加公告原文校验 | 已接入 |
| 宏观数据及历史版本 | FRED/ALFRED | 官方发布机构 | 已接入 |
| 分析师预期 | 待选择授权数据源 | 不使用事后网页快照替代 | 接口待建 |
| 另类数据 | 按数据集单独签订契约 | — | 接口待建 |

AKShare 适合免密启动和横向核验，但它聚合第三方网页接口，不能单独承担生产级历史主数据。Tushare 的前复权结果依赖查询结束日，因此平台保存未复权价格和独立复权因子，在 curated 层按固定规则生成复权序列。

## 安装

```bash
cd "/Users/eddiedeng/Desktop/发财与研究/quant_research_platform"
python3 -m venv .venv
.venv/bin/pip install -e '.[china,dev]'
```

密钥只通过环境变量提供：

```bash
export TUSHARE_TOKEN='...'
export FRED_API_KEY='...'
```

## 使用

查看数据源能力：

```bash
.venv/bin/qrp-data providers
```

CLI 会自动读取项目根目录下、已被 Git 忽略的 `.env`，但任何运行产物只记录密钥是否存在，不记录密钥值。

免密下载一只股票的未复权日线：

```bash
.venv/bin/qrp-data bars \
  --provider akshare \
  --symbol 600000.SH \
  --start 2024-01-01 \
  --end 2024-03-31
```

使用 Tushare 分开保存未复权价格和复权因子：

```bash
.venv/bin/qrp-data bars \
  --provider tushare \
  --symbol 600000.SH \
  --start 2024-01-01 \
  --end 2024-03-31

.venv/bin/qrp-data adjustments \
  --provider tushare \
  --symbol 600000.SH \
  --start 2024-01-01 \
  --end 2024-03-31
```

读取财务报表时，`--start/--end` 表示公告日期范围，而非报告期范围：

```bash
.venv/bin/qrp-data fundamentals \
  --provider tushare \
  --statement income \
  --symbol 600000.SH \
  --start 2023-01-01 \
  --end 2024-12-31
```

下载带历史实时区间的宏观数据：

```bash
.venv/bin/qrp-data macro \
  --series CPIAUCSL \
  --start 2010-01-01 \
  --realtime-start 2010-01-01 \
  --realtime-end 2024-12-31
```

运行或续跑全市场 P0 接入任务：

```bash
.venv/bin/qrp-data backfill-p0 \
  --start 2024-01-02 \
  --end 2024-01-05 \
  --workers 8 \
  --requests-per-minute 400
```

任务按交易日分别拉取全市场未复权行情、复权因子和每日市值指标。每个成功任务立即写入 checkpoint；相同配置再次运行时自动跳过已完成任务。

运行或续跑全市场财务数据任务（正式多年任务前先用 `--symbol` 做小样本验证）：

```bash
.venv/bin/qrp-data backfill-fundamentals \
  --start 2014-01-01 \
  --end 2026-08-14 \
  --by-period-vip \
  --workers 8 \
  --requests-per-minute 400
```

正式全市场任务优先按“报表 × 报告期”调用 VIP 全市场接口；普通权限仍可按
“报表 × 股票”保存不可变快照。三张报表的普通接口日期参数表示公告日范围；
`financial_indicators` 表示报告期范围。零行会保存为已覆盖快照。财务指标达到供应商
100 行返回上限时任务失败并要求缩短区间，不接受可能被截断的数据。

从完成的运行和冻结交易日历构建 P0.8 财务 PIT artifact：

```bash
.venv/bin/qrp-data build-fundamentals-pit \
  --run artifacts/ingestion_runs/<run_id> \
  --calendar data/lake/raw/provider=tushare/dataset=trading_calendar/<file>.parquet
```

正式回补前可生成研究就绪缺口报告：

```bash
.venv/bin/qrp-data audit-research-readiness \
  --start 2018-01-01 \
  --end 2026-08-14 \
  --calendar <calendar.parquet> \
  --fundamental-artifact <fundamental_artifact> \
  --industry-membership <historical_industry.parquet>
```

完整的双时间、修订选择和晋级规则见
`docs/p08_fundamental_pit_standard.md`。

历史行业归属使用申万2014/2021两个版本的历史成员区间，不允许把新版分类回填旧时期：

```bash
.venv/bin/qrp-data backfill-industry --requests-per-minute 400
.venv/bin/qrp-data build-industry-pit \
  --run <completed_run> \
  --start 2016-01-01 \
  --end 2026-08-14
```

分类体系在2021-12-13切换，完整标准见
`docs/p08_historical_industry_standard.md`。

接入并构建 P0.5 可交易性矩阵：

```bash
.venv/bin/qrp-data backfill-p05 \
  --start 2024-01-02 \
  --end 2024-01-05 \
  --requests-per-minute 400

.venv/bin/qrp-data build-tradability \
  --start 2024-01-02 \
  --end 2024-01-05
```

`backfill-p05` 冻结历史股票池、未复权行情、官方涨跌停价、停复牌事件、ST 状态、当前证券主数据和北交所新旧代码映射。构建阶段默认 fail closed：无法解释的缺行情、有行情却缺涨跌停记录或价格越过官方边界，都会阻止产物晋级。详细规则见 `docs/p05_tradability_standard.md`。

批量接入默认使用 400 次/分钟（每次真实 API 调用间隔 0.15 秒，分页和重试也分别计数）。分页不是统一填一个任意大值，而是使用各接口审阅后的最大页长：`daily/adj_factor/daily_basic/stock_basic=6000`、`stk_limit=5800`、`bak_basic=7000`、`suspend_d=5000`、`stock_st/bse_mapping=1000`、`dividend=2000`。每次写入同时记录页长、请求页数和总行数；发现供应商忽略 offset 并返回重复页时立即失败。摄取策略版本 `p0_tushare_max_page_v3_rpm400_vendor_sentinels_master_interval_fallback` 进入 checkpoint 数据身份，因此旧限速、旧分页或旧历史缺失处理任务不会让升级后的运行错误跳过。

从冻结订单和已晋级 P0.5 产物构建 P0.6 执行审计：

```bash
.venv/bin/qrp-data build-execution \
  --tradability-artifact data/curated/tradability/artifact_id=<P0.5_ID> \
  --orders artifacts/pilots/p061_orders_20240102_20240105.parquet \
  --initial-cash 1000000
```

P0.6.1 默认处理 T+1、沪深/北交所申报数量、方向性涨跌停、滞后成交量/成交额容量、自由流通盘、正常与压力退出天数、部分成交、现金与持仓会计、滑点、佣金、印花税和过户费。主情景参与率默认 1%，并保存保守成交与延迟一个交易日情景。详细规范见 `docs/p06_execution_standard.md` 和 `docs/p061_institutional_controls.md`。

接入并因果化公司行为：

```bash
.venv/bin/qrp-data corporate-actions \
  --symbol 000001.SZ \
  --start 2023-01-01 \
  --end 2024-12-31

.venv/bin/qrp-data build-corporate-actions \
  --raw data/lake/raw/provider=tushare/dataset=corporate_actions/.../part-....parquet \
  --tradability-artifact data/curated/tradability/artifact_id=<P0.5_ID>
```

运行 P0.6.3 逐日组合回测：

```bash
.venv/bin/qrp-data build-backtest \
  --tradability-artifact data/curated/tradability/artifact_id=<P0.5_ID> \
  --capacity artifacts/pilots/p063_capacity.parquet \
  --targets artifacts/pilots/p063_targets.parquet \
  --corporate-actions data/curated/corporate_actions/artifact_id=<ACTION_ID>/corporate_actions.parquet \
  --max-stale-nav-bound-pp 2.0 \
  --initial-cash 1000000

.venv/bin/qrp-data report-backtest \
  --artifact data/curated/backtests/artifact_id=<BACKTEST_ID> \
  --output artifacts/reports/p063_backtest.md
```

P0.6.3 沿用 P0.6.2 的严格事件时钟和组合会计，并加入滞后个股波动率、
平方根冲击、冲击约束成交量、最低佣金诊断、常规小额订单审计，以及超限
陈旧持仓的末价/零价 NAV 上下界。详细规范见
`docs/p062_portfolio_backtest_standard.md` 和
`docs/p063_execution_cost_standard.md`。

容量面板、目标权重订单和真实成交校准分别通过以下命令构建：

```bash
.venv/bin/qrp-data build-capacity-panel \
  --bars bars.parquet \
  --daily-indicators daily_indicators.parquet \
  --adjustment-factors adjustment_factors.parquet \
  --output capacity.parquet

.venv/bin/qrp-data build-target-orders \
  --targets targets.parquet \
  --positions positions.parquet \
  --prices raw_execution_prices.parquet \
  --capacity-panel capacity.parquet \
  --portfolio-nav 1000000 \
  --output orders.parquet

.venv/bin/qrp-data calibrate-execution \
  --fills broker_fills.csv \
  --minimum-group-samples 30
```

验证数据文件、清单哈希、行数和数据契约：

```bash
.venv/bin/qrp-data audit-lake
```

从不可变 raw 快照构建不依赖未来锚点的因果收益产物：

```bash
.venv/bin/qrp-data build-prices \
  --start 2024-01-02 \
  --end 2024-01-05 \
  --mode causal_returns
```

如需前复权展示，必须显式冻结知识截止日：

```bash
.venv/bin/qrp-data build-prices \
  --start 2024-01-02 \
  --end 2024-01-05 \
  --mode qfq_asof \
  --as-of-date 2024-01-05
```

完整的三时钟、复权公式、purge/embargo、最终保留集访问规则见 `docs/data_isolation_and_adjustment.md`。

## 存储约定

每次请求写入新的不可变 Parquet 文件：

```text
data/lake/
├── manifest.jsonl
└── raw/
    └── provider=tushare/
        └── dataset=daily_bars/
            └── ingest_date=YYYY-MM-DD/
                └── part-*.parquet
```

`manifest.jsonl` 保存请求参数（密钥除外）、行数、字段、转换说明和文件 SHA-256。后续 curated 层只能引用 raw 文件及其哈希，不能覆盖原始快照。

## P0.7 单因子研究

P0.7 已提供时点严格的单因子评测底座：因子观测与未来收益标签物理分表，
逐决策日完成 MAD 缩尾、行业/对数市值中性化、IC/Rank IC、分层、衰减、
HAC 统计、Top/Bottom 真实组合暴露、换手和年度稳定性评测；多持有期标签按
冻结样本终点区分结构性尾部与内部缺失，并输出可交给 P0.6.3 的多头目标权重。完整标准见
`docs/p07_single_factor_evaluation_standard.md`。

因子研究按“原始信号 → 量化选股合成 → 组合与交易”分层。首个基本面原始
因子 `sp_ttm` 已提供 PIT 输入构建命令：

```bash
.venv/bin/qrp-data build-sales-to-price-inputs \
  --fundamentals-artifact data/curated/fundamentals/artifact_id=<P0.8_ID> \
  --tradability-artifact data/curated/tradability/artifact_id=<P0.5_ID> \
  --industry-artifact data/curated/industry/artifact_id=<INDUSTRY_ID> \
  --research-universe cn_a_sw_l1_core \
  --start 2016-01-01 \
  --end 2026-08-14 \
  --research-as-of-at 2026-08-17T08:00:00Z
```

它逐决策日重放财报修订，使用“本年累计 + 上年全年 − 上年同期”构造 TTM
营业收入，遇到缺失组件、金融公司专用报表或业务不连续重组时不做填补。基本面决策日不引入量价形成窗口预热；120 个交易日的上市限制使用真实 `list_date` 和冻结交易日历计算。
完整分类、公式和审计标准见 `docs/factor_research_standard.md`。

## 当前边界

- manifest 已增加进程级文件锁；同一 checkpoint 当前仍只允许一个编排进程写入。未来并行 Agent 共享任务状态前，需要增加 checkpoint 锁或迁移到事务型任务存储。
- AKShare 股票列表只有当前截面，不能用于历史股票池。
- Tushare 权限受积分影响；当前全市场任务已实现分页、限流、指数退避和断点续传。
- 财报 raw 接口已保留公告日、实际公告日和修订行；P0.8 已完成全市场多年回补、下一交易日可用时间、不可变版本索引、决策时点版本选择和历史行业接入。
- 分析师一致预期必须使用能够提供历史快照/变更时间的数据源后再接入。
- P0.6.1 已提供真实券商成交校准产物和最小样本门禁，但在导入真实成交回报前，滑点参数仍明确标记为研究假设；日线无法证明盘口排队或集合竞价实际成交。
