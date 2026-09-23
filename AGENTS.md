# Kantoku AI 协作规范

任何 Work、AI 或自动化工具在开始功能设计、代码修改或架构调整前，必须先阅读并遵守 [docs/product/product-principles.md](docs/product/product-principles.md)，再核实当前代码。本文件是协作约束；详细的实现边界与交付步骤见 [docs/engineering/README.md](docs/engineering/README.md)。

- **One Core. Multiple Domains.** `core/` 只放通用 Run、Workflow、预算、审批、Artifact、Skill 等机制；不得出现 Comic、Commerce、Ozon、SKU 等领域逻辑。领域业务放 `domains/`，通用 AI 能力放 `capabilities/`，外部模型与平台通过 Adapter 接入。
- Skill 必须能从目录发现并独立测试，不把 Provider 写死在 Skill 或 Core 中。优先复用已有实现，不为修一个问题造第二套系统；禁止 `*_new`、`*_old`、`*_backup`、`*_final`、`*_v2` 式复制开发。
- Secret 只放被 Git 忽略的 `.env`；模型、端点、价格与预算只从 `config/settings.yaml` 读取。任何付费生成都要遵守预占、单次提交、查询、结算；未知账单不得自动重提。
- Python 后端必须能通过 `python -m kantoku serve` 和 PyCharm 的 **Kantoku Backend → Run** 一键启动。Vue 3 + TypeScript + Vite 前端只维护 `frontend/` 一份源码，由前端自己的 `npm run dev` 启动。
- 所有异常必须关联日志、`trace_id`、`error_id` 和完整脱敏 traceback；不能记录 Secret。修 Bug 必须补能复现问题的回归测试。
- 修改前先审计引用、路由、动态加载、Skill manifest 与测试，保留用户现有改动和业务记录。功能完成须通过结构检查、ruff、pytest、前端构建与类型检查，检查 Git diff 和敏感文件后创建有意义的 commit，并推送当前 GitHub origin；禁止 force push。无法完成的验收须明确标记 BLOCKED。
