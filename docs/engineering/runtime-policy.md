# Runtime Storage 工程规范

此规范适用于所有开发者、AI、脚本和验收工具。用户授权的产物删除仍须经过原 Artifact 服务；运行环境清理不得代替业务删除。

## 唯一入口与目录

`config/runtime_paths.py::RuntimeManager` 统一校验、建目录、登记文件、原子写入和临时目录。`core/database.py::DatabaseManager` 是应用唯一 SQLite 连接工厂。`core/runtime/storage.py::FileCatalog` 是既有数据库里的通用文件目录，不能另建业务 Runtime 或 Artifact 系统。

| 目录 | 内容 | 生命周期 |
| --- | --- | --- |
| data/ | SQLite、原有 checkpoint/state、恢复资料和迁移备份 | 持久保留 |
| logs/ | 活跃应用日志与轮转日志 | 单文件 10 MB；已登记轮转日志 7 天、合计 256 MiB |
| cache/ | 可重建工具/模型缓存 | 已登记文件按访问/修改时间淘汰，容量 1 GiB |
| artifacts/ | 最终用户交付 | 不自动删除；写入预留 64 MiB 空间 |
| tmp/ | 临时计算、测试会话、原子写入暂存 | 使用结束清理；测试会话在下次测试启动时回收 |

默认 Runtime root 为项目根目录。可优先用 `KANTOKU_RUNTIME_ROOT` 指定绝对路径，或在 `config/settings.yaml` 的 `storage.runtime_root` 写同一路径（环境变量优先）。两者都留空即项目根。把 root 指到项目外可让运行数据完全不落在仓库里，但属于搬迁：`data/kantoku.db`、`data/images` 中已记录绝对路径的引用不会自动改写，切换前须评估。已有明确配置的非系统 Temp 数据路径兼容保留，不偷偷搬迁数据库、图片或历史引用。原子替换需要在目标卷暂存，唯一例外由 RuntimeManager 创建 `.kantoku-*` 同目录暂存文件并立即清理；显式导出目录不参与后台自动删除。

递归删除由 `runtime_paths.py::_remove_tree` 完成，**不使用 `shutil.rmtree`**。原因：`shutil.rmtree` 只在顶层判断符号链接，递归时不拒绝目录联接，一旦跟随就会删掉链接指向的真实数据。仓库里确实存在这类联接（`data/qa/*` 快照与 `tmp/staged-check` 下有 15 个 `frontend/node_modules` 联接指向真实依赖）。`_remove_tree` 对每个条目先判断链接，遇到符号链接或目录联接只删除链接本身，绝不进入目标；`remove_owned_directory` 仍额外拒绝顶层为重定向目录。

禁止数据库、日志、缓存和临时文件写入 Windows Temp，包括环境 TEMP/TMP/TMPDIR、LocalAppData/Temp、Windows/Temp 及重定向路径。SDK 的 Python 临时目录指向进程专属的 `tmp/staging/kantoku-sdk-<pid>-<uuid>`，不修改整个操作系统的 TEMP/TMP 环境。

## 文件登记与清理

登记包含 file_id、type、owner、created_at、expire_at、status、version 和路径。文件目录追加到当前 SQLite；首次为既有数据库增加目录表前，用 SQLite backup 保存一致性备份。恢复时使用备份副本，禁止用文件复制覆盖活跃 WAL 数据库。

`temp/cache/log/qa/test_session` 可以过期；`artifact/export/database/backup/recovery/audit` 不会自动过期。未决供应商响应 URL 和任务输入是恢复资料，不能归入 24 小时缓存。Run、Workflow、Checkpoint、Approval、Artifact、Budget、Runtime Event 与账本历史全部保留。

活动写入以 SQLite 事务登记跨进程 lease；进程存活时不删除。清理使用版本校验和 deleting → deleted 状态；文件删除失败记录 error_id/trace_id，不报告释放空间，下次只重试已登记临时文件。清理线程每 60 秒运行，启动与正常退出也执行；容量限制允许短暂超过一轮检查窗口，不是每次字节写入的硬配额。程序关闭期间不运行后台清理，重启时补查。

旧文件默认未分类、保留。旧 data/logs、data/qa、cache/tests、系统 Temp 和回收站本轮不删除。仅本规范启用后的指定工具缓存，以及 Loguru 本轮新轮转日志可登记并清理。活动 kantoku.log 始终不由容量清理删除。未知文件、用户图片、账本、备份不能按文件名或大小猜测删除。

测试默认放入 tmp/sessions/kantoku-test-*，独占自己的 Runtime root 与数据库；正常退出等待连接关闭后移除整个自有会话。一个小型 `.kantoku-session.json` 保存归属和 PID，支持进程异常终止后的恢复清理。恢复分两级：pytest 启动时调用 `RuntimeManager.sweep_abandoned_test_sessions()`，对 owner=kantoku-tests 且 PID 已退出的会话**立即**整目录删除，不再等待 24 小时；其余未登记或未分类的会话仍由每 60 秒的维护线程按 24 小时过期规则处理。PID 存活（含并发运行的另一次 pytest）、标记缺失或越界的目录一律跳过。未知目录或缺少有效标记的旧测试目录不得自动清理。失败信息由测试报告输出，不永久保留大媒体夹具。需要保存可交付报告时通过 RuntimeManager 明确登记 export/audit。

SDK 暂存目录复用同一标记、文件登记与进程租约。维护线程启动时登记归属，所有 Worker 停止后释放租约并删除本进程目录。异常遗留只在创建已超过 24 小时、过期且进程已结束后回收；有效租约和存活进程仍受保护。删除失败进入原错误日志，不报告已释放空间。`doctor` 将已登记 SDK 目录内文件计入对应目录归属，并报告 SDK 目录数量和临时区大小。

### 2026-10-12 测试存储修正（优先于上面的旧默认路径）

pytest 和 `isolated_runtime` 默认使用 `get_test_runtime_paths()`，会话位于用户目录的 `.kantoku-tests/<项目路径哈希>/tmp/sessions/kantoku-*`，不落源码目录，也不改变生产 Runtime、数据库或图片位置。正常结束与异常抛出均在连接关闭后直接删除自有会话；PID 已退出的异常遗留由下次测试启动及原 60 秒维护循环回收。目录为空后可以保留少量空父目录，不保留测试图片、日志或 SQLite。

创建测试会话、开始验收和回收异常测试会话前检查标准删除函数。发现宿主改写删除行为时拒绝创建/回收，记录原因，不关闭宿主安全守卫，不把清理失败称为释放空间。当前 Codex 环境的正常与失败退出实测可直接删除；其他宿主存在重定向时应在普通本机终端运行。操作系统层的未知重定向不由 Python 函数检查证明。

付费验收脚本也使用临时自有会话，图片副本、报告文件、日志及隔离数据库结束即删除，公开结果输出到终端。只有原稳定预算身份的少量累计费用记录仍为 audit，不因临时清理归零；未知用量保留上界。既有付费审计资料不自动删除。累计账本缺失时后续模块验收拒绝付费，不能新建预算身份绕过。旧残留清理的批量及单目录永久删除在本轮被自动审批拒绝（仅返回 `blocked by policy`），用户已选择保留待清理清单并先继续模块二。

## 数据库和退出

保持 WAL 模式；每个进程、每个数据库有一个有限的保活连接，业务事务仍使用独立连接。退出顺序：停止新请求、关闭 SSE、等待请求和 Worker、停止文件维护、checkpoint(TRUNCATE)、关闭数据库连接。其他进程或未关闭事务占用时报告 Warning/Error 并保留 WAL。禁止手工 unlink WAL/SHM。强制结束进程、断电时不承诺没有侧文件。

所有调用方显式 close，SQLite `with connection` 仅管理事务，不会关闭连接。不同进程单独建立 DatabaseManager；不得跨 fork 复用连接。测试中检查完数据库也要关闭独立检查连接。

PyCharm 的 `scripts/run_backend.py` 委托标准 `kantoku.__main__.main`，复用信号处理、Web/Worker/维护线程退出及 SQLite checkpoint。强制杀进程仍按异常恢复处理。

## 日常检查和开发约束

执行 `python -m kantoku doctor`：仅检查路径、数据库 quick_check/WAL、孤儿侧文件、日志/缓存/临时大小、过期与未分类文件，输出 Healthy / Warning / Error。检查不创建目录、数据库、日志，不 checkpoint、不删除，不默认调用付费模型。供应商配置检查仍保留；`--live` 单独显式启用。

Vite 缓存使用 cache/vite，ruff 使用 cache/ruff；pytest 自带缓存隔离到测试 Runtime。应用导入停止后续 Python 字节码写入；PyCharm 启动配置使用 -B。验证建议 `uv run python -B -m pytest`，避免测试框架导入阶段的字节码。虚拟环境和 node_modules 是安装依赖，不等于可以删除的运行垃圾。

禁止在根目录创建临时文件，随手增加 temp/output/cache/data 子树，创建没有生命周期的生成文件或重复数据库，长期保存大测试图片/视频。业务模块禁止直接 mkdir、write_bytes、write_text 或 tempfile；使用 RuntimeManager。结构检查的 AST 规则拦截直接 SQLite 和常见写文件入口，但不替代代码审查。

不新增依赖；不修改业务、费用或审批逻辑。交付压缩包排除 data/logs/cache/artifacts/tmp、数据库、虚拟环境、node_modules 和 QA 文件；保留后端必需的 src/kantoku/shells/web 构建页面。
