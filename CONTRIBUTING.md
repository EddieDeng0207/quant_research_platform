# Contributing

本项目欢迎以可复现、可审计的方式扩展数据源、因子、执行模型和研究报告。

## 开发环境

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[china,dev]'
.venv/bin/pytest
.venv/bin/ruff check src tests
```

## 分支与提交

- 从最新 `main` 创建主题分支；
- 一个 PR 只解决一个清晰问题；
- 提交信息说明“改变了什么”，不要只写 `update`；
- 不使用 destructive Git 命令覆盖他人的本地修改；
- 正式 artifact 只能由 clean Git tree 生成。

## 代码要求

- 新功能应有成功路径、边界条件和失败关闭测试；
- 数据字段、时间语义或 schema 变化必须同步更新文档；
- 供应商回退规则必须记录来源、适用区间和是否允许用于研究特征；
- 不能通过放宽门禁来隐藏数据质量或执行问题；
- 代码使用清晰的 spec、artifact 和 manifest 边界，避免研究参数散落为硬编码。

## 研究要求

研究 PR 应在读取结果前冻结：

- 因子公式与方向；
- 股票池、样本区间和决策频率；
- `research_as_of_at` 与 outcome 截止时间；
- 中性化、分组和统计口径；
- 初始资金、执行情景、费用与容量假设；
- 晋级门禁和允许的解释边界。

报告必须同时披露毛收益代理、费用、滑点、净收益、回撤、换手、容量和未成交路径。工程门禁通过不能表述为实盘承诺。

## 数据安全

禁止提交：

- `.env`、API token、Cookie 或账户凭据；
- 受授权约束的原始数据；
- 未经过滤的券商成交回报或账户信息；
- 能够反推出密钥或个人身份的日志。

大型数据和正式 artifact 保存在内容寻址数据湖；Git 只保存代码、schema、配置、报告和可验证的哈希身份。

## Pull Request 检查

提交 PR 前确认：

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests
git diff --check
git status --short
```

PR 描述应使用仓库模板，明确测试结果、artifact、研究边界和文档更新。
