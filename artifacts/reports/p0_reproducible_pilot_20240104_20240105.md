# P0 数据接入验收报告

## 结论

本批次通过验收。运行状态为 `completed`，数据湖完整性审计通过，未发现文件缺失、哈希不一致、行数不一致或数据契约错误。

## 运行身份

- Run ID：`20260814T215512Z_e8a6698a494f_3b52c9`
- 开始时间：`2026-08-14T21:55:12.035183+00:00`
- 结束时间：`2026-08-14T21:55:21.964808+00:00`
- 源代码树 SHA-256：`6fac3e68667bf75d5e51a9dbf465eac8c3acd57ed7bfbe007652805dd88c2560`
- 配置 SHA-256：`e8a6698a494f2ddeaf8352c73b344ba65de0ac5c7c2de8c47270a6bca4ff081f`
- Checkpoint：`state/p0_reproducible_pilot_20240104_20240105_8995cb2cd5a76e95.json`

## 冻结配置

| 配置项 | 值 |
|---|---|
| `datasets` | `["daily_bars", "adjustment_factors", "daily_indicators"]` |
| `end_date` | `2024-01-05` |
| `exchange` | `SSE` |
| `include_instruments` | `False` |
| `job_name` | `p0_reproducible_pilot_20240104_20240105` |
| `max_attempts` | `3` |
| `requests_per_minute` | `120` |
| `retry_base_seconds` | `2.0` |
| `start_date` | `2024-01-04` |

## 本次产物

| 数据集 | 文件数 | 行数 |
|---|---:|---:|
| `adjustment_factors` | 2 | 10,730 |
| `daily_bars` | 2 | 10,662 |
| `daily_indicators` | 2 | 10,662 |
| `trading_calendar` | 1 | 2 |
| **合计** | **7** | **32,056** |

## 断点续传

- 本次完成任务：7
- 从 checkpoint 跳过：0
- 覆盖交易日：2
- 失败任务：0

## 数据湖完整性审计

- Manifest 条目：21
- 已检查文件：21
- 已检查行数：70,076
- 错误：0
- 警告：0
- 最终状态：`PASS`

## 可复现说明

相同数据范围、数据集、交易所和任务名称共享同一 checkpoint；改变限速或重试参数不会改变数据任务身份。每次运行仍生成独立 Run ID、配置快照和事件日志。密钥值不进入任何运行产物。

该报告由运行清单、事件日志和审计 JSON 自动生成，不接受手工修改指标。
