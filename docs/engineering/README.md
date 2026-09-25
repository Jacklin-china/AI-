# 工程边界与维护手册

根目录 `AGENTS.md` 是 AI 改动入口。本文件只记录实施细节；产品里程碑与配置约束仍见 `docs/01`–`04`。

## 架构边界与改动顺序

改动前先读产品原则、本文件和实际代码，再确认问题属于哪一层。目标调用方向是 **Conversation → Unified Agent Orchestrator → Action / Plan → Direct Capability 或 Domain Workflow → Core Infrastructure → Provider**。这是收口方向，不代表当前已有独立的统一编排模块；禁止为了满足图示另起第二套运行时。

- Conversation 只管理会话、消息、持久化和实时事件；统一编排只做意图、上下文、交互策略和执行路径决策。不要把首页、Comic、Commerce 的判断继续堆进 `stream_conversation`，也不要为每个入口各建 Router / Planner。
- ImageService、VideoService 和 Prompt Enhancer 应是各自唯一的共享能力入口；Autonomous / Guided 差异由 mode、domain、policy 和 action 表达。简单请求直达共享能力，复杂请求才启动领域 Workflow，两者共用预算、Artifact、日志与幂等机制。
- Core 不理解领域词或业务流程；Domain 只定义专业 Schema、Policy、Workflow 和 Prompt，不复制 Core。Provider 只实现供应商协议，不决定用户交互、审批或业务流程。前端只调用应用 API，不直接访问 Provider。
- 禁止伪造 Provider 状态、进度与结果。修 Bug 补回归测试；先收口已有实现，再删除被替代代码，历史由 Git 保存。不得创建 `_new`、`_old`、`_backup`、`_final`、`_v2` 副本。

## 本轮架构审计（2026-09-25）

- `shells/web_studio.py` 的 `stream_conversation` 同时处理意图、首页生图/恢复、预算策略、Guided 任务构造及聊天流，职责明显过重。现有 `core/conversations.py::IntentPlanner`、`capabilities/creative.py::plan_creative_turn` 与 `shells/conversation_router.py::route_conversation` 是相连但分散的决策步骤；未来应在现有调用链中收口为一个编排入口，而非再加一套 Planner。
- `core/conversations.py::IntentPlanner` 包含 Ozon、SKU、Comic、Commerce 领域词和分支，与 Core 不理解领域的原则冲突。迁移时保留现有路由行为与回归测试，将领域分类知识移到编排/领域策略；本轮不搬迁。
- 首页图片由 `capabilities/image.py::ConversationImageService` 复用 `tools/image_gen.py::gen_image` 的预算/供应商提交，Artifact 落在共享 RuntimeStore；Comic 使用同一 `ImageProvider`/`gen_image` 底层，Commerce 通过 `ProductImageCapability` 协议并以现有适配器实现。`VideoService` 被 Comic Workflow 引用。没有证据表明这些应整套重写，但图片领域入口尚未完全收成单一 Service API。
- `shells/web_studio.py::_enhance_prompt` 与 `capabilities/creative.py::compile_image_prompt` 分别服务不同路径，是提示词处理分散的收口候选；不能仅凭同名或 IDE unused 删除。`src/kantoku/core/` 未发现直接导入 `domains/` 或 `adapters/`，但上述领域词属于语义泄漏。
- 对源码与测试文件名的 `_new/_old/_backup/_final/_v2` 审计未发现可据此删除的副本。`src/kantoku/shells/web/` 是前端构建输出，不是第二份源码；不得当废弃代码删除。清理任何动态加载、Skill、路由或 Schema 前仍须逐项核对引用。

## 唯一入口

- 后端：`src/kantoku/__main__.py` → `shells/web_studio.py` → `core/runtime/`。PyCharm 共享运行配置在 `.run/Kantoku Backend.run.xml`，使用项目 `.venv` 与 `scripts/run_backend.py`；标准命令为 `python -m kantoku serve`。
- 前端：`frontend/src/main.ts` → `App.vue`/`router.ts` → `views/`。`frontend/src/services/core.ts` 是 Core API 客户端。Vite build 输出到 `src/kantoku/shells/web/`，该目录是后端静态资源，不是另一份前端源码。
- `core/` 不导入领域或适配器；`domains/` 负责状态和业务分支；`capabilities/` 负责通用 AI 能力；`adapters/` 负责供应商与平台协议。文件 Skill 由 `core/skills/loader.py` 扫描 `skills/`。

## 生图请求与恢复

`generation_request_id` 是台账的 `reservation_id`，本地 `idempotency_key` 与它一致。`ledger` 保存 `run_id`、`provider`、`provider_job_id`、预算状态与最终 `artifact_id`；`image_result` 保存最近的查询结果。旧数据库由现有预算连接入口增量补充元数据列，保留历史台账。

一次请求只调用一次 `submit`。`submitted`/`unknown` 且有 `provider_job_id` 时继续 `query`；还在生成则 Run 保持 `waiting`，重启后对同一 Run 执行 `resume`。没有任务 ID 时标记 `NEEDS_RECONCILIATION` 并保留预占，不自动重提。只有用户明确创建新的生成任务，才使用新的请求 ID。`released` 表示未提交或明确失败并释放预算，不能冒充已提交任务。

查询 `data/logs/kantoku.log` 时用 UI 的 `error_id` 或 `trace_id` 定位完整脱敏 traceback；生图步骤还带 Run/Node、生成请求、Provider 和供应商任务 ID。`data/` 内的日志、SQLite、下载图片以及 Run、Artifact、Ledger 业务记录均不进入 Git，也不因代码清理而删除。

## 清理审计规则

先用 `rg` 和 Git 清单检查静态引用，再查路由、动态导入、Skill manifest、测试及构建输出。没有证据证明安全删除时只标注候选。旧版本交给 Git 历史，不创建备份副本。`src/kantoku/shells/web/` 虽是构建产物，但 Python 独立启动时会从这里提供页面，因此不能把它当缓存直接清空。

## 交付检查

在根目录执行 `uv run python scripts/check_structure.py`、`uv run ruff check .`、`uv run pytest`；在 `frontend/` 执行 `npm run build` 和 `npx vue-tsc --noEmit`。检查 `git diff --check`、待提交文件与忽略规则，确认没有密钥、日志、数据库、运行时图片后按 Conventional Commits 提交并推送，不使用 force push。涉及付费供应商的实测须单独确认预算与凭据，离线测试不冒充真实账单验收。
