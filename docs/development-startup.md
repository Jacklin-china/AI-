# Kantoku 开发启动

## 一键启动后端

在项目根目录运行：

```powershell
uv run python -m kantoku serve
```

也可以直接运行 `uv run python -m kantoku`，默认行为相同。后端默认监听
`http://127.0.0.1:8000`，同时提供 Core API、Runtime SSE 与已构建前端。
PyCharm 可使用仓库中的 `Kantoku Backend` Run Configuration（项目 `.venv` 解释器）。

## 独立启动 Vue 开发服务器

```powershell
cd frontend
npm run dev
```

也可在 VS Code 选择 `Kantoku Frontend` Task/Run，一键运行同一命令。开发模式默认连接本机后端 `http://127.0.0.1:8000`，跨机器调试时才需要设置 `VITE_API_BASE_URL`。Vite 只负责前端热更新，不会代替或自动启动 Python 后端。浏览器打开
`http://127.0.0.1:5173`。后端仅允许本机 Vite 来源并继续校验会话令牌。

## 环境诊断

```powershell
uv run python -m kantoku doctor
```

默认诊断只检查配置、数据库、静态构建和密钥是否存在，不联网、不产生费用。
`doctor --live` 也不会自动发起付费生成；需要真实供应商验收时，仍须在工作台显式确认预算。

## 日志

控制台显示简洁启动与错误状态，应用 JSON 日志写入 `data/logs/kantoku.log`，按 10 MB 轮转并保留 14 天。异常详情经过脱敏，保留错误类型、截断后的说明与 traceback 文件/行；用户界面只显示安全说明、Trace ID 和 Error ID。开发日志可用 `python -m kantoku logs archive` 归档，不清理 Run/Artifact/Ledger/Audit。
