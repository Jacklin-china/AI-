# AGENTS.md — AI 协作开发约束（每次会话必须先读本文件）

项目：kantoku-agent（监督酱 · AI 漫剧副导演）。
本文件是 AI 编码助手（ZCode/Claude 等）在本仓库工作的**入口约束**，与 docs/01-开发规范.md 具有同等效力。

## 会话工作流（每次开发会话按此执行）

1. 先读 `docs/04-里程碑计划.md`，确认当前里程碑；**只做当前里程碑范围内的任务**。
2. 动手前读相关文档（01 规范 / 02 选型 / 03 范围）；需求只从 `docs/05` 的痛点表取，不自行发明功能。
3. 新增/修改代码必须：类型注解完整、配置走 `kantoku.config.get_settings()`、异常走 `kantoku.errors` 体系。
4. 完成标准（自 M1 有代码起适用）：`uv run ruff check .` 与 `uv run pytest` 全绿，否则不得收尾。
5. 收尾动作：更新 `docs/06-开发日志.md` → 按 Conventional Commits 给出提交信息建议。

## 硬性红线（违反即返工）

- 密钥只存 `.env`；代码、日志、文档、提交信息中出现真实密钥 = 立即作废密钥并返工。
- 模型名、端点、价格、预算、超时只从 `config/settings.yaml` 读取，**禁止硬编码**。
- 任何付费调用（生图/图生视频）必须先过预算控制器（reserve → 调用 → settle），超限抛 `BudgetError` 并由 Agent 语音告知，禁止默默失败。
- 外部调用必须有超时与重试（默认 timeout_s=30、retry=2）；禁止裸 `except:`。
- 公共函数必须有类型注解；新增第三方依赖必须先在 `docs/02` 登记用途与理由。
- **禁止修改 Open-LLM-VTuber 底座核心代码**，只通过其对外接口集成；底座独立运行。

## 范围冻结

`docs/03-产品需求与范围.md` 中"明确不做"清单（Computer Use、自动剪辑、多 Agent 框架、
聊天机器人端、爬虫等）在任何会话中都不得实现；相关想法写进该文档的 Future Work 即可。

## 环境注意（Windows）

- 全程 UTF-8；文件读写显式 `encoding="utf-8"`；路径一律 `pathlib.Path`。
- 行尾 LF（见 `.gitattributes`）；命令行环境为 cmd，注意命令兼容性。
