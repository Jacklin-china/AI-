# kantoku-agent · 监督酱（AI 漫剧副导演）

> 一个**本地优先的 AI 漫剧制作工作台**：把「创作对话 → 画面方案 → 生图指令 → 图片优化 →
> 改图与版本比较 → 故事编排 → 导出材料 → 视频生成」串成一条可回溯的生产链路。
>
> 它不承诺一键爆款。目标是**提高首次可用率、减少定向返工**，并让每一单的成本与利润可核算：
> 所有产物落本地 SQLite、每次调用留 trace、账单不明时标 `unknown` 而不是当 0。

前端是 Vue 3 工作台（`frontend/`），后端是统一 Core Runtime（`src/kantoku/`），
通过 `python -m kantoku serve` 同时提供 API、SSE 与生产工作台。

## 现在能做什么

| 环节 | 能力 |
|---|---|
| 创作对话 | 首页对话入口，一条创作消息生成一份独立方案；`fast` / `professional` 两种执行模式，不因此切换页面 |
| 01 画面方案 | 导演上下文（DirectorSpec）、分镜方案、专业评审（Critic）与装配 |
| 02 生图指令 | Prompt 编译、语义约束核验、四段式提示词与产物交付 |
| 03 图片优化 | 导入或生成图片、框选区域改图、审阅结论落库 |
| 04 改图指令 | 基于图片内容的改图指令生成，按内容指纹归属报告 |
| 05 版本比较 | 多版本并排比较、选定「修改基础」，版本可回退 |
| 06 故事编排 | 故事初稿、结构校验（缺镜头/动作/运镜/时长由确定性规则给出）、内容一致性校验 |
| 07 导出材料 | 交付清单与素材导出 |
| 08 视频生成 | 视频阶段的素材组织与提交入口（当前 Provider 为 Mock，见「当前边界」） |
| 资产库 | 角色、场景、素材的统一管理与复用 |
| 任务中心 | 任务、批次与运行记录 |

## 架构

分层与调用方向是硬约束（详见 [AGENTS.md](AGENTS.md) 与 [工程手册](docs/engineering/README.md)）：

```
Conversation → Unified Agent Orchestrator → Action / Plan
             → 直接调用 Capability 或走 Domain Workflow
             → Core Infrastructure → Provider
```

```
src/kantoku/
├── core/          Run / Workflow / Node / Artifact / Memory / Runtime Event / 文件台账
├── domains/       comic（漫剧）、commerce（商品图）
├── capabilities/  image / video / creative / web_search —— 跨域共享的唯一入口
├── adapters/      外部平台协议适配
├── shells/        web_studio（API + SSE + 工作台）、cli、image_cli、qc_cli
├── config/        设置、日志、运行路径与临时文件台账
├── memory/        人设与素材检索
└── perception/    视觉质检

frontend/          Vue 3 + TypeScript + Vite 工作台
tests/             后端 pytest（离线可跑）
docs/              工程、产品、架构文档
```

四条规则：**Core 不知道业务**（不出现漫剧/商品等词）、**Domain 不复制 Core**、
**Provider 只实现协议不决定交互**、**前端只调应用 API**。

## 快速开始

需要 Python 3.11–3.12、[uv](https://docs.astral.sh/uv/)、Node 18+。

```bash
# 后端：API + SSE + 生产工作台
uv sync
uv run python -m kantoku serve            # http://127.0.0.1:8000
uv run python -m kantoku serve --no-browser --port 8000
uv run python -m kantoku doctor           # 本机配置自检（不联网）
uv run python -m kantoku doctor --live    # 附加供应商连通性探测

# 前端开发服务器
cd frontend
npm install
npm run dev                               # http://127.0.0.1:5173
```

配置与密钥（不要提交）：

```bash
cp config/settings.example.yaml config/settings.yaml
cp config/.env.example .env               # 仅在本地填写密钥
```

`config/settings.yaml` 与 `.env` 均已在 `.gitignore` 中。运行期数据
（`data/`、`logs/`、`tmp/`、`cache/`、`artifacts/`）也全部忽略，不进版本库。

## 测试

测试完全离线，不需要密钥、不产生费用：

```bash
uv run pytest                              # 70 个测试文件 / 1321 个用例
cd frontend && npm test                    # 15 个文件 / 233 个用例
cd frontend && npx vue-tsc --noEmit        # 类型检查
```

## 文档导航

| 文档 | 内容 | 何时读 |
|---|---|---|
| [AGENTS.md](AGENTS.md) | AI 协作入口与修改前必读清单 | 开始改代码前 |
| [docs/engineering/README.md](docs/engineering/README.md) | 分层边界、目标调用方向、改动顺序 | 定位实现位置前 |
| [docs/engineering/AI_CODE_RULES.md](docs/engineering/AI_CODE_RULES.md) | 代码红线、禁止平行实现 | 写代码前 |
| [docs/product/product-principles.md](docs/product/product-principles.md) | 产品原则与取舍 | 做功能决策前 |
| [docs/engineering/runtime-policy.md](docs/engineering/runtime-policy.md) | 运行文件、临时目录与台账规范 | 改存储/清理逻辑前 |
| [docs/architecture/](docs/architecture/) | 逐模块设计、上下文锁与[架构例外清单](docs/architecture/exceptions.md) | 改动涉及历史边界时 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 启动方式、日志查询、交付门禁 | 开发与验收时 |
| [docs/01](docs/01-开发规范.md)–[04](docs/04-功能开发引导.md) | 早期 10 周计划的开发规范与引导 | **仅作历史参考**，不反映当前实现 |

## 当前边界

诚实标注，避免把「已实现」误读成「已验收」：

- **供应商账单未验收**：成本台账在供应商不返回金额时保留 `unknown` 并等待对账，
  不会把估算冒充实扣；预算上限只覆盖已接入的生图账目，不含文本、配音与视频。
- **Commerce 领域是 Mock 链路**：不代表真实电商平台对接完成。
- **视频能力只有 Mock Provider**：返回的是 `mock://video/...` 逻辑产物，没有真实渲染、
  没有真实计费；接入真实视频服务前不得把结果称为成片。
- **画面质量需人工验收**：结构化校验与 Prompt 锚点校验通过，不等于成片可用。
- **已知失败测试**：仓库内仍有少量历史遗留失败用例（如 Critic 不可用时的导演确认路径），
  修复前需先确定产品契约。不会通过伪造 pass 或放宽门禁来消除。

## License

MIT，见 [LICENSE](LICENSE)。
