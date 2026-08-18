# 复现与运行指南

## 1. 复现范围

本项目把“代码可运行”和“授权实证数据可重建”分开说明：

- 克隆仓库即可复现代码环境、测试、schema 和报告生成逻辑；
- 正式全市场实证重算需要 Tushare/FRED 权限以及对应的不可变 raw lake；
- 授权数据不进入 Git，正式报告通过 artifact_id、SHA-256、Git commit 和环境锁保留证据链。

## 2. 安装

```bash
git clone https://github.com/EddieDeng0207/quant_research_platform.git
cd quant_research_platform
python3 -m venv .venv
.venv/bin/pip install -e '.[china,dev]'
```

项目要求 Python `>=3.9`。正式研究环境还应与 `requirements.lock` 对齐。

## 3. 密钥与数据安全

在项目根目录创建 `.env`：

```bash
TUSHARE_TOKEN=your_token
FRED_API_KEY=your_key
```

`.env` 已被 Git 忽略。日志和 manifest 只允许记录密钥是否存在，不允许记录密钥内容。提交前运行：

```bash
git status --short
```

确认 `.env`、授权 Parquet 和本地 artifact 没有进入暂存区。

## 4. 无授权数据的验证

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/qrp-data providers
.venv/bin/qrp-data --help
```

单元测试覆盖复权、时点隔离、PIT 财务、历史行业、P0.5 可交易性、因子评测、成交约束、公司行为、容量和组合账本。

## 5. 数据接入

查看供应商能力：

```bash
.venv/bin/qrp-data providers
```

全市场日线、复权因子和每日估值回补：

```bash
.venv/bin/qrp-data backfill-p0 \
  --start 2023-01-01 \
  --end 2023-12-31 \
  --workers 8 \
  --requests-per-minute 400
```

全市场财务回补：

```bash
.venv/bin/qrp-data backfill-fundamentals \
  --start 2014-01-01 \
  --end 2026-08-14 \
  --by-period-vip \
  --workers 8 \
  --requests-per-minute 400
```

历史行业回补与 PIT 构建：

```bash
.venv/bin/qrp-data backfill-industry --requests-per-minute 400
.venv/bin/qrp-data build-industry-pit \
  --run artifacts/ingestion_runs/<run_id> \
  --start 2016-01-01 \
  --end 2026-08-14
```

批量任务保存 checkpoint；相同配置续跑时只跳过已经成功并通过身份校验的任务。

## 6. 构建 P0.5 可交易域

```bash
.venv/bin/qrp-data backfill-p05 \
  --start 2023-01-01 \
  --end 2023-12-31 \
  --requests-per-minute 400

.venv/bin/qrp-data build-tradability \
  --start 2023-01-01 \
  --end 2023-12-31
```

P0.5 默认失败关闭。无法解释的缺行情、有行情但缺官方涨跌停记录或价格越过官方边界，都会阻止 artifact 晋级。

## 7. 构建 SP 因子输入

```bash
.venv/bin/qrp-data build-sales-to-price-inputs \
  --fundamentals-artifact data/curated/fundamentals/artifact_id=<P08_ID> \
  --tradability-artifact data/curated/tradability/artifact_id=<P05_ID> \
  --industry-artifact data/curated/industry/artifact_id=<INDUSTRY_ID> \
  --research-universe cn_a_sw_l1_core \
  --start 2016-01-01 \
  --end 2026-08-14 \
  --research-as-of-at 2026-08-17T08:00:00Z
```

输入构建会按决策日重放财报修订，使用“本年累计 + 上年全年 − 上年同期”构造 TTM 营业收入。缺失组件、金融专用报表或业务不连续重组不会被乐观填补。

## 8. P0.7 到 P0.6.3

晋级后的 P0.7 因子通过通用交接层生成目标权重和滞后容量：

```bash
.venv/bin/qrp-data build-factor-execution-inputs \
  --factor-artifact data/curated/factor_evaluations/artifact_id=<P07_ID> \
  --warmup-tradability-artifact data/curated/tradability/artifact_id=<WARMUP_P05_ID> \
  --execution-tradability-artifact data/curated/tradability/artifact_id=<EXECUTION_P05_ID> \
  --execution-year 2023 \
  --research-as-of-at 2026-08-17T08:00:00Z
```

运行成交、成本和容量回测：

```bash
.venv/bin/qrp-data build-backtest \
  --tradability-artifact data/curated/tradability/artifact_id=<P05_ID> \
  --capacity data/curated/factor_execution_inputs/artifact_id=<INPUT_ID>/capacity.parquet \
  --targets data/curated/factor_execution_inputs/artifact_id=<INPUT_ID>/targets.parquet \
  --corporate-actions data/curated/corporate_actions/artifact_id=<ACTION_ID>/corporate_actions.parquet \
  --max-stale-nav-bound-pp 2.0 \
  --initial-cash 10000000
```

生成报告：

```bash
.venv/bin/qrp-data report-backtest \
  --artifact data/curated/backtests/artifact_id=<BACKTEST_ID> \
  --output docs/research/<report_name>.md
```

## 9. 正式运行检查表

正式研究运行前后至少确认：

- 工作区 clean，研究代码绑定 Git commit 而非 dirty tree；
- `research_as_of_at`、样本区间、股票池和因子方向已冻结；
- 输入 manifest、Parquet 和环境锁哈希一致；
- P0.7 与 P0.6.3 的 hard failures 全部逐项上报；
- 工程晋级与投资结论分开表述；
- gross、cost、net、容量和未成交路径同时报告；
- 报告包含数据边界、模型假设和不能推出的结论。

更完整的门禁定义以 `docs/` 下各阶段标准为准，CLI 参数以当前版本的 `--help` 为准。
