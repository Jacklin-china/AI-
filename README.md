# kantoku-agent · 监督酱（AI 漫剧副导演）

> 一个**副导演 Agent**：在 AI 漫剧兼职流程中帮我拆分镜、锁定角色/场景约束、预筛废片、
> 管定向返工与成本台账。它不承诺一键爆款，目标是提高首次可用率、减少返工并让每单利润可计算。
>
> 双线咬合：**Agent 工程能力 × AI 漫剧真实交付**——真实作业提供需求与失败数据，Agent 反过来压工时和单可用镜头成本。先作为内部生产工具验证单位经济，再决定是否产品化。

## 文档导航（四份产品主文档 + 工程入口）

| 文档 | 内容 | 何时读 |
|---|---|---|
| [docs/01-开发规范.md](docs/01-开发规范.md) | 代码 / Git / 测试 / 成本红线，**所有提交的硬性标准** | 写代码前 |
| [docs/02-技术选型与配置约束.md](docs/02-技术选型与配置约束.md) | 选什么、为什么、默认参数、依赖登记表 | 加依赖 / 改配置前 |
| [docs/03-项目开发流程.md](docs/03-项目开发流程.md) | **做什么不做什么 · 系统全景 · 10 周里程碑 · 防烂尾 · 怎么保证不是 demo** | 排进度 / 复盘时 |
| [docs/04-功能开发引导.md](docs/04-功能开发引导.md) | **⭐ 今天做什么：看哪段视频 → 写哪个文件 → 怎么写 → 有什么用** | **每天开工看这份** |
| [AGENTS.md](AGENTS.md) | AI 协作会话入口约束 | AI 会话开始 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 当前代码的 canonical 调用链、边界与 Legacy 审计 | 找实现位置前 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 双 IDE 启动、日志查询与交付门禁 | 开发/验收时 |

> 用法一句话：**写代码前看 01，改配置前看 02，问"还要多久"看 03，问"今天干啥"看 04。**

## 当前工程入口

Backend 是 `src/kantoku` 标准包，`python -m kantoku serve` 经 `shells/web_studio.py` 进入单一 Core Runtime；Frontend 是独立的 `frontend/` Vue 3 + TypeScript + Vite 项目。现行依赖链、领域边界、Legacy 候选与日志链路见 [ARCHITECTURE.md](ARCHITECTURE.md)。下面的周次表保留历史计划，不能据此推断当前代码入口或真实供应商已验收。

## 里程碑状态（10 周）

| 周 | 里程碑 | 内容 | 状态 | Tag |
|---|---|---|---|---|
| 兼职周0 | 手动做 10 镜基线 | 填 `03` §8.2 的 `BASE-01` | 已记录：18 分钟、首次可用率 50%、最终 7/10 可用 | — |
| W1 | M0 地基 | 骨架 + LLM 客户端 + trace + CLI + 手工基线 | 工程验收完成：真实五轮调用、BASE-01、演示录像均已留档；学习验收按授权后补 | 已完成（工程） |
| W2 | M1 分镜生成器 | Tool 协议 + 结构化输出 + 评测集 | 工程验收完成：20 镜落库、Agent 端到端、10 例评测与失败定向回归均已留档 | 待打 v0.2.0 |
| W3 | M2 上下文工程 | 人设卡 + 按镜号裁剪 + prompt 版本化 | 工程完成：20/20 锚点评测通过；五镜画面人工验收待 W4 出图 | 待打 v0.3.0 |
| W4 | M3 生图 + 预算 | 四级预算 + 付费幂等/对账 + 成本台账 | 工程 R9：原 unknown 已按 0 元对账；诊断确认当前 AK 格式错误，真实成功图待补 | 待打 v0.4.0 |
| W5 | M4 可收费闭环 | VLM 预筛 + 人工终审 + 定向重做 | 未开始 | v0.5.0 |
| W6 | M5 影子交付 + 按需记忆 | 先验证单位经济，再决定 FTS5/sqlite-vec | 未开始 | v0.6.0 |
| W7 | M6 按需断点/状态机 + HITL | 最小断点 + 确认节点；有证据再升级状态机 | 未开始 | v0.7.0 |
| W8 | M7 可观测 + 可选 MCP | 经营/技术报告；有调用方再做 MCP | 未开始 | v0.8.0 |
| W9 | M8 真实验收 | 用 Agent 交付 1 单 + 质量/工时/单位经济对比 | 未开始 | v0.9.0 |
| W10 | M9 收尾 | README + 演示合集 + 简历定稿 | 未开始 | v1.0.0 |

## 快速开始

当前开发启动和人工验收以 [CONTRIBUTING.md](CONTRIBUTING.md) 为准。项目已有 Core Runtime、浏览器工作台和 Mock Commerce 链路；Mock 不代表真实平台或即梦账单验收。下方 CLI 命令保留为历史能力入口，不作为后端服务正式启动方式。

在项目根目录执行（Python 3.11–3.12）：

```powershell
uv sync --locked
uv run ruff check .
uv run pytest
uv run python -m kantoku.shells.cli --help
uv run python -m kantoku.shells.cli --ledger 10
```

这些步骤不需要密钥、不调用模型。现有离线测试覆盖 CLI → Agent 工具循环 → 结构化分镜 → 临时 SQLite，以及成功、恢复、止损和脱敏路径。`--ledger` 只读取本地台账，不会产生费用。

离线校验你保存的分镜 JSON：

```powershell
uv run python -m kantoku.shells.cli --validate-storyboard data/storyboard.json
```

文件需由你提供，字段见 `docs/04` §2.6；不能把格式校验通过当成画面质量通过。

离线保存角色卡、列出角色卡，并查看某个已落库镜头的裁剪效果：

```powershell
uv run python -m kantoku.shells.cli --save-persona assets/persona/gunan.example.json
uv run python -m kantoku.shells.cli --personas
uv run python -m kantoku.shells.cli --context "轨道站上的童年回声" 7
uv run python scripts/run_eval.py --kind consistency --output docs/evals/M2-2026-09-12.md
```

`--context` 不调用模型；它只读取本地 SQLite 中已保存的分镜与角色卡。示例实测将近似上下文从 1245 token 裁到 239 token（减少 80.8%）。这是估算值，不冒充供应商精确计费 token；20/20 自动评测只证明角色锚点进入 Prompt，画面一致性仍需生图后人工验收。

准备真实对话时：若本地配置不存在，将 `config/settings.example.yaml` 复制为 `config/settings.yaml`，按模板填写供应商与模型；将 `config/.env.example` 复制为根目录 `.env` 后仅在本地填写密钥。**不要覆盖已有配置，也不要把密钥发到聊天中。** 密钥按名称从进程环境优先读取，未设置时读取项目根目录 `.env`。

```powershell
# 以下两条会调用你配置的模型，可能收费；先确认供应商及消费上限。
uv run python -m kantoku.shells.cli --ask "只回复：你好"
uv run python -m kantoku.shells.cli --ask "请把下面的剧本拆成20镜：……"
uv run python -m kantoku.shells.cli --chat
# 以下只查询本地记录。
uv run python -m kantoku.shells.cli --traces 5
```

交互模式每次只发送当前一句话，不保留对话历史；输入 `exit`/`quit` 退出，`/traces` 查最近记录。当前 token 日限额尚未接入强制拦截，请勿批量调用。trace 中未知成本和未报告 token 均显示“未知”，不能当作免费证明。

R2 起每次应用层请求尝试（含失败重试）各记一条 `llm.chat.attempt`；旧 `llm.chat` 记录粒度不同，不可混算成功率。数据库自动兼容新增列，保留旧数据、不猜测历史用量。当前离线测试 243 项通过；DeepSeek 真实五轮请求 5/5 成功，trace 共记录输入 186、输出 647 tokens，平均延迟约 2.01 秒。供应商未回传金额，成本保持“未知”。

W4 当前按用户提供的即梦客户端价格截图采用保守估算：`1 积分 = ¥0.10`、普通图 `3 积分/次`，所以一次先预占 ¥0.30；单日、单项目（单条视频）和单集上限均为 ¥20，单镜上限为 ¥1，并发先锁为 1。10 镜离线演练的模拟实际费用为 ¥3，**不是平台真实扣款**。即梦客户端会员积分与火山引擎官方 API 资源包可能属于不同计费体系，真实接入后必须以 API 账单回填；供应商不返回金额时台账保留“待对账”，不会把估算冒充实扣。

官方即梦适配器已实现单次提交、任务 ID 查询、有限查询重试和原子保存图片。真实调用前需先在火山引擎开通对应服务，并将 `VOLC_ACCESSKEY`、`VOLC_SECRETKEY` 只写进根目录 `.env`；不要把密钥发到聊天或写入 YAML。R2 提供下列单镜入口，本轮开发只做离线验证。

配置好凭据后先运行本地上线自检。即梦必须填写火山 IAM 控制台生成的一对 Access Key ID 和 Secret Access Key，不能使用方舟 API Key。自检会检查凭据存在性和空白字符、单图模式、官方尺寸限制与本地预算配置，但绝不联网、预占或提交任务：

```powershell
uv run python -m kantoku.shells.image_cli preflight
```

出现“本地生图自检通过”只表示本地格式正确，不代表服务已经开通或价格已经核实。再运行下列查询式诊断；它只查询一个不存在的任务，不预占预算、不创建生图任务：

```powershell
uv run python -m kantoku.shells.image_cli doctor
```

`doctor` 会分别报告“签名鉴权”和“查询接口可用性”，不能把供应商对不存在任务返回的内部错误误判为真实生图已就绪。当前重新配置的 AK/SK 已通过本地格式检查，原 `InvalidAuthorization` 不再出现；查询式诊断返回 `50500 Internal Error`，因此只能确认签名未被拒绝，仍需一次单独授权的小额真实生成确认服务、模型与计费。适配器只记录安全的 `code / message / request_id`，不会记录密钥或请求正文。它还会在提交前拒绝无效参考图 URL；查询结果只有有效 PNG 才会原子落盘，同一供应商任务 ID 返回不同内容时保留原文件并停止。

### W4 单镜操作流程

先在项目根目录运行这个预览示例。它使用仓库自带的提示词，不读取密钥、不建台账、不产生费用：

```powershell
uv run python -m kantoku.shells.image_cli generate --prompt-file assets/demo/w4-shot.example.txt --project video-001 --episode ep01 --shot 1 --request-id ep01-shot01-v1
```

确认真实 API 对应模型、尺寸的单次费用上界后，再在上述命令末尾添加 `--estimate-fen 实际上界分数 --confirm-paid`。例如上界确认为 30 分才填 `--estimate-fen 30`。`--confirm-paid` 表示确认本次新增费用，不能绕过项目/单集/单镜预算。仅填确认开关而未填写 API 费用上界会被拒绝；客户端积分折算只能用于初步估算。

提示词换成自己的 UTF-8 文本文件；同一条视频始终沿用相同 `--project` 和 `--episode`，防止分散记账。`--request-id` 标识一个候选：超时恢复时必须沿用；真正决定付费返工时才使用新的版本 ID。相同 ID 修改提示词、参考图、seed、供应商、模型、尺寸、接口版本或单图模式都会被拒绝，避免配置变化后错误复用旧结果。指纹只包含非敏感配置，不保存密钥。参考图可用重复的 `--reference-url URL` 传入；这些 URL 会随确认后的请求交给供应商。

任务排队、生成中或请求中断后，使用原请求 ID 查询。这条命令只查询任务，不提交新图：

```powershell
uv run python -m kantoku.shells.image_cli query ep01-shot01-v1
uv run python -m kantoku.shells.cli --ledger 10
```

查询结果包含文件路径和供应商任务 ID。台账现在显示本地请求 ID，可直接复制用于恢复。若提交时供应商任务 ID 未返回，需要人工核对平台记录；程序保留预占，不能自动重提。执行中和状态未知的任务占用配置中的并发名额，需处理后才能继续。

图片成功但费用显示“待对账”时，到平台核对该任务最终账单，再回填。下面的 `0.30` 只是输入格式示例，必须替换成实际人民币金额：

```powershell
uv run python -m kantoku.shells.image_cli settle ep01-shot01-v1 --actual-cny 0.30 --confirm-bill
```

费用精确到分，拒绝负数、无穷值和不足一分的小数；同一笔账重复确认不会重复计费，已结算金额不能覆盖。实际费用超过预占时先保存账单事实再报警，本次流程停止，继续前必须重新核价。

仅对未提交的预占，或供应商已确认失败且不收费的任务，才能释放：

```powershell
uv run python -m kantoku.shells.image_cli release ep01-shot01-v1 --confirm-no-charge
```

`unknown` 状态即使带确认开关也不能释放。若人工确认最终账单为零，应使用 `settle --actual-cny 0 --confirm-bill` 留下对账结果。退出码：0 操作完成、1 失败/预算拒绝、2 仍待查询、130 用户中断。

单条成片最高 ¥20 是用户的总成本目标；目前代码强制控制已接入的生图账目，文本模型、外部手工消费和未来视频/配音费用尚未统一进账。请给其他环节留余额，不能把“生图上限 ¥20”理解为已保证整条成片成本不超 ¥20。

### W4 十镜清单与恢复

API 尚未配置时，可以直接运行本地十镜预览（无需密钥，不写生产台账）：

```powershell
uv run python -m kantoku.shells.image_cli batch assets/demo/w4-batch.example.json
```

默认估算每镜 30 分，整批 300 分。真实调用时需把清单复制到 `data/`，填自己的项目、集数、提示词和请求 ID；核实 API 费用后，再添加 `--estimate-fen 实际上界分数 --confirm-paid`。同一批每镜只允许一个候选；模型和预算配置仍来自 YAML。

如果 W3 配方已经保存在 SQLite，不必再复制提示词。下面的命令会按镜号读取同一版本配方、核对模型和尺寸，并自动生成稳定请求 ID；默认仍然只预览、不读取 API 密钥：

```powershell
uv run python -m kantoku.shells.image_cli batch-recipes --project video-001 --episode ep01 --prompt-version prompt-v1
```

配方含参考素材 ID 时，在 `data/` 保存一个 `{"素材ID":"https://可访问地址"}` 的 JSON 对象，并添加 `--reference-map data/reference-urls.json`。缺少映射、提示词过长、配方模型或尺寸与当前 YAML 不一致都会在预算预占和供应商初始化前停止。核价后的付费确认方式与清单批次相同：追加 `--estimate-fen 实际上界分数 --confirm-paid`。

整批流程是“全部校验 → 一次性预占整批预算 → 逐镜串行提交”。例如 10 镜需要预占 300 分，而项目只剩 299 分时，整批拒绝，零提交、零新增预占。已有同 ID 且内容一致的记录不重复占款；有 ID 冲突时回滚本批新增预占，保留历史记录。

程序将成功/失败结果和图片路径持久化到 `image_result`，财务状态仍由 `ledger` 管理。重启后沿用原清单，会读取已完成图片，继续尚未提交的镜头。某镜仍为 `unknown` 时整批停止，先用 `query 原请求ID` 处理，再运行原清单；已失败的镜头须人工决定是否返工，不能自动以新 ID 重做。若停止时后续镜头已预占，它们会继续占款；取消时使用 `release` 逐条释放未提交任务。

成功缓存的图片丢失时会报错，不会自动花钱重新生成。即使你先结算再退出，后续 `query` 也能从本地结果中找到原图片路径；结算成功不代表画面通过人工验收。

查看单条视频预算：

```powershell
uv run python -m kantoku.shells.image_cli status video-001
```

汇总分别显示已结算金额、仍预占金额、剩余项目预算和未知记录数，统计全部记录，不受“最近 100 条”的分页影响。最终提交还要检查日/集/镜额度。单日预算按 `budget.accounting_utc_offset_hours` 配置的自然日计算，当前为 UTC+8 北京时间；跨日未提交的预占须先释放，再用新请求 ID 重新确认。调低配置中的预算后，旧预占也会在提交前重新检查；历史出现实扣高于估价时，低估的后续任务会被拦截，核价并重新预占后才继续。

本轮离线证据见 [M3 评测记录](docs/evals/M3-2026-09-12.md)。示例清单用于验证流程，画面效果、真实扣费和人物连续性仍待 API 配置后实测。

供应商专用请求参数同样从 YAML 读取，主模型与备用模型分别配置。本地 DeepSeek 测试关闭默认思考后，长提示从“输出耗尽 512 tokens 且无正文”恢复为 28 tokens 的正常响应；可变参数没有硬编码进 Python。

日志和相对数据库路径均以项目根目录为基准。日志按天轮转并保留 14 天，轮转/清理由日志事件触发，不是后台定时删除服务。

继续开发或恢复中断：先读 `docs/04` §0 的“当前开发断点”与 `docs/03` §10，核对当前额度后再开一个小批次。工程实现完成与个人学习验收分开记录。

## License

MIT，见 [LICENSE](LICENSE)。
