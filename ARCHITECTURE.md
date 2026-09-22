# Kantoku 当前工程架构（2026-09-21 代码审计）

本文件记录当前被导入、被启动的实现，不提出新框架。主产品范围与里程碑仍以 `docs/03-项目开发流程.md` 为准。

## Backend canonical 链路

`pyproject.toml` 用 Hatchling 安装 `src/kantoku`；正式命令为 `python -m kantoku serve`。`src/kantoku/__main__.py` 初始化日志，再进入 `shells/web_studio.py` 的 `serve()`、`StudioApplication` 和本机 HTTP Handler。配置唯一入口是 `config/settings.py:get_settings()`，从仓库根目录 YAML 和 `.env` 读取；密钥不在命令、日志或源码中。

`shells/` 是 HTTP/CLI 边界与 UI 静态服务；`core/runtime/{graph,store,runner}.py` 是 Run、Workflow、NodeExecution、Checkpoint、RuntimeEvent、Batch 的执行与持久化主实现；`core/approval/`、`core/artifacts/`、`core/skills/`、`core/budget.py` 维护通用能力。`domains/commerce/`、`domains/comic/` 定义领域 State/Workflow；`adapters/` 对接或 Mock 外部服务；`capabilities/` 放复用能力。依赖遵循 shell → domain/core → config，领域经注入端口使用 Adapter，不反向导入 Adapter。`core/tracing.py` 的旧模型调用 SQLite trace 与 `config/observability.py` 的 HTTP Trace ID 是两种不同记录，不可互相替换。

每个 HTTP 请求只生成或继承一个安全的 `trace_id`，进入 Loguru context；`TaskRunner` 复制 context 到执行线程。RuntimeEvent 写入时记不含业务 payload 的日志。错误由 `public_error()` 分类，返回安全消息、`trace_id`、`error_id`，同一 ID 的异常类型与 traceback 文件/行在应用日志中可检索。重启后的新请求有新 Trace ID；持久 Run ID 与事件序号用于跨请求关联。

## Frontend canonical 链路

`frontend/` 是唯一 Vite 工程；`src/main.ts` 加载 `App.vue`、`styles/tokens.css`、`styles/globals.css` 与 `style.css`。`App.vue` + `router.ts` 选择 `views/HomeView.vue`、`ProductionWorkspace.vue`、`TasksCenter.vue`、`AssetsLibrary.vue`；组件在 `components/`、领域呈现在 `domains/`。`services/core.ts` 是现行 Core API/SSE 客户端；真实 Run/Approval/Artifact/Batch 数据由后端提供。Vite `5173` 与 Python `8000` 为独立进程；build 输出到 `src/kantoku/shells/web/` 供 Python 生产静态服务，不能把 build 产物当源码编辑。

## 审计分类及保留策略

| 路径 | 分类 | 证据/处理 |
|---|---|---|
| `src/kantoku/__main__.py`、`shells/web_studio.py`、`core/runtime/` | canonical | 包入口与 HTTP 实际调用；保留并就地修复 |
| `frontend/src/main.ts`、`App.vue`、`router.ts`、`services/core.ts` | canonical | 从页面入口可达；保留并就地修复 |
| `frontend/src/api.ts`、`views/DomainWorkspace.vue`、`components/workspace/` | 已清理 Legacy | 审计确认 App/Router、测试、动态加载、Skill manifest 均无引用；旧文件从工作树删除，历史由 Git 保留 |
| `shells/web_studio.py:main()`、`start-studio.cmd` | 兼容入口 | 已将 cmd 指向包入口；旧 Python main 暂留兼容，不作为正式入口 |
| `src/kantoku/shells/web/`、`frontend/design-spec.html`、根目录 `dashboard.png` | 生成/设计产物 | 静态 bundle 为 Vite build 输出；设计/截图不是运行入口，不参与架构迁移 |
| `data/`、`.venv/`、`.uv-cache/`、`.workbuddy/` | 本机数据/工具目录 | 与源码分开，不当成重复应用删除 |

审计没有发现 `*_v2/_new/_fixed/_backup` 命名的并行源码；没有第二个 Vite package。上述无引用旧组件已删除；动态 Skill、静态 bundle 与业务记录仍按其实际用途保留。
