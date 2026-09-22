# 工程边界与维护手册

根目录 `AGENTS.md` 是 AI 改动入口。本文件只记录实施细节；产品里程碑与配置约束仍见 `docs/01`–`04`。

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
