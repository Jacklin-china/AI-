# Kantoku 产品设计原则

> 本文规定产品目标与功能设计边界，不表示下述能力都已在当前版本实现。开展功能设计、代码修改或架构调整前，先读本文，再核对实际代码与配置。

## 产品定位

Kantoku 是 **AI Production Supervisor**。用户用自然语言提出需求，系统理解意图、调用共享能力或专业工作流、交付结果，并在需要时进行 QC、返工和人工决策。Kantoku 不局限于聊天、生图或生视频中的任何一种形态。

整个产品遵守 **One Core. Multiple Domains.** 首页是轻量自动入口；创作域是专业生产入口。两者共享同一套底层能力和业务记录。

## 两种交互模式

| 模式 | 入口与体验 | 人工参与 |
|---|---|---|
| Autonomous | 首页默认模式。用户不必先选功能，直接描述需求；系统区分普通聊天、单图生成、改图、生视频和复杂任务，并尽量直接完成简单任务。 | 只在预算、质量、风险或任务本身需要时打断用户。 |
| Guided | 用户进入漫剧、电商、广告、Studio 等创作域。系统提供领域上下文、引导提问、专业工作流、中间结果、提示词编辑、QC 与返工。 | 用户可以检查阶段结果并作必要的确认或修改。 |

功能按钮选择的是**领域上下文和交互策略**，不是能力开关。进入创作域后仍须判断本次请求是聊天、图片、视频，还是完整领域生产；一句话不应自动等于启动整个 Workflow。

## Conversation 与任务分流

Conversation 是所有自然语言请求的统一入口，保存用户消息、系统响应及任务关联。其后的 Tool Router 应将请求路由到 `chat`、`image.generate`、`image.edit`、`video.generate` 或 `workflow.start` 等明确动作。聊天入口只协调路由与呈现，不持续堆积各领域的 `if image / if video / if comic / if commerce` 业务分支。

普通聊天和单图生成、简单改图、简单媒体生成属于轻量任务，可直接调用共享 Service 或 Capability。多镜头、多阶段、需要持续 QC、返工或人工决策的任务进入 Graph Runtime / Workflow。首页绕过重型 Workflow 时，仍必须保留预算、日志、Artifact、持久化与幂等保护。

AI 能自行完成的步骤应尽量完成；人工只处理必要的审核、费用或方向决策。SSE 用于实时展示，不承担历史记录的持久化。

## 分层与复用

- **Core** 只提供领域无关的 Run、State、Graph、Node、Checkpoint、Approval、Artifact、Batch、Budget、Skill 与 Runtime Event。Core 不理解 Comic、Commerce、Ozon、SKU、Storyboard 等领域词汇。
- **Domain** 负责专业知识、Schema、Workflow、Policy、Skill 与领域 Prompt；Domain 不复制 Core 的机制。
- **Capability** 提供可复用的文本生成、Prompt Enhancement、生图、视觉理解与视频生成能力；首页和各创作域调用同一能力。
- **Provider / Adapter** 负责具体模型或平台的协议。业务 Workflow 不绑定单一供应商；更换模型优先调整配置或 Provider 层，不改上层业务规则。Provider 不承载领域逻辑。

漫剧域是专业视觉内容生产域：可以制作单图、多镜头、Storyboard、角色一致性画面、视觉 QC、返工、图生视频和完整漫剧。进入漫剧域并不预设用户要制作完整漫剧。

Prompt Enhancer 是共享能力。首页可在后台自动优化，通常无需展示；创作域在用户开启“AI 提示词渲染”时展示优化后的 Prompt，并允许继续修改。不得为首页与漫剧域各造一套实现。

图片与视频共享 Budget、Artifact、日志、Provider 抽象与错误处理，但执行生命周期可以不同：图片可以同步完成；视频通常按 `submit → task_id → query/poll → result` 异步完成。

## 结果、费用与恢复

所有生产结果，包括图片、视频、Prompt、文档和报告，统一成为持久化 Artifact。Artifact 应记录来源，关联 Conversation 与相关 Workflow/Run，并支持父子版本关系。首页生成的媒体必须属于当前聊天；刷新或重开历史聊天后仍能从数据库恢复并展示，不能只依赖 SSE 或临时文件路径。

首页和创作域共用 Budget。首页可在配置允许的小额范围内自动执行，以减少频繁审批；复杂或高成本任务要求用户确认。任何轻量体验都不能绕过预占、单次提交、查询与结算，也不能自动重提未知账单。

**Retry** 是同一请求在网络或临时故障后的继续执行，沿用原请求与供应商任务，不重复付费。**Regenerate** 是用户主动要求新的结果，应创建新的生成请求与计费行为。两者在界面、记录与执行语义上必须区分。

日志应贯通 Conversation、Run、Node、Provider、Artifact 与 Error。界面展示的 Error ID 必须能定位后端完整、脱敏的异常记录。

## 前端与扩展

首页呈现自然 AI 聊天体验，轻量、低认知负担，结果直接出现在聊天中。创作域呈现专业 AI Production Workspace，提供阶段状态、中间产物、编辑、QC、返工与确认。两种界面服务于不同深度的工作，共享 Conversation、Prompt Enhancer、Image、Video、Vision、Budget、Artifact、Logging、Provider 与 Idempotency。

新增 Comic、Commerce、Ads、Studio 或其他 Domain 时，复用现有 Core、Capability、Artifact、Budget、Conversation、Logging 与 Provider；不复制整套系统。设计新功能前，先判断它是通用能力、领域规则、供应商适配，还是交互策略，并查找可复用的现有实现。
