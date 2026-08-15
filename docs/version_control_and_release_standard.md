# P0.G0 版本控制、发布与研究代码身份标准

## 1. 仓库边界

Git 仓库根目录固定为 `quant_research_platform/`。上级个人研究目录不纳入本仓库。
本地 `.git/` 保存完整版本历史；GitHub 私有仓库保存远程副本与协作记录。

## 2. Git保存与排除范围

Git必须保存源代码、测试、脚本、配置、文档、依赖锁、小型manifest和研究报告。
以下内容不得进入Git：密钥、`.env`、虚拟环境、raw/curated数据、状态文件、缓存、
Parquet、CSV和大型source bundle。大型对象由内容寻址数据湖保存，Git只保存
artifact ID、SHA-256、schema、行数和非敏感位置。

## 3. 正式版本

- `main` 始终代表已通过测试的可复现基线；
- 开发使用短生命周期分支；
- 正式阶段使用注释Tag，例如 `v0.6.3`；
- 禁止改写已推送的正式Tag；
- 禁止对共享历史执行force push；
- 每次功能修改必须包含测试或明确的验证记录。

## 4. 正式研究代码身份

正式artifact和experiment必须记录：

```text
git_commit
git_tree
branch
exact_tag（如存在）
normalized_remote
working_tree_clean
dirty_state_sha256（仅探索运行）
environment_lock_sha256
implementation_sha256
```

哈希只证明内容身份；Git commit和source bundle提供恢复路径。两者必须并存。

## 5. Clean-tree晋级门禁

正式CLI默认要求：

```text
存在Git仓库
HEAD存在正式commit
工作区无未提交修改
requirements.lock存在
输入artifact哈希通过
测试和数据质量门禁通过
```

`--allow-dirty-code` 只允许探索性回测。探索产物必须记录dirty fingerprint，
不得进入正式候选池、最终保留集或对外交付报告。

## 6. 实验注册

`research_experiment_v2` 必须绑定clean Git commit、Git tree、环境锁、输入artifact、
参数、随机种子和完整已提交源码tar包。调用者列出的关键代码文件只用于可读性，
不再承担完整代码身份。

## 7. 依赖锁

`pyproject.toml` 描述兼容范围，`requirements.lock` 冻结当前可复现环境。正式运行
记录Python、平台、精确依赖版本和锁文件SHA-256。升级依赖必须单独commit并运行
全量测试，不得在因子结果变化时同时静默升级环境。

## 8. Schema与迁移

旧artifact永不原地改写。读取器按schema显式分派；迁移生成新artifact并引用
旧artifact ID、迁移代码commit和新旧schema。禁止通过覆盖文件“升级”历史产物。

## 9. GitHub安全

远程仓库必须为private。密钥即使在private仓库中也禁止提交。若密钥误入commit，
必须先撤销并轮换密钥，再清理历史；仅删除当前文件不足以消除Git历史中的密钥。

## 10. 发布验收

每个正式Tag至少验证：

- `pytest`全量通过；
- `ruff check`通过；
- `git status --short`为空；
- 远程Tag与本地commit一致；
- GitHub仓库visibility为PRIVATE；
- `.env`、数据目录和大型二进制未被Git跟踪；
- release manifest引用关键artifact与报告。
