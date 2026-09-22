# Kantoku 开发入口与验收

先读 `AGENTS.md`、`ARCHITECTURE.md` 与 `docs/03-项目开发流程.md`。确认现有调用链后，在原实现上做最小改动；不创建平行 service、manager、`*_new` 文件。

## 两个独立进程

- Backend：PyCharm 打开仓库根目录，选择共享配置 **Kantoku Backend**，右上角 Run。配置运行 `scripts/run_backend.py`，工作目录为 `$PROJECT_DIR$`，使用项目 Python 解释器；脚本调用同一个 `shells.web_studio.serve`。标准命令：`uv run python -m kantoku serve`。不要直接运行 `src/kantoku/__main__.py`。
- Frontend：VS Code 打开仓库根目录，运行 Task 或 Run 配置 **Kantoku Frontend**。它只在 `frontend/` 执行 `npm run dev`，地址通常为 `http://127.0.0.1:5173`。Backend 不启动 Vite，Vite 不启动 Python。首次使用前在 `frontend/` 安装 `npm` 依赖。
- 后端地址 `http://127.0.0.1:8000`，可选供应商缺密钥显示 `BLOCKED`，不影响本地 API 启动。仅项目根目录 `.env` 存密钥；IDE 不需要 dotenv 插件。

## 诊断与安全

应用 JSON 日志在 `data/logs/kantoku.log`，按 10 MB 轮转、14 天保留。一次 HTTP 请求的 `X-Trace-ID` 与错误响应的 `error_id` 可用于查同一链路；前端 `CoreApiError` 保存 HTTP 状态和两个 ID。日志有结构化 `component`、Run/Node/Provider 等可选字段；不记录请求正文、查询参数、Cookie、令牌或原始异常消息。`RuntimeEvent`、Run、Artifact、Ledger/Audit 属于业务记录，不是可清理开发日志。

停止 Backend 后，仅归档当前开发日志：`uv run python -m kantoku logs archive`。Windows 上运行中的 Backend 可能占用日志文件。命令不删除业务数据库或审计文件。日志文件由 Loguru 自动轮转和保留；不要手动对 `data/` 做递归清理。

## 交付门禁

在仓库根目录运行 `uv run python scripts/check_structure.py`、`uv run ruff check .`、`uv run pytest`。前端在 `frontend/` 运行 `npm run build`（先做 `vue-tsc --noEmit` 再打包）。IDE GUI 的绿色 Run/Task 需在用户机器上点一次确认，静态配置检查不冒充 GUI 验收。新依赖先在 `docs/02-技术选型与配置约束.md` 登记；修改前先记录基线，失败需说明是既存还是本次引入。
