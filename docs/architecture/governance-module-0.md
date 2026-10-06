# Module 0 — Architecture Governance Stabilization

日期：2026-10-05。范围仅为危险脚本审计/隔离、架构例外清单及 AI 修改规范阅读入口。归属：工程维护和规范入口；不是 Core 重构、业务修复或 Prompt Pipeline 开发。

## 1. `_tail_errors.py` 风险报告

```text
文件：scripts/_tail_errors.py（审计时未被 Git 跟踪）
引用：除脚本自身及原审计文档外，未发现项目调用
执行路径：已检查的生产启动、测试及动态 Skill 加载路径不调用该文件
风险：显式运行/导入即连接当前配置账本，批量 unknown → failed → released
处理建议：原样隔离，禁用正常 Python 发现/加载，保留证据；不得执行对账
处理结果：scripts/archive/_tail_errors.py.disabled；原路径已不存在
```

脚本查询 `status='unknown' AND provider_job_id IS NULL`，没有查供应商明确拒绝、实际费用或单个请求的可信对账依据。`budget.release` 本来拒绝 unknown，但脚本先调用 `mark_outcome(..., 'failed')`，再 release，因而绕过“unknown 不等于失败”的安全语义。没有 main guard，导入也有数据库副作用。

没有执行原脚本，也没有使用 fake budget 导入它来验证；风险判断来自 AST/源码、现有账本转换函数及已有离线状态测试。没有修改真实账单，没有证据证明脚本已造成实际损失。

### 引用与可达性证据

| 检查项 | 文件 / 位置或范围 | 结果 |
| --- | --- | --- |
| 名称/路径直接引用 | src、skills、scripts、tests、frontend、docs、.workbuddy、.run、.vscode、.idea、pyproject.toml、start-studio.cmd | 隔离前仅原脚本及 audit-report 文档匹配；无 import/调用/启动引用 |
| Python import / 执行函数 | 99 个 src Python、6 个 Skill handler、8 个 scripts、60 个测试、18 个 .workbuddy Python | 检查 import、import_module、__import__、runpy、exec、eval、subprocess、glob 及其调用位置；未发现加载该脚本 |
| 动态 Skill loader | `src/kantoku/core/skills/loader.py:25–30,39–69`，10 个 manifest | 只遍历 skills 内 manifest；handler 路径必须在 skills 根目录内；不扫描 scripts 或 archive |
| 生产后端入口 | `src/kantoku/__main__.py`、`scripts/run_backend.py`、`start-studio.cmd` | 指向既有 serve；没有尾账维护 hook |
| IDE/前端启动 | `.run/Kantoku Backend.run.xml`、.idea 本地 Run 配置、.vscode launch/tasks、frontend/package.json | 后端 run_backend/旧 CLI；前端 npm dev/build/test；没有维护脚本调用 |
| subprocess | web_studio 的 `_port_owner_pid` | 只执行 netstat，不启动维护 Python |
| 测试动态执行 | `tests/test_director_live_eval.py:15,28`；`tests/test_smoke.py:28` | run_path 只加载 verify_director_pipeline；import_module 只加载列明的 kantoku 子包 |
| Python glob | check_structure、test_core_runtime、Studio 数据文件枚举等 | 源码读取/检查或 JSON 数据枚举，不批量执行 scripts |
| 项目 Git hook | `.git/hooks`、有效 core.hooksPath 查询 | 本地 hook 目录仅 .sample；未配置替代 hook 目录，未发现调用 |
| 本项目虚拟环境启动 | `.venv/Lib/site-packages/_virtualenv.pth`、`_editable_impl_kantoku_agent.pth` | 前者加载 _virtualenv；后者仅指向 src；没有维护脚本路径 |
| 打包/测试发现 | pyproject.toml | wheel 只包含 src/kantoku；pytest testpaths=tests，不自动发现 scripts 下此文件 |

结论：在当前项目可检查的生产路径中未发现可达调用。原文件仍可被人工命令或把 scripts 加到 sys.path 后误导入，因此隔离有必要。项目外部计划任务、其他电脑的脚本和历史执行记录没有核验，不把静态“未找到调用”写成“从未执行过”。

搜索最初触及无权限的 uv/pytest 缓存，随后明确限定上述项目源码、配置、IDE/启动和测试目录重新搜索；缓存/生产数据库/日志不属于此次执行入口证据，不据此声称检查过其全部内容。

### 隔离方式与完整性

- 仅移动这一份脚本，不删除其内容，不复制第二份实现。
- 目标：`scripts/archive/_tail_errors.py.disabled`；隔离前后 SHA-256 相同：`7450d4d9e9e4d8124cb14a66a717b143bf2d5eda29a8e2e15f93283445997be6`。
- 原 `scripts/_tail_errors.py` 路径不存在；`.disabled` 不进入普通 Python 模块发现及 `.py` 文件扫描。归档目录没有可执行 `.py`。
- [archive/README](../../scripts/archive/README.md) 明确禁止执行、runpy/自定义 loader、扫描运行及恢复后缀。
- 后缀隔离不是安全沙箱：显式 `python 文件` 或自定义 loader 仍可能执行任意扩展名内容。此文件仅供只读审阅，恢复/安全对账须独立授权和验收。

## 2. 架构例外清单

新增 [exceptions.md](exceptions.md)，按具体文件登记已有债务、保留原因、新代码边界、退出条件和拟治理模块。tools、adapters、video 目录没有被整体豁免；无证据释放 unknown 账单也不列为例外。

Core 不迁移、不重写。现有接口、旧实现、用户记录及默认业务路径保持。新增代码仍须遵守边界；例外不能作为继续污染或另起系统的理由。

## 3. AI 修改规范入口

在当前 AGENTS 内容上追加必读链接，不覆盖用户此前重写的规则。工程 README 同步明确相同顺序：

```text
AGENTS.md
→ docs/engineering/AI_CODE_RULES.md
→ docs/product/product-principles.md
→ docs/engineering/README.md
→ 相关架构例外及实际调用方
```

AI_CODE_RULES 补充例外入口及隔离目录要求。这里只建立使用入口，没有添加 CI、第二套 linter、自动重构或运行时强制加载。

当前 Comic 完整 Prompt 仍由 DirectorSpec → 原 PromptCompiler → Core Prompt Artifact 产生；没有新增 PromptBuilder/Helper/Manager/Generator，没有修改 UI 或图片/视频路径。

## 4. 验证及交付状态

本次实际执行的验证：

| 验证 | 结果 |
| --- | --- |
| `uv run pytest tests/test_budget.py tests/test_core_runtime.py tests/test_skills_video.py tests/test_smoke.py` | 41 passed；临时隔离库/Mock，不调用真实模型 |
| `uv run python scripts/check_structure.py` | PASS；未扩展现有 guard，也不声称覆盖所有架构语义 |
| 原路径不存在、归档 SHA-256、归档无 `.py` | PASS；原内容未改 |
| PathFinder 只读检查原 scripts 和 archive 模块发现 | PASS；`_tail_errors` / `scripts.archive._tail_errors` 均无法普通发现，没有执行归档内容 |
| 规范入口、例外、报告、archive README 的本地链接及代码块 | PASS |
| src/skills/tests/frontend/src 的 248 个非缓存文件 hash | 与本模块开始时完全一致；既有未提交 UI/源码/测试保留 |
| 暂存区 | 仍仅原 WorkspaceShell 重命名，未暂存本模块文件 |

现有预算回归包括 unknown 保持预占/不能直接释放、明确失败允许释放、提交认领不重复等场景；没有为了验证危险脚本而执行其释放逻辑。预算测试的合法状态转换发生在隔离测试库，不连接当前业务账本。

前端、运行入口和业务代码未修改，因此本次未执行会覆盖当前静态 bundle 的前端构建，未启动真实后端或 Provider；未修改测试、新增依赖、运行时权限或账本 Schema。

本模块修改清单：AGENTS、工程 README、AI_CODE_RULES 的最小阅读入口/冻结规则；新增 exceptions、此风险报告及 archive README；将唯一危险文件原样隔离。新增文档分别承载例外基线、可达性与验证证据、归档禁用说明，没有新增业务实现。

2026-10-06 用户通过 Module 0 整体验收，授权仅提交本模块；推送不在本次授权内。按用户最新确认，提交完成后优先进入 Prompt 链路（EX-11/EX-12），然后 Trace（EX-10）、状态（EX-14）、Core 边界（EX-01～EX-04）。Core 全面迁移暂停，各模块仍须独立修改、测试、人工验收、commit。
