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
- Phase 6：先建立作品级 Prompt Compiler 与不可变 Prompt Artifact，不提交生图任务；图片能力、预算和供应商恢复验收顺延至下一图片生产阶段。
- Phase 7：复用现有视觉 QC 与审批，验证 PASS/MINOR/MAJOR 的真实证据、返工版本和费用；没有可用局部编辑 Provider 时明确标记 `MINOR_ERROR` 需要人工处理或获批重生，不伪称局部修复成功。

每阶段先运行相应回归测试，再运行全项目结构检查、ruff、pytest 与前端构建/类型检查；有真实供应商费用的验收另外确认预算。**不得在 Phase 1 一次实现 Phase 2–7，也不得在本图片阶段自动启用视频。**

### Phase 2 已实现的作品上下文边界

`domains/comic/projects.py` 在现有 Core SQLite 文件中追加 `comic_schema_migrations`、`comic_projects` 和 `comic_entity_versions`，后者保存不可变的 Project/CreativeBrief 修订。创建作品同时创建 Brief v1；更新 Brief 必须提交当前作品 `expected_version`，并原子追加 Brief 与 Project 修订。重复提交完全相同的内容不增加版本；并发不同修改不能静默覆盖。`GET` 可按作品版本读取旧 Brief，Context 响应带明确的来源版本。

HTTP 入口为 `POST /api/comic/projects`、`GET /api/comic/projects/{project_id}`、`PUT /api/comic/projects/{project_id}/brief`、`GET /api/comic/projects/{project_id}/context`，同时兼容请求中的 `/comic/projects` 写法；均沿用现有本机会话令牌、Trace 和错误处理。创建请求可直接提供结构化 Brief；若只提供标题，则仅把标题保存为原始需求，其他字段保持空列表，**不会凭空推断用户约束或偏好**。Phase 2 的 Context 最初只含 Project、Brief、可选当前任务与来源修订，`relevant_memory` 为空；Phase 4 才加入有界资产选择。Context 是可按版本重建的派生视图，不另存一套会漂移的内容表。

本次迁移只建新表，不导入、改写或删除旧 `shot/persona/recipe`、StudioTask、账单、Run、Artifact；旧入口保持原样。历史数据自动归属作品会出现同名冲突，因此必须等待显式映射与对照测试，不能按作品名猜测迁移。

### Phase 3 已实现的导演决策边界

`domains/comic/director.py` 使用 Phase 2 的 `ComicContextBuilder`，只把当前 Project、CreativeBrief、可验证的相关记忆与当前任务送往现有共享 `core.llm.chat`。Phase 4 起导演生成可通过任务名称/别名或显式资产 ID 选取已有资产；未匹配时 `relevant_memory` 仍为空，不能伪造资产。它不会把全量聊天、旧作品或旧 Prompt 偷渡给模型。模型返回的是结构化、可编辑的 DirectorSpec（叙事目标、视觉方向、镜头语言、构图、光影、色彩、情绪、角色重点、约束与创作选择原因），**不是生图 Prompt**。导演指令禁止固定情绪词到摄影公式的映射；用户硬约束由服务补齐并在存储层再次检查，人工编辑也不能遗漏。

DirectorSpec 与 `project_id`、`creative_brief_version` 绑定，在原有 `comic_entity_versions` 中追加不可变修订；迁移 v2 仅给 `comic_projects` 增加当前导演方案的 ID/版本指针，不改变旧业务表。Brief 实质更新会清除当前指针，但保留全部旧导演修订；旧 Brief 下的方案可查看，不可直接恢复为新版 Brief 的当前方案。恢复会复制旧内容成为**新修订**，不覆盖历史；提交需要当前作品版本以防并发覆盖。`source` 标明模型生成、人工编辑或历史恢复。

### Phase 7.2 第一阶段：DirectorSpec v2 Schema

DirectorSpec v2 在同一个领域实体上增加 `schema_version`、`creative_decision`、`director_plan`、`cinematography`、`critic_result` 与 `knowledge_refs`。这些字段保存公开、可编辑的导演决策，不保存模型私有思维链，也不包含供应商 Prompt 或生成参数。`schema_version=1` 继续表示现有扁平方案；读取没有版本字段的历史 JSON 时显式标记为 v1，分层字段保持空值，禁止凭空补全。只有完整提供创意理解、导演计划和摄影计划时才允许写入 v2。该阶段不改变表结构，仍使用 `comic_entity_versions` 的追加修订；Coordinator、Critic、Skill Registry 和 API 扩展在后续阶段接入。

### Phase 7.2.2：Director Skill Registry

导演 Skill 使用现有 Core `SkillRegistry`/`SkillLoader`，manifest 位于 `skills/comic/`。当前登记 `comic.creative_understanding`、`comic.visual_direction`、`comic.cinematography`、`comic.director_critic` 和 `comic.director_assemble`，每个 manifest 声明输入/输出 Schema、`execution_policy`、知识引用和契约测试。Skill 没有 `required_tools`，不绑定 Provider，也不修改 Asset；当前 handler 明确标记为 `contract_only`，只校验 Coordinator 提供的结构化结果。Fast 与 Professional 通过同一组 Skill 的 `allowed_execution_modes` 复用，不建立第二套 Skill 或 Runtime。

### Phase 7.2.3：ComicDirectorCoordinator

`domains/comic/coordinator.py` 使用现有 `SkillRegistry`、Core Run/Runtime Event/Trace 和作品版本存储。调用方须注入共享文本能力的阶段执行函数；Coordinator 本身不导入 Provider，也不会把契约 handler 冒充模型。Fast 顺序执行创意理解、视觉导演、摄影指导、组装；Professional 额外执行 Critic，逐阶段将公开结构化结果写入 Run state 与事件。专业模式可指定先前已完成 Run 和起始阶段，在输入 Brief/资产/分镜/镜头版本一致时复用前段结果并从该阶段重跑；重跑创建新的 Core Run，旧记录不覆盖。

两种模式均使用同一 `DirectorSpec v2` 结构；输入资产、Storyboard、Shot 版本写入导演方案。若传入旧方案且依赖版本发生变化，Run state 与 `director_spec_created` 事件标记 `storyboard/shot/prompt_artifact` 为 stale，供后续 UI/API 阻止沿用旧结果；当前不改写这些实体的历史修订。失败写入具体 `skill_id`、`trace_id`、`error_id` 和脱敏 traceback，Run 标为 failed，禁止默认填充导演结果；已完成、失败或等待审核的专业 Run 可在输入版本一致时从指定阶段重新执行。本阶段未新增 HTTP API，也未接入图片或视频。

### Phase 7.2.4：Director Critic 与一次修订

`domains/comic/critic.py` 用确定性规则检查用户硬约束、资产版本与上下文范围；使用现有共享 `core.llm.chat`（配置仍来自 settings）检查具体故事的视觉因果、资产语义一致性、模板化表达和公开导演理由。不存在情绪到镜头的固定映射。模型只能返回严格的公开证据 Schema；字段路径和逐字证据必须可核验，私有 reasoning/CoT、整份重写方案和无效结构均拒绝，原始非法响应不会带进 traceback。旧审核 JSON 的 category/message 和 field/reason 保留读取兼容；新结论增加 code、field_path、evidence、expected、suggested_action、allowed_patches、审核版本与方案指纹，无数据库迁移。

Professional Coordinator 先用 Engine 审核，再经现有 SkillRegistry 校验/记录结论。轻微 warning 只允许修改相关白名单字段：构图、色彩、主体环境关系及部分摄影光线字段；Patch 必须带预期原值，不能修改 CreativeDecision、硬约束、资产或来源版本。自动修订最多一次，然后重新审核一次。最终组装由已有协调层投影分层结果，不再另调用模型重写已审核方案。严重 error/blocked、无可用 Patch 或复审未通过时保留候选到原 Run state，标记 `needs_review`，Run 等待；不保存为当前 DirectorSpec，也不继续 Prompt 编译。重新进入 Critic 时重新审核，不能借用旧 pass 结论。

公开事件使用原 Core Runtime Event 的 `director_event` payload：`director_critic_started/completed`、`director_revision_requested`、`director_patch_applied`、`director_review_blocked`，带原 trace/project/Run/Skill 和审核次数。每次审核结论及 Patch 存在原 Run/Event 中，服务重启仍可查询；错误使用原 `public_error`，不新建日志或任务表。Prompt Compiler 与 Prompt Store 拦截没有实际 pass、内容修改后指纹不匹配或资产版本改变的 v2；v1 旧生产链保持兼容。

本轮没有修改 HTTP/UI 入口；Fast 暂时仍保留前阶段的轻量导演路径，未审核 v2 不能直接进入 PromptCompiler。下一阶段将 Fast/Professional 应用入口接到同一 Coordinator，Fast 可精简公开节点但仍保留约束检查与必要审核；Professional 展示公开结论、局部修订和人工决定。没有图片 Provider、Vision QC 或新 Runtime。本轮测试使用文本模型替身，不冒充真实付费模型验收。

### Phase 7.2.5：Creation Mode 应用接入

创建导演方案的原 POST 现在接受可选 `creation_mode=fast/professional`。该字段是客户端交互策略，进入 Coordinator 后映射到原 `execution_mode`，不增加另一份持久化模式或 Runtime。未传此字段的旧请求/人工 v1 编辑和读取接口仍兼容；显式模式任务全部经过同一个 `ComicDirectorCoordinator`、原 SkillRegistry、共享文本调用与实际 Critic（最多一次自动 Patch）。Fast 隐藏内部节点与审核细节，不绕过约束与质量门禁。严重冲突仍等待处理，不能以默认方案假装成功。

模式请求返回 `{run_id,status,director_spec,director_execution_summary,recovery_required}`；summary 含 `mode/current_stage/status_label/available_actions/stages`。Fast 只公开自然状态、结果摘要和必要错误；Professional 返回五个节点的真实状态、输入版本、结构化公开结果、摘要与审核结论。最终 DirectorSpec 保持同一 v2 契约并绑定审核指纹，可进入现有 Shot Prompt 编译入口；不自动创造 Storyboard/Shot，不提交图片或视频。已有 `/projects/{id}/tasks` 查询同一 Run Store，模式任务按上述用户可见投影返回，不把 Fast 的内部日志当专业节点呈现，也不出现在旧生产任务中心。

Professional 支持 `previous_run_id + rerun_from`，可用 `stage_edits={节点名:完整公开节点对象}` 修改创意/视觉/摄影节点；由原 Skill 契约验证，后续阶段重新执行并重新 Critic，追加新导演版本。审核结论不能由客户端编辑，修改用户硬约束会被拦截；真正修改原始需求应调用已有 Brief 编辑 API。`resume_run_id` 显式恢复失败/等待/旧实例中断的任务，校验作品、模式、任务、Brief、资产、分镜与镜头版本，复用已完成阶段，创建后继 Core Run 并保留来源 Run。仍在本实例执行的任务禁止重复启动；已经完成的任务直接返回原版本结果，不调用模型。服务重启仅显示可恢复，不自动重发未知文本调用；用户显式恢复时未完成阶段可能产生新的文本费用。

新增公开事件 `director_mode_selected/director_stage_visible/director_stage_completed` 使用已有 Runtime Event payload；原 Skill/审核事件仍保留用于后端追踪。创作域的「导演方案」工作区复用当前品牌、样式 Token 和 API 客户端，普通模式仅输入需求与查看摘要；专业模式按需展开节点/版本/编辑。旧制作对话、资产栏与 Inspector 保留可收起，不复制聊天或生产 Workflow。刷新后从现有作品与任务 API 恢复，浏览器只缓存上次作品 ID，不缓存真实任务结果。当前只有导演与 Prompt 基础能力，没有新图片、Vision QC 或视频链路。

HTTP 入口：`POST /api/comic/projects/{id}/director-spec`（仅 `expected_project_version`/可选 `task` 为模型生成；附 `draft` 为人工编辑）、`GET /api/comic/projects/{id}/director-spec`、`GET /api/comic/projects/{id}/director-spec/versions`、`POST /api/comic/projects/{id}/director-spec/restore`（`version` 与 `expected_project_version`）。沿用本机会话、Trace 与异常机制；API 不创建 Storyboard、Prompt Artifact，不调用图片、视频或 QC。共享文本模型可能产生费用，离线测试使用替身，不能声称已完成真实模型付费验收。下一阶段应在现有版本体系中加入项目范围的角色/场景/风格资产及真实相关记忆选择，仍不应把资产与 DirectorSpec 直接拼成供应商 Prompt。

### 漫剧任务追踪补充（Storyboard 前）

DirectorSpec 创建沿用 Core `runs` 与 `run_events`，每次请求有独立 `run_id`、`task_id`、`trace_id`、`project_id`；`state` 保存 `task_type`、`task_status`、`last_completed_step`、Brief/作品版本、选中资产 ID 与版本，失败时保存 `error_id`。领域任务状态词预留 `draft/planning/generating/checking/completed/failed`，目前只有 DirectorSpec 的实际步骤会推进这些状态；不把未来 Storyboard/Image/QC/Repair 冒充已实现。Run 的通用状态仍由 `ExecutionStatus` 管理。`GET /api/comic/projects/{id}/tasks` 从原有 Run Store 按作品读取；进程重启后可查询最后持久化状态，旧实例遗留的运行中任务标记 `recovery_required`，但**不会自动重发未确认的模型调用**。这些轻量追踪 Run 不进入生产任务中心，也不能通过 Graph Workflow resume 误启动。

`data/logs/kantoku.log` 的结构化记录包含公开的任务输入摘要（脱敏且最多 1000 字）、创建时间、Brief 版本、上下文范围、资产选取原因与版本、模型请求 ID/供应商响应 ID、token 用量（未知时明示 unknown）、耗时与重试次数。错误经现有 `public_error` 记录 `trace_id`、`error_id`、异常类型和脱敏 traceback；HTTP 仅返回安全错误。`conversation_id` 仅在调用方传入时关联，独立作品 API 不伪造会话 ID。资产引用缺失时日志包含项目与资产 ID；不写 Secret、私有思维链或完整模型上下文。未来 Storyboard/Shot 生图/QC/Repair 应沿用同一 Run/事件与上下文约定，并按真实步骤推进状态，不另建 ComicRuntime 或 ComicTaskManager。

### Phase 4：小范围资产研究与实现

只参考资产管理的三个案例，不照搬其产品流程：

| 案例 | 可借鉴结构 | 不适合 Kantoku 的部分 |
| --- | --- | --- |
| [Figma 组件变体与版本历史](https://help.figma.com/hc/en-us/articles/360056440594-Create-and-use-variants)、[恢复版本](https://help.figma.com/hc/en-us/articles/360038006754-View-a-file-s-version-history) | 稳定的资产身份、可控属性差异、非破坏性恢复。 | 全文件版本及变体组合不等于角色/场景逐项修订；大量变体会使资产库臃肿。 |
| [InvokeAI Gallery](https://invoke.ai/features/gallery/) | 区分生成结果与外部参考素材，保留可追溯的生成元数据。 | Board 是媒体整理视图，不应复制为第二个 Artifact 库或替代作品关系。 |
| [Mem0 检索](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) | 先限定所属范围，再按相关性与数量选择记忆。 | 当前项目没有经验证的向量索引；不能把名称/别名匹配冒充语义召回，也不引入独立记忆服务。 |

Kantoku 的资产契约：`ComicAsset` 有稳定 `asset_id`、`project_id`、`kind`、名称/别名、`version`、`project_version`、状态和可选 `pinned_version`；`details` 按 `CharacterAsset`（外观、服装、特征）、`SceneAsset`（地点、时间、天气、光线、氛围、环境特点）或 `StyleBible`（艺术方向、色彩、材质、镜头语言、光影）严格校验。共通字段包括固定约束、标签、`reference_artifact_ids`。参考图只引用已有且就绪的 Core 图片 Artifact，不保存第二份媒体记录。资产描述由用户/现有调用方提供；Phase 4 不自行猜测角色外观或自动生成参考图。

迁移 v3 只增加 `comic_assets` 当前指针表，资产内容继续追加到已有 `comic_entity_versions`；旧 StudioTask、Persona、Shot、Run、Budget、Artifact 不迁移、不删除。编辑、锁定、解锁、删除和恢复均追加修订，作品版本同步递增并用预期版本阻止并发覆盖。锁定版本供后续多个镜头引用；编辑仍保存新修订，锁定指针不会暗中漂移。删除写墓碑，恢复写新修订，历史保留。过去某个 Project 版本的上下文只会读取当时已存在的资产修订，不混入未来资产。

Context Builder 现在可组合当前/指定作品版本的 Brief、与该 Brief 对齐的 DirectorSpec，以及有限的相关资产。显式 `asset_id` 优先；随后只用任务中的**名称/别名精确包含**选择角色和场景；若仅有一个有效 StyleBible 则自动带入。最多选择 8 个，返回每个资产的真实修订号和参考 Artifact ID；不发送全资产库，也不声称已有语义搜索。HTTP 入口为 `/api/comic/projects/{id}/assets`（POST 创建、GET 列表）、`/{asset_id}`（GET、PUT 编辑）、`/{asset_id}/versions`（GET）、`/{asset_id}/lock|restore|delete`（POST）；原 `/context` 可用 `task` 或重复的 `asset_id` 参数选择。尚未建设上传素材、资产自动提取、分镜关联、Prompt Compiler、生图、QC 或视频。

### Phase 5：作品级 Storyboard 与 Shot

迁移 v4 仅增 `comic_storyboards`、`comic_shots` 当前指针表；不可变内容继续写入已有 `comic_entity_versions`，作品版本在同一 SQLite 事务中递增。旧 `tools/storyboard.py` 的固定 20 镜/按剧集覆盖工具与历史 `shot` 表保持兼容，不导入、覆盖或删除旧数据；它的结构与新的可变镜数、资产版本引用不同，不能直接当新作品存储。

`ComicStoryboard` 固定 `project_id` 与创建时的 `director_spec_version`，保存标题、描述、状态及有序 Shot ID；镜头数不固定，单次规划上限 100 仅用于防止异常输出。`ComicShot` 有稳定 `shot_id`、序号、目的、主体、行动、环境、情绪与摄影建议，只引用明确的 Character/Scene/Style 资产 ID **及版本**，不复制完整外观或场景描述。编辑、重排、删除和恢复追加新修订；删除是墓碑，恢复生成新版本。历史 Storyboard 恢复若镜头集合已变，必须先恢复相应 Shot，不能悄悄丢弃镜头。Brief/Director 变更后旧 Storyboard 保留可读，但不能继续编辑为当前制作方案。Phase 5 手动 Shot 仅允许 `draft/planned`；`generating/checking/approved/failed/repairing` 预留给后续真实生产步骤，不允许通过编辑接口冒充执行。

创建分镜可提交手动草案，或明确设置 `generate=true`。AI 规划只接收当前 Project、CreativeBrief、DirectorSpec、最多 8 个相关资产与当前任务，调用现有共享文本模型；模型自行决定镜头数量和节奏。模型输出的资产引用必须出现在传入上下文，存储前还要核验项目、类型、状态与固定修订号。生成或写操作沿用 Core Run/Event 与现有 `trace_id/error_id` 日志，记录作品、分镜、镜头和版本；这些规划 Run 不进入生产任务中心。模型调用的供应商、请求 ID、token、耗时与重试由共享 LLM 日志记录。本阶段不调用图片/视频 Provider，也不创建 Prompt Artifact、预算预占或 QC 结果。

HTTP：`POST/GET /api/comic/projects/{id}/storyboards`；`GET/PUT /api/comic/storyboards/{id}`；`GET /api/comic/storyboards/{id}/versions`；`POST /api/comic/storyboards/{id}/restore`；`POST/GET /api/comic/storyboards/{id}/shots`；`GET/PUT /api/comic/shots/{id}`；`GET /api/comic/shots/{id}/versions`；`POST /api/comic/shots/{id}/delete|restore`。所有写操作需要预期作品版本及相应对象版本，避免并发静默覆盖。Phase 6 从当前 Storyboard/Shot 修订和固定资产版本编译 Prompt；不能让用户编辑 Shot 等同于已生成图片。

### Phase 6：Prompt Compiler 与 Prompt Artifact

`domains/comic/prompts.py` 只做作品级镜头提示词编译。它复用当前 Project/Brief/Director、当前 Storyboard/Shot 和 Shot **显式固定的**角色/场景/风格资产版本；不装入全聊天、其他镜头、旧 Prompt 或无关资产。共享文本模型依据镜头目的、导演方案和目标模型特点生成结构化正向/负向 Prompt，编译器及存储层均拒绝遗漏用户硬约束或资产固定约束的结果。模型标识由 `config/settings.yaml` 提供；编译适配接口可按目标模型扩展，不改图片 Provider。共享 LLM 日志记录模型调用、耗时和可用的 token 用量；领域日志记录版本、资产、编译器和输入输出长度，不写密钥或私有思维链。

迁移 v5 仅追加 `comic_prompts` 当前指针表；每次编译、人工编辑或恢复都在已有 `comic_entity_versions` 中追加 `prompt` 修订，并在**同一 SQLite 事务**写入 Core `ArtifactType.PROMPT`。Prompt 正文与溯源元数据随 Artifact 持久化，领域修订保留完整可比较快照；旧版本与旧 Artifact 不覆盖。每版保存 Brief/Director/Storyboard/Shot、资产 ID/版本、编译器版本、模型、来源 Run 和 Prompt 哈希。作品与 Prompt 的预期版本防止并发写入；镜头变更后须重新编译，旧来源不匹配时禁止直接恢复。复用 Core Run/Event/Trace，不建立第二套任务或媒体存储。当前版本**没有**图片生成、预算预占、视频或 QC。

HTTP：`POST /api/comic/shots/{id}/prompt/compile`（`expected_project_version`、`expected_shot_version`）；`GET /api/comic/shots/{id}/prompt[?version=n]`；`GET /api/comic/shots/{id}/prompt/versions`；`PUT /api/comic/shots/{id}/prompt`（人工编辑，含预期作品/Prompt 版本）；`POST /api/comic/shots/{id}/prompt/restore`（从兼容的旧版本追加新修订）。下一图片生产阶段才把已选定的 Prompt Artifact 作为不可变输入，调用共享 Image Capability，并沿用预算预占、幂等、Artifact、Provider Job 恢复和 QC 的既有基础设施。

## Phase 7.2.6：同一创作 Workspace 的前端投影

漫剧工作台只维护一份 `DirectorWorkspace`，复用现有 Splitpanes、MessageComposer、用户气泡、Markdown 与图片附件。普通/专业模式控制下一次导演接口的 `creation_mode` 以及节点可见深度，不创建第二套 Conversation、Coordinator 或工作流。左侧资产入口切换中央资产页面，不再使用覆盖对话的制作资产 Drawer；Inspector 默认关闭，只占上方节点区，不改变下方聊天宽度。节点编辑草稿按 Project/Run/Stage 隔离，切换页面不丢草稿；实际保存、重跑、恢复与 Prompt 编译调用已有 API，使用已有预期版本检查。

导演 Draft 的确认现在复用服务端 Core Approval，不再由浏览器确认指纹充当权限。`POST /api/comic/projects/{id}/director-spec/confirm` 接收 `version/expected_project_version`，只确认当前 Brief 下已经通过实际 Critic 的不可变导演修订，并检查绑定资产/镜头依赖。确认绑定 Project/Spec/Version/Brief，编辑或恢复产生新版本，不继承旧审批。Storyboard 创建和修改、Shot 操作、Prompt Store 在后端检查审核及确认；创建分镜在文本模型调用前拦截。旧 v1 生产契约保留兼容，新 v2 不能通过直接 API 绕过。确认记录使用同一 Core Run/Approval/Event，不增加数据库表。

当前导演 API 接收 `conversation_id` 并保存在 Core Run，但不追加 Conversation Message。前端为 Project 复用一个既有 Conversation ID，并由真实关联 Run 的用户任务、公开导演摘要与修改记录恢复连续对话展示；不调用旧 Guided 聊天路由去偷偷创建单镜头生产。**数据库 Conversation.messages 同步仍未接入**；用户确认已从共享 Runtime 持久化读取，不依赖当前浏览器。已有单镜头 Run/Artifact/审批/供应商查询保留兼容入口；新作品没有接入图片生产时必须明确说明，不伪装出图。UI 只呈现 v2 白名单公开字段、真实节点摘要和 trace/error ID，不输出旧字段或私有思考。

布局参考 [Figma 导航](https://help.figma.com/hc/en-us/articles/360039831974-Explore-the-navigation-bar-and-left-sidebar)、[Runway 对话式创作](https://help.runwayml.com/hc/en-us/articles/51601639579667-Creating-with-Runway-Agent)、[InvokeAI](https://github.com/invoke-ai/InvokeAI)、[Langflow](https://github.com/langflow-ai/langflow) 与 [ComfyUI Frontend](https://github.com/Comfy-Org/ComfyUI_frontend)。只提炼可收起导航、节点工作区、上下文详情与结果历史，不复制节点 Runtime 或引入另一套组件体系。离线 UI 验收可运行 `frontend/tests/workspace-preview.mjs`，使用隔离端口与内存测试数据；测试数据不写入业务数据库，不冒充真实模型验收。

左侧入口固定为对话、导演、分镜、Prompt、资产、历史。对话入口只是展开同一 Project Conversation，创意理解仍由导演节点和连续聊天共同呈现，不另建“原始创意”页面。已有生成结果作为资产页面的一部分展示，避免再出现覆盖聊天的 Drawer 或独立作品面板。

Director Critic 接受少量旧字段别名时，必须先标准化为 v2 叶子路径再执行白名单校验：`emotion → creative_decision.emotional_target`、`visual_direction → director_plan.visual_focus`、`camera_language → cinematography.camera_language`。标准化不扩展可写范围；硬约束、Project 身份、资产版本及 StyleBible 引用始终不可 Patch。兼容字段由分层方案投影，不能维护两份互相漂移的导演决策。

### Phase 7.3：字段容错、上下文审计与执行中交互

Critic 模型 findings 只读取 `CRITIC_ALLOWED_FIELD_PATHS` 内的公开 v2 叶子字段。已知别名先规范化（包括 `creative_brief.original_request → creative_decision.intent_summary`）；未知/越界字段写脱敏 `critic_warning`、原路径、`action=ignored` 和 Trace 后忽略，不使整个任务失败。输入上下文仍不是可写输出。模型 Patch 必须关联真实 warning、在可写白名单内、预期值匹配且不重复；无法应用的建议转为待人工审核，不放宽直接 `apply_patches` 的保护。确定性硬约束与资产身份/版本审核不受模型字段容错影响；只允许一次自动局部修订，重新审核未通过则等待用户。

DirectorPlan 追加可选 `style_boundary`、`character_expression`、`character_pose`、`character_presence`；沿用已有实体版本存储，不新增表。未设置的新增字段不参与历史审核指纹，避免旧 v2 审核凭据无故失效。每次导演执行记录脱敏 Director Input Snapshot：Project、Conversation、用户请求摘要、Brief 版本、选中资产/记忆版本、前序 Run 与 Trace。四类创意回归使用隔离文本模型替身，只验证路由、上下文与数据契约，不代表真实模型的导演审美评价。

工作台执行中输入框保持可用：补充需求保留发送时模式，在**当前页面**排队，当前 Run 结束后依次提交，错误或结果未知时停止自动处理；可撤回未执行补充。队列不是后端 Conversation Message，刷新会丢失未提交补充，UI 必须明确说明。取消调用现有 Core Run 接口，Coordinator 在阶段前后和 Critic 事件边界检查取消；不能中断已发出的模型 HTTP 请求，但取消后不进入下一阶段、不保存新方案。复用已有 Run/Event，不增加 Runtime、API 或 Workflow。Fast 对话只展示创作理解与导演摘要；Professional 可查看独立节点，页面切换保留原会话与编辑草稿，尊重减少动画设置。

## Director Pipeline 稳定性与真实验收（2026-09-29）

前端 Workspace 大规模布局改造暂缓；本轮仅补现有草稿编辑/确认交互。
收口既有 Director Coordinator / Critic / Context，
不创建 Runtime、Skill、Asset、Prompt 或 Provider 副本。

- **输入隔离**：导演调用只读取当前 Brief、当前任务与选中资产。项目标题/描述属于导航元数据，
  可能保留旧创意，不再送入导演模型充当故事事实；Project API 仍保留导航元数据，
  所有 Context 只传 project_id，兼容的旧导演入口也不能从标题恢复旧创意。
  新的导演 `task` 先经 Context Builder 的语义边界检查（复用共享文本出口，无关键词映射）；
  同方向修改保留 Brief。新方向以当前请求创建独立 Brief 修订，不携带旧偏好或硬约束；
  `CreativeBriefFork` 通过现有 `comic_entity_versions` 和 CAS 追加父版本、原因及归档生命周期，
  不覆写旧 JSON，不新增表。历史快照保留当时状态，`brief_versions()` 投影当前归档状态。
  判断失败直接返回可追踪错误，不能默认继承或伪造新 Brief。GET Context 仍为只读，不调用模型。
  分叉后的隐式资产召回排除该创意边界前创建的资产（改版不改变资产所属方向）；只有明确
  指定 Asset ID 才可跨方向复用。新资产仍复用原资产召回，旧 DirectorSpec/分镜/镜头不自动继承。
  既有聊天历史、其他作品不会自动送入生成上下文。
- **恢复校验**：已经完成的 Run 也先校验任务、Brief/资产版本、Storyboard/Shot 身份，
  再返回原结果。新输入不能用 `resume_run_id` 借回旧方案；显式空资产列表不会偷用旧引用。
  阶段恢复核验实体 ID，而不只比较恰好相同的版本号。
  源 Run 的请求不同时在边界判定和模型调用前拒绝恢复；普通新请求不查找或恢复失败 Run。
  每个 Run 明确保存 `input_brief_id` / `input_brief_version`，生成事件关联输出 DirectorSpec ID
  和版本。输入快照事件的 `creative_context` 公开 brief_used、previous_brief_detected、
  fork_created 和 reason，便于区分沿用、分叉与显式恢复，不保存思维链。
- **审核与写权限分离**：Critic 可以审核 v2 公开决策字段，但 Patch 只允许小范围白名单。
  别名先规范化；未知路径记录 warning 后忽略。硬约束、Project/资产身份与风格引用始终只读。
  逐字证据仅忽略排版空白，不做模糊语义匹配。不可核验的合法字段结论转为 `needs_revision`，
  保留候选并停止下游，不能使 Run 因引文格式直接崩溃，也不能伪装 pass 或应用未核验 Patch。
- **Director Debug Trace**：在原 Run state / Runtime Event 中保存输入快照与每个阶段的
  输入/Context/输出 SHA-256、长度、键名、来源版本、复用阶段与编辑来源。控制台和现有结构化
  日志能按 trace/run/project/skill 关联共享 LLM 请求 ID、provider、model、真实 token、耗时、
  retry、审核规范化 warning 与安全 error_id/traceback；不记录原始无效模型响应或 CoT。
- **草案与审核结果分离**：前三个导演阶段的真实公开输出通过 Schema 后，先保存
  `director_candidate`，发出 `director_draft_created`，再执行 Critic。审核待修订、审核请求
  失败或 Patch 应用失败时保留原草案，Run 为 `waiting`，不伪造 pass，也不启动 Prompt/生图。
  生成阶段失败仍为 `failed`，不默认填充方案。每阶段的 `stage_statuses` / `stage_failures`
  复用现有 Run state，记录 stage_name、trace_id、error_id、input_version、已完成公开输出和
  安全异常摘要；完整脱敏堆栈留在统一日志中。审核失败不得发出审核完成事件或推进完成检查点。
  任务 API 的 `director_spec` 可返回真实草案，并明确 `director_spec_status=draft`、
  `draft=true`、`ready_for_prompt=false`。满足不可变约束的待审草稿追加到原实体版本表，
  返回真实 spec_id/version；违反硬约束的候选只保留 Run，不提升为当前修订。
  通过审核后的已存方案才返回 `reviewed`；ready_for_prompt 还须当前版本的用户确认。
  Fast 隐藏节点输出，但返回最小真实状态和故障原因；Professional
  提供完整公开阶段输出。重启只查询现有 Run，不自动重提；显式恢复只重做必要审核。
  非法 Patch（未知字段、禁止写入、格式错误、过期预期值或替换值不符合 DirectorSpec Schema）
  记录 `INVALID_PATCH` 并忽略该建议。硬约束/资产身份只读，最多自动修订一次，门禁不放宽。
- **真实与离线验收分开**：`evals/director.jsonl` 定义异兽、人物、情绪、科幻四个独立案例。
  `uv run python scripts/verify_director_pipeline.py --allow-paid --max-cny 2` 经现有应用 API、
  Coordinator 和共享 LLM 执行；项目/会话/Run 位于独立 `data/qa/director-live-*/runtime.db`，
  不写生产业务库。输出公开方案、Trace、真实 token、保守费用估算和审核结果到同目录 report。
  配置缺少对应文本价格时拒绝执行；逐调用检查剩余额度，错误/未知用量停止，
  不自动重试、不自动切备用供应商。总额是按配置的保守单价估算，不冒充供应商最终账单。
  四组均完成、实际 pass、主体无串案、视觉策略不完全相同，才能标记通过；
  四个样本不代表所有创意的审美质量已获证明。离线替身只验证工程契约，不替代真实验收。

### 当前创意和可编辑草稿补充（2026-09-29）

- 工作台新输入显式发送 `creative_operation=new`；不同于当前 Brief 时直接分叉，
  不把旧 Brief 交给模型猜测是否沿用。新任务只使用本轮显式绑定资产，旧 StyleBible
  不自动继承。旧客户端的 `auto` 语义边界接口保留兼容；恢复操作仍严格绑定原请求。
  阶段系统指令不再把所有当前任务都解释为旧作品补充。
- 两种模式均可直接编辑公开分层方案；未保存编辑可以放弃，版本历史可恢复为新修订。
  指令式修改必须绑定 `expected_director_version`，只传当前 Brief、当前公开草稿及其资产，
  通过原共享文本出口产生修订。当前方案未选中、版本变化或硬约束/资产引用改动即拒绝。
- 原 POST 的 `draft` 保存人工修订，`revision_instruction` 保存指令修订，均清除旧 Critic
  结果。`creation_mode + review_current=true + expected_director_version` 通过同一 Coordinator
  重新审核当前稿，不重新生成前三阶段、不接受客户端 pass。审核通过仍需显式用户确认。
  手工修改在原 Run 中记录请求、版本与 Conversation，任务 API 投影为保存草稿，
  不伪造 Skill 执行。没有新 Runtime、Skill、Prompt、Asset、Provider 或数据库表。
- 当前修订由后端版本/审批恢复；旧 Run 入口 URL 不应让工作台在刷新后默认编辑第一版。
  前端仅复用已有 DirectorNodeView、MessageComposer、Markdown、历史和 API 客户端，
  保留现有布局与字号。离线浏览器验收复用 `workspace-preview.mjs` 的内存数据，
  与真实付费模型验收分开。

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
