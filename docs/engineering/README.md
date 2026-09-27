# 工程边界与维护手册

根目录 `AGENTS.md` 是 AI 改动入口。本文件只记录实施细节；产品里程碑与配置约束仍见 `docs/01`–`04`。

## 架构边界与改动顺序

改动前先读产品原则、本文件和实际代码，再确认问题属于哪一层。目标调用方向是 **Conversation → Unified Agent Orchestrator → Action / Plan → Direct Capability 或 Domain Workflow → Core Infrastructure → Provider**。这是收口方向，不代表当前已有独立的统一编排模块；禁止为了满足图示另起第二套运行时。

- Conversation 只管理会话、消息、持久化和实时事件；统一编排只做意图、上下文、交互策略和执行路径决策。不要把首页、Comic、Commerce 的判断继续堆进 `stream_conversation`，也不要为每个入口各建 Router / Planner。
- 会话原有 `interaction_mode=autonomous/guided` 是持久化兼容字段；对产品与客户端呈现为 `execution_mode=fast/professional`。同一个 `conversation_router` 决定 Action、Domain 与执行模式；首页复杂请求可推断 Domain，但不会因此跳入专业页面。不要另存一份互相竞争的模式状态。
- ImageService、VideoService 和 Prompt Enhancer 应是各自唯一的共享能力入口；Autonomous / Guided 差异由 mode、domain、policy 和 action 表达。简单请求直达共享能力，复杂请求才启动领域 Workflow，两者共用预算、Artifact、日志与幂等机制。
- Core 不理解领域词或业务流程；Domain 只定义专业 Schema、Policy、Workflow 和 Prompt，不复制 Core。Provider 只实现供应商协议，不决定用户交互、审批或业务流程。前端只调用应用 API，不直接访问 Provider。
- 禁止伪造 Provider 状态、进度与结果。修 Bug 补回归测试；先收口已有实现，再删除被替代代码，历史由 Git 保存。不得创建 `_new`、`_old`、`_backup`、`_final`、`_v2` 副本。

## 本轮架构审计（2026-09-25）

- `shells/web_studio.py` 的 `stream_conversation` 同时处理意图、首页生图/恢复、预算策略、Guided 任务构造及聊天流，职责明显过重。现有 `core/conversations.py::IntentPlanner`、`capabilities/creative.py::plan_creative_turn` 与 `shells/conversation_router.py::route_conversation` 是相连但分散的决策步骤；未来应在现有调用链中收口为一个编排入口，而非再加一套 Planner。
- `core/conversations.py::IntentPlanner` 包含 Ozon、SKU、Comic、Commerce 领域词和分支，与 Core 不理解领域的原则冲突。迁移时保留现有路由行为与回归测试，将领域分类知识移到编排/领域策略；本轮不搬迁。
- 首页图片由 `capabilities/image.py::ConversationImageService` 复用 `tools/image_gen.py::gen_image` 的预算/供应商提交，Artifact 落在共享 RuntimeStore；Comic 使用同一 `ImageProvider`/`gen_image` 底层，Commerce 通过 `ProductImageCapability` 协议并以现有适配器实现。`VideoService` 被 Comic Workflow 引用。没有证据表明这些应整套重写，但图片领域入口尚未完全收成单一 Service API。
- `shells/web_studio.py::_enhance_prompt` 与 `capabilities/creative.py::compile_image_prompt` 分别服务不同路径，是提示词处理分散的收口候选；不能仅凭同名或 IDE unused 删除。`src/kantoku/core/` 未发现直接导入 `domains/` 或 `adapters/`，但上述领域词属于语义泄漏。
- 对源码与测试文件名的 `_new/_old/_backup/_final/_v2` 审计未发现可据此删除的副本。`src/kantoku/shells/web/` 是前端构建输出，不是第二份源码；不得当废弃代码删除。清理任何动态加载、Skill、路由或 Schema 前仍须逐项核对引用。

## 漫剧制作台审计与演进边界（2026-09-27）

- 当前 `domains/comic/workflow.py` 的真实闭环是**单镜头任务准备 → 费用确认 → 生图 → VLM QC → 人工审核/返工 → 归档 → 可选视频**。`prepare` 只创建可恢复的 StudioTask，不生成导演分析或结构化分镜；前端不得把它标作已完成的「导演规划」。
- `tools/storyboard.py` 已能生成并持久化 20 镜分镜，`tools/prompt_factory.py` 已提供镜头与 Persona 的可追溯配方，`memory/` 保存角色记忆。这些能力目前未由上述 Comic Workflow 统一编排；不得在 UI 中伪装已有 Character/Scene/Style Bible 或已锁定资产。
- 制作台复用 Run、NodeExecution、Artifact、QC State 和原有专业对话。默认让 AI 导演对话占满主区；有真实 Run 后，才出现可拖动的对话/结果预览分区及顶部轻量阶段。左侧仅留资产图标入口，真实产物按需展开；右侧 Inspector 默认隐藏，只呈现当前阶段的真实信息，Prompt、模型、Seed、QC 与成本渐进展开。没有 Run 时不出现空镜头、假资产或假进度。Commerce/Studio 沿用原页面。
- 后续导演层应在 `domains/comic/` 中定义可持久化的 Creative Brief / Director Spec / Asset Bible / Storyboard，并区分用户硬约束、软偏好与可发挥空间；用 Stable Context + Relevant Memory + Current Task 编译不同模型的 Prompt。复用现有 Capability、Budget、Artifact、Provider、审批和 Runtime，不增加第二套生图/视频或独立聊天系统。QC 应区分通过、小修和重做，实际支持局部修复前不得在界面声称已有该能力。

交互参考：[Runway Agent 的对话式创作](https://help.runwayml.com/hc/en-us/articles/51601639579667-Creating-with-Runway-Agent)、[Figma 的可收起侧栏](https://help.figma.com/hc/en-us/articles/360039831974-Explore-the-navigation-bar-and-left-sidebar)、[Figma 的上下文属性面板](https://help.figma.com/hc/en-us/articles/360039832014-Design-prototype-and-explore-layer-properties-in-the-right-sidebar)、[InvokeAI 的 Gallery/Canvas](https://invoke.ai/features/gallery/)。拖动分区继续使用项目已有 `splitpanes`，简单高级信息使用原生折叠，不为同一用途重复引入组件库。

## 漫剧图片生产：Phase 1 架构契约与迁移计划（2026-09-27）

本阶段只完成审计、数据契约和迁移设计，**不把设计视为已上线能力**。交付目标限于图片生产；现有可选 Video Node 与 Mock Provider 保留兼容，但新作品流程不自动接入图生视频、时间轴或视频编辑。未来视频仍须复用共享 Video Capability，不把视频状态预塞进图片专用表。

### 现状与处置

| 现有模块 | 证据与边界 | 处置 |
| --- | --- | --- |
| `domains/comic/workflow.py`、`models.py` | `comic.production.v1` 是单镜头准备、费用审批、生图、QC、人工审核、返工、归档的固定图；`ComicState` 不是作品模型。 | **保留兼容**现有 Run/Checkpoint；未来在 Comic Domain 按任务类型组合领域 Workflow，不把旧图扩充成所有作品必经的巨型图。 |
| `domains/comic/services.py` 与 `tools/studio.py` | 已复用 `gen_image` 的预算、幂等和供应商查询，但服务直接持有 `ImageProvider`，StudioTask 另存 JSON 文件，按项目名与镜号扫描防重。 | **渐进重构**为共享 Image Capability 的领域调用端口；旧 request ID、账单与恢复路径保持有效，不能为了收口重新提交旧任务。 |
| `tools/storyboard.py`、`schemas/storyboard.py` | 有严格镜号校验和实际持久化；生成固定 20 镜，`save_storyboard` 按 episode 替换旧镜头。 | **复用校验，迁移存储**到作品级、可变镜数和追加版本；旧 CLI/Agent 调用保持兼容直到验证迁移。 |
| `memory/persona.py`、`tools/prompt_factory.py`、`agent/context.py` | 已有角色结构、Prompt 配方及最近镜头窗口；Persona 以名字全局覆盖，Recipe 以 episode/镜号定位，现有质量词是固定文本。 | **复用约束、溯源和上下文裁剪**；改为项目范围的资产版本与按模型编译，不再用全局角色名或固定质量词代替导演分析。 |
| `perception/qc.py`、`perception/review.py`、`tools/archive.py` | 已有视觉预筛、不可变人工审核、返工队列与归档；当前返工是新生成任务，不是已实现的局部编辑。 | **保留并扩展领域判定**；只有接入可验证的局部修改能力后，`MINOR_ERROR` 才能自动修补。 |
| `core/runtime/`、`core/budget.py`、Artifact、Approval、Image/Video Capability | 通用 Run、Node、Checkpoint、事件、预算台账和 Artifact 已共享；预算当前仍要求 project/episode/shot_no，属于待收口的通用身份约束。 | **复用而不复制**；预算身份泛化需另做兼容迁移，不在 Comic Domain 另建账本。 |
| `shells/web_studio.py` 的旧 Studio 操作与 Core Run API、Comic Skill manifest | 均有路由、动态加载或测试引用。 | **暂不删除**；替换后先验证旧入口、Skill、恢复和历史数据，再移除确实重复的领域适配代码。 |

目前**没有证据支持立即删除任何业务文件或历史表**。`db/schema.sql` 的 `shot`、`persona`、`recipe` 和 StudioTask 文件均视为历史业务记录，绝不靠清空或覆盖完成迁移。

### 作品级数据契约

稳定的 `project_id` 是作品身份；项目名称只用于展示，不能作为去重、预算或关联键。以下对象属于 `domains/comic/`，不是 Core 通用模型。所有可编辑对象使用稳定 `entity_id` 与从 1 开始递增的修订号；编辑追加新修订，不覆盖旧内容。删除分镜或资产写入墓碑修订，历史版本仍可恢复。

| 对象 | 领域数据与关系 | 版本边界 |
| --- | --- | --- |
| `CreativeProject` | `project_id`、标题、作品状态、创建/更新时间；关联 Conversation、Run 的 ID，不复制其记录。 | 项目元数据可更新；创作内容独立修订。 |
| `CreativeBrief` | `original_request`、`hard_constraints`、`soft_preferences`、`creative_freedom`，保留用户原始表述；模型推断与用户明示应标明来源。 | 每次创作理解或人工修改产生新修订；硬约束不能被导演或 Prompt 编译静默改写。 |
| `DirectorSpec` | 关联 Brief 修订，记录 `visual_direction`、`mood`、`color_language`、`lighting`、`camera_language`、`composition`，并说明与故事、角色、场景及镜头目的的关系。 | 动态分析，不建立“情绪词 → 固定摄影公式”；引用的 Brief 修订必须明确。 |
| `CharacterAsset`、`SceneAsset`、`StyleBible` | 项目范围内的角色外观/服装/特征、环境/时间/天气/光线、风格/色彩/材质/摄影语言；参考图片只存 Core `artifact_id`。 | 各资产独立修订，可锁定某一修订供多个镜头复用；不可通过重名覆盖其他项目。 |
| `Storyboard`、`Shot` | Storyboard 保存有序 Shot ID 列表，镜头数由需求决定；Shot 保存 `shot_id`、目的、主体、动作、环境、构图、摄影、引用的资产 ID 与修订、状态。顺序号仅用于展示，不作为镜头身份。 | 分镜和单镜头分别追加修订；重排、删除或返工不改变稳定 Shot ID。 |
| `PromptArtifact` | 使用 Core `ArtifactType.PROMPT`，在 metadata 记录所用 Brief/Director/Asset/Shot 修订、编译器版本、模型标识、Prompt 哈希及来源 Run/Node；`location` 指向持久化的 Prompt 正文。 | 新 Prompt 创建新 Artifact/版本，不覆写已提交生图任务的输入。 |
| `GenerationTask`、`QualityReport`、最终 `Artifact` | 生成沿用 Core Run/Node、StudioTask 或其兼容替代、`generation_request_id`、预算台账和 Image Capability；QC 沿用现有预测/人工审核并关联对应镜头及图片 Artifact。 | Retry 恢复原请求；Regenerate 创建新请求。图片、QC 报告及未来视频均由 Core Artifact 保存，不建立 Comic 专属媒体库。 |

领域 Shot 状态为 `draft → planned → generating → checking → approved`；`generating/checking` 可转 `failed`，`checking/failed` 可在明确修复方案后转 `repairing`，再进入新一次 `generating/checking`。这些是 **Shot 的领域状态**，不替代 Core Run/Node 状态；状态改变应与相应修订或生成请求建立可追溯关联。`PASS / MINOR_ERROR / MAJOR_ERROR` 是 QC 判定，不是供应商状态；不允许把“已提交”当成“已出图”。

### 存储与迁移顺序

1. Phase 2 在现有 SQLite 数据库中添加 **Comic Domain 自有**的项目与追加修订存储（项目表、带 `project_id/entity_type/entity_id/revision` 唯一约束的领域修订表）。Pydantic 领域 Schema 校验每一类 payload；一次写入使用事务和预期修订号，避免并发覆盖。迁移不得修改 Core `runs`、`artifacts` 或预算台账语义。
2. 新写路径先只服务新作品；旧 `comic.production.v1`、StudioTask、`shot/persona/recipe` 继续原样读取。为旧记录建立**显式、可重跑**的导入映射：先列出 episode、项目名、角色名和请求 ID，发现同名冲突时要求人工选择归属，不猜测合并；导入只追加新记录并保存 legacy ID 对照，不删除旧行或文件。
3. 通过对照测试核实镜头顺序、角色内容、Prompt 哈希、Run/Artifact/预算关联、未知供应商任务恢复与审批记录。确认新旧读取一致并完成备份/回滚演练后，才切换新项目读取入口；旧 API、CLI 与 Skill 在调用方迁移前保持兼容。
4. 仅当静态引用、路由、动态加载、Skill manifest、前端调用和回归测试均证明旧适配实现不再使用，才删除被替代**代码**。历史账单、作品、Artifact、StudioTask 文件及其迁移对照长期保留；表的删除不是本图片生产阶段目标。

### 后续阶段的交付门槛

- Phase 2：Project + CreativeBrief 的保存、编辑、恢复、并发修订与旧数据隔离测试；不调用生图。
- Phase 3：DirectorSpec 使用 Brief 修订、故事/角色/场景上下文；测试硬约束保持、修订溯源与无固定摄影公式。
- Phase 4：资产项目隔离、引用 Artifact、锁定版本与跨镜头复用测试。
- Phase 5：任意合法镜头数、稳定 Shot ID、重排/墓碑/恢复和状态转换测试；旧 20 镜接口继续兼容。
- Phase 6：复用现有 Image Capability、Budget、Run、Artifact 和 Prompt 配方，验证首提、重启恢复、未知状态不重提、Retry/Regenerate 区分及多镜头版本引用。
- Phase 7：复用现有视觉 QC 与审批，验证 PASS/MINOR/MAJOR 的真实证据、返工版本和费用；没有可用局部编辑 Provider 时明确标记 `MINOR_ERROR` 需要人工处理或获批重生，不伪称局部修复成功。

每阶段先运行相应回归测试，再运行全项目结构检查、ruff、pytest 与前端构建/类型检查；有真实供应商费用的验收另外确认预算。**不得在 Phase 1 一次实现 Phase 2–7，也不得在本图片阶段自动启用视频。**

### Phase 2 已实现的作品上下文边界

`domains/comic/projects.py` 在现有 Core SQLite 文件中追加 `comic_schema_migrations`、`comic_projects` 和 `comic_entity_versions`，后者保存不可变的 Project/CreativeBrief 修订。创建作品同时创建 Brief v1；更新 Brief 必须提交当前作品 `expected_version`，并原子追加 Brief 与 Project 修订。重复提交完全相同的内容不增加版本；并发不同修改不能静默覆盖。`GET` 可按作品版本读取旧 Brief，Context 响应带明确的来源版本。

HTTP 入口为 `POST /api/comic/projects`、`GET /api/comic/projects/{project_id}`、`PUT /api/comic/projects/{project_id}/brief`、`GET /api/comic/projects/{project_id}/context`，同时兼容请求中的 `/comic/projects` 写法；均沿用现有本机会话令牌、Trace 和错误处理。创建请求可直接提供结构化 Brief；若只提供标题，则仅把标题保存为原始需求，其他字段保持空列表，**不会凭空推断用户约束或偏好**。当前 Context 只含 Project、Brief、可选当前任务与来源修订；`relevant_memory` 为空，因为角色/场景/镜头资产尚未在 Phase 4/5 接入。Context 是可按版本重建的派生视图，不另存一套会漂移的内容表。

本次迁移只建新表，不导入、改写或删除旧 `shot/persona/recipe`、StudioTask、账单、Run、Artifact；旧入口保持原样。历史数据自动归属作品会出现同名冲突，因此必须等待显式映射与对照测试，不能按作品名猜测迁移。

### Phase 3 已实现的导演决策边界

`domains/comic/director.py` 使用 Phase 2 的 `ComicContextBuilder`，只把当前 Project、CreativeBrief、可验证的相关记忆与当前任务送往现有共享 `core.llm.chat`。目前相关资产尚未接入，所以 `relevant_memory` 为空；不会把全量聊天、旧作品或旧 Prompt 偷渡给模型。模型返回的是结构化、可编辑的 DirectorSpec（叙事目标、视觉方向、镜头语言、构图、光影、色彩、情绪、角色重点、约束与创作选择原因），**不是生图 Prompt**。导演指令禁止固定情绪词到摄影公式的映射；用户硬约束由服务补齐并在存储层再次检查，人工编辑也不能遗漏。

DirectorSpec 与 `project_id`、`creative_brief_version` 绑定，在原有 `comic_entity_versions` 中追加不可变修订；迁移 v2 仅给 `comic_projects` 增加当前导演方案的 ID/版本指针，不改变旧业务表。Brief 实质更新会清除当前指针，但保留全部旧导演修订；旧 Brief 下的方案可查看，不可直接恢复为新版 Brief 的当前方案。恢复会复制旧内容成为**新修订**，不覆盖历史；提交需要当前作品版本以防并发覆盖。`source` 标明模型生成、人工编辑或历史恢复。

HTTP 入口：`POST /api/comic/projects/{id}/director-spec`（仅 `expected_project_version`/可选 `task` 为模型生成；附 `draft` 为人工编辑）、`GET /api/comic/projects/{id}/director-spec`、`GET /api/comic/projects/{id}/director-spec/versions`、`POST /api/comic/projects/{id}/director-spec/restore`（`version` 与 `expected_project_version`）。沿用本机会话、Trace 与异常机制；API 不创建 Run、Storyboard、Prompt Artifact，不调用图片、视频或 QC。共享文本模型可能产生费用，离线测试使用替身，不能声称已完成真实模型付费验收。下一阶段应在现有版本体系中加入项目范围的角色/场景/风格资产及真实相关记忆选择，仍不应把资产与 DirectorSpec 直接拼成供应商 Prompt。

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
