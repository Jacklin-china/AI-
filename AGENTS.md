# Kantoku AI 协作规范

任何 Work、AI 或自动化工具在开始功能设计、代码修改或架构调整前，必须依次阅读并遵守 [docs/product/product-principles.md](docs/product/product-principles.md)、[docs/engineering/README.md](docs/engineering/README.md)，再核实当前实现、调用方和测试。先判断问题属于交互、编排、领域、能力、Core 还是 Provider 哪一层，再修改代码。

- **One Core. Multiple Domains.** `core/` 只放通用 Run、Workflow、预算、审批、Artifact、Skill 等机制；不得出现 Comic、Commerce、Ozon、SKU 等领域逻辑。领域业务放 `domains/`，通用 AI 能力放 `capabilities/`，外部模型与平台通过 Adapter 接入。
- **One Unified Orchestrator.** Conversation 统一接收自然语言，经同一编排决策产生 Action / Plan；简单任务调用共享 Capability，复杂任务进入 Domain Workflow。不得为首页、Comic、Commerce 各造一个大脑，也不得在 `stream_conversation` 中不断追加 `if image/comic/commerce`。ImageService、VideoService、Prompt Enhancer 各保持一个共享能力入口；差异由 mode、domain、policy、action 表达。UI 不直接调用 Provider，Provider 不承担业务流程，Domain 不复制 Core。
- Skill 必须能从目录发现并独立测试，不把 Provider 写死在 Skill 或 Core 中。优先复用已有实现，不为修一个问题造第二套系统；禁止 `*_new`、`*_old`、`*_backup`、`*_final`、`*_v2` 式复制开发。
- Secret 只放被 Git 忽略的 `.env`；模型、端点、价格与预算只从 `config/settings.yaml` 读取。任何付费生成都要遵守预占、单次提交、查询、结算；未知账单不得自动重提。
- Python 后端必须能通过 `python -m kantoku serve` 和 PyCharm 的 **Kantoku Backend → Run** 一键启动。Vue 3 + TypeScript + Vite 前端只维护 `frontend/` 一份源码，由前端自己的 `npm run dev` 启动。
- 所有异常必须关联日志、`trace_id`、`error_id` 和完整脱敏 traceback；不能记录 Secret。不得伪造 Provider 状态、进度或成功结果。修 Bug 必须补能复现问题的回归测试；被替代代码应删除，历史交给 Git。
- 修改前先审计引用、路由、动态加载、Skill manifest 与测试，保留用户现有改动和业务记录。功能完成须通过结构检查、ruff、pytest、前端构建与类型检查，检查 Git diff 和敏感文件后创建有意义的 commit，并推送当前 GitHub origin；禁止 force push。无法完成的验收须明确标记 BLOCKED。
