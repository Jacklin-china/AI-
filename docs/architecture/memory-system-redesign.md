# Kantoku 记忆系统重构设计（参照 WorkBuddy 底层记忆设计）

状态：设计稿（仅设计，不含任何代码修改）。
建立日期：2026-10-11。
适用范围：`src/kantoku/core/conversation_context.py`、`src/kantoku/shells/web_studio.py`、`src/kantoku/domains/comic/projects.py`、`src/kantoku/domains/comic/coordinator.py`、`src/kantoku/config/settings.py`。
前置阅读：[AI 修改规范](../engineering/AI_CODE_RULES.md)、[产品原则](../product/product-principles.md)、[架构例外清单](exceptions.md)、[上下文连续性](comic-context-continuity-feedback.md)。

> 本设计只提出方案，不授权自动重构。任何落地须按规范第 10 节逐模块"修改 → 测试 → 人工验收 → commit"，并遵守 EX-09 对 Run 生命周期收敛的边界。

---

## 0. 结论摘要（TL;DR）

用户反馈的四类问题在本项目中不是四个独立 bug，而是**一个根因的两个投影**：

1. 记忆装配层（`ConversationContext`）没有"层次"，把"用户级事实 / 项目级设定 / 会话级轨迹"全部塞进同一条消息流水，用同一套 token 预算竞争 → 表现为**记忆不成功 + 污染**。
2. 上下文锁层（`CreativeContextLock`）把"锁的有效性"与"导演/Brief 版本号强一致"绑成同一个布尔判断，且失效时**静默返回 None**，无显式释放、无显式迁移、无失效可见性 → 表现为**锁死**。
3. 字段域、动作集合、模式判定在多处以字面量硬编码，且三处持有各自不一致的子集 → 表现为**硬编码 + 字段域撞车报错**（`ERR-3053EA5D`）。

重构方向：引入 **WorkBuddy 式三层记忆（Session / Project / User）+ 只读 profile 注入 + 版本身份化锁 + 声明式能力清单**，将"是否继承"从"版本相等"改为"身份匹配 + 显式失效标记"。

---

## 1. 病症诊断（逐条对应用户反馈 + 代码级根因）

### 1.1 问题一：上下文记忆不成功（记忆继承失败）

**证据链**

- `conversation_context.py:140-404` `prepare()` 把"摘要 + 状态 + 近期消息 + 召回候选"全部序列化进**一条 system 消息**：
  - `conversation_context.py:393-398`：`messages = [{"role": "system", "content": "以下是有来源的会话记忆…" + json.dumps({**base, ...})}, {"role": "user", "content": request}]`
  - 也就是说，模型看到的"记忆"是**一段 18000 token 预算内的扁平 JSON**，没有"哪些是不可变事实、哪些是历史轨迹"的层次。
- `conversation_context.py:179-192`：`pending` 只按"是否被 summary 覆盖"切分，`recent` 按 token 从尾部贪心回填。**没有按任务的活跃度或重要性分层**。
- `conversation_context.py:206-234`：`older` 分块压缩时，`previous_summary` 只保留 `overview/tasks/sources`（`ConversationSummary`，`conversation_context.py:73-77`），压缩是**有损**的；压缩后 `covered` 增加，原始消息不再进入 `recent`。
- `conversation_context.py:286-374`：`interpret` 最多 3 轮循环，若第 3 轮 `reads` 非空直接报错 `"上下文历史读取超过上限，原请求保留"`（`conversation_context.py:335-336`）。长对话中一次请求需要多跳读取时，**整轮记忆装配直接失败**，用户感知即"记忆继承失败"。
- `web_studio.py:2804-2808`：`dynamic_turn` 分支先 `add_conversation_message` 落用户消息，再 `prepare(through_message_id=user_turn.id)`。若 `prepare` 抛 `ToolError`，用户消息**已经落库**但本轮无 `remember`，下轮该消息只作为普通历史参与压缩——形成"半写入"状态。

**触发路径重现（ERR-3053EA5D 侧）**

1. Guided 会话发一条依赖上一轮导演方案的请求（如"继续，把镜头拉近"）。
2. `interpret` 第 1 轮返回 `reads=[{kind:"run", ...}]`，第 2 轮又返回新 `reads`，第 3 轮仍有 `reads` → `conversation_context.py:335` 抛 `"上下文历史读取超过上限"`。
3. 该 `ToolError` 冒泡到 `_create_comic_director_mode` 外层，前端得到 `ERR-3053EA5D` 的通用包装。
4. 用户再发一条试图"补救"，因为上一条已成为普通历史（未 remember），上下文锚点与任务状态错位，表现为"记忆没继承上"。

> **同源放大（ERR-91A181FF）**：本问题的**另一条**表现路径是接口校验失败被误报为生图失败。文档随附的另一张报错截图 `图像生成失败 · ERR-91A181FF` 经日志溯源与 `ERR-3053EA5D` 属**同一次请求、同一 trace `trace-b378ca7aeb8f`**，终点同为 `web_studio.py:2860` 的 `raise ToolError("继承字段不属于导演上下文范围")`——即 `ERR-91A181FF` **并非生图故障**，而是 §1.4 字段域错配（P12）的同一 fatal 在 frontend 被错误归因成语义的结果（详见 §1.4 与 §5 P15/P16）。

**根因**：记忆装配是**无层次、单预算、单轮有损压缩**，且把"读取不足"从可降级错误升级为**硬失败**；`prepare`/`remember` 不是原子对。

---

### 1.2 问题二：上下文污染

**证据链**

- `conversation_context.py:393-397`：system 消息把 `base`（含 `summary`、`task_state`、`recent_dialogue`、`recalled`）**整体**注入，且明确写着"只使用本轮相关来源"。但 `base` 里没有任何"与本轮相关性"的物理隔离——相关性完全依赖模型自律，服务器**不做裁剪**。
- `conversation_context.py:271-277`：`recalled` 候选在 `interpret` 之前就被 `evidence.update(recalled)` 合并（第 277 行），但候选来源是 `_prepare_conversation_context` 的 `candidates`（`web_studio.py:2345-2353`），**默认包含最近 40 条 Run 的摘要**，且 `read_required=True` 的候选也进了 `recalled`。这些候选的文本会出现在 system JSON 里（`base["recalled"]`），即使模型未选它——**污染面在"未读候选已进上下文"**。
- 摘要压缩把"模型创作选择"与"用户事实"混存于 `TaskProgress.facts`，仅靠 `MemoryFact.origin` 区分（`conversation_context.py:33-38`）。`_validate_facts`（`conversation_context.py:417-439`）只在**新增**事实时校验原文连续摘录；`verified_tasks`（第 377-379 行）复用的历史事实**不再校验内容**，只校验 `(task_id, value, origin, source)` 四元组是否已存在。若历史摘要把模型选择误标为 `user_request`，**污染会被永久继承**。
- `conversation_context.py:206-207`：压缩输入 `previous_summary` 直接作为下一轮摘要的基底，错误一旦进入摘要，**沿链条复利放大**。
- `web_studio.py:2974-2985`：`creative_context` 把 `prepared_context.public()`、`locked_bibles`、`request` **整体**塞进 `input_request`，经 `coordinator.py:1325-1326` `payload["conversation_context"] = request.creative_context` 进入导演 Prompt。**上一轮的 `request`（含旧 task、旧 asset 选择）随之进入本轮模型输入**——这是最直接的跨轮污染通道。

**根因**：缺少"污染隔离策略"：无只读事实层、无"候选不入上下文、只有读过的才入"的强制门、无按来源可信度分层；摘要链是**不可回滚的单一真相**。

---

### 1.3 问题三：创作上下文锁被锁死（无法释放）

**证据链**

- `projects.py:591-604` `creative_context_lock()`：读最新锁，**只有当** `lock["director_version"] == snapshot.project.director_version` **且** `lock["brief_version"] == snapshot.creative_brief.version` 才返回，否则 **静默 `return None`**（第 604 行）。没有任何日志、事件或返回值区分"没有锁"与"锁因版本漂移而失效"。
- `web_studio.py:3122-3140` `_comic_context_lock()`：任一条件不满足（`director_id is None` / `director.creative_brief_version != snapshot.creative_brief.version` / `schema_version != 2` / 未确认 / 无 owner Run）都 `return None`。**六层门禁全部静默**。
- `web_studio.py:2949-2960`：`dynamic_turn` 分支在 `original != snapshot.creative_brief.original_request` 时 `fork_brief`，或在 `user_constraints` 变化时 `replace_brief`——**新建/替换 Brief 后，旧锁的 `brief_version` 立即不再等于新 Brief 版本**（`projects.py:601-602`）。
- `web_studio.py:2967-2973`：fork 后新建的 `lock` 只在新方案 `save_director` 后经 `coordinator.py:711-714` / `web_studio.py:2671-2672` 写回。但 `save_context_lock`（`projects.py:606-619`）要求 `snapshot.project.director_version == spec.version` 且 `snapshot.creative_brief.version == spec.creative_brief_version`——**若 fork 与 save_director 之间存在任何版本推进，save_context_lock 抛 `"导演方案已变化，请刷新后继承创作设定"`**，锁既不更新也不清除，旧锁永远对新版本失效。
- `web_studio.py:3114-3119`：仅在 `lock and (changes or recovery)` 且 `current_director_version != lock["director_version"]` 时重写锁；**`lock` 为 None 时（即已静默失效）这段补偿根本不执行** → 死锁闭环。

**触发路径重现**

1. 会话已有有效锁（绑定 Brief v3 / Director v2）。
2. 用户说一句被判定为"新创作方向"的话（`relation=new`，`web_studio.py:2830` `references=[]`）。
3. `web_studio.py:2949` 触发 `fork_brief` → Brief v4；`independent_plan` 或 fork 分支下 `director_version` 推进。
4. 下一轮普通请求：`_comic_context_lock` 在 `web_studio.py:3132` 因 `director.creative_brief_version(v3) != snapshot.creative_brief.version(v4)` 返回 None。
5. 既 `无法继承`（锁读不出来），又 `无法通过普通消息释放`（因为普通消息路径 `web_studio.py:3005-3030` 也先 `_comic_context_lock(...)` 返回 None 才跳过，锁从未被显式清空，但永远不匹配）→ 用户体感"锁死"。

**根因**：锁的**有效性**与**版本号字节相等**耦合；失效状态**不可观测、不可显式释放、不可显式迁移**；fork/replace 路径没有"锁的显式前移"事务。

---

### 1.4 问题四：记忆系统被"写死"（硬编码）

**证据链（字面量清单）**

- `web_studio.py:2851-2852`：`allowed = {"character_context", "world_context", "style_context", "camera_style", "color", "lighting"}` —— **6 个粗粒度域硬编码**，且与 `projects.py` 的 `CreativeContextLock` 六类（character/world/visual_style/camera_style/environment/fixed_attributes）**命名不一致**。
- `web_studio.py:2896-2901`：`camera_fields` 映射把 `camera_style` 展开成 `{shot_size, camera_angle, camera_distance, spatial_feel, camera_language, depth_strategy, lens_or_spatial_feel, material_language}` —— 硬编码叶子集合。
- `web_studio.py:2853-2857`：`leaves` 由 `type(body).model_fields` 动态生成（**这部分是对的**），但 `allowed.update(leaves)` 后又用第 2859 行 `if any(field not in allowed ...)` 校验——**动态叶子与硬编码粗域拼接**，两套来源做校验。
- `web_studio.py:2921-2922`：`scopes` 初始集合 `{"character_context", "world_context", "style_context", "camera_style", "color", "lighting", "action"}` —— **又一份硬编码**，与 2851 行不一致（多了 `action`）。
- `projects.py:974-975`：`resolve_edit_scopes` 的 `allowed` 又是同一份 6 元集合 + `action` —— **第三份**。
- `projects.py:653-654` / `projects.py:670-672`：`{"character_context": "character", "world_context": "world", "style_context": "visual_style"}` 映射在 `save_context_lock` 与 `locked_bibles` **两处重复**。
- `coordinator.py:2662-2667`：`affected` 映射（`character_context` → `{director_plan.character_presence}` 等）**第四份**字段域硬编码。
- `conversation_router.py:25-27`：`allowed_actions=("chat", "create", "clarify", "image.generate", "image.edit", "video.generate", "comic_production", "commerce_production")` —— **动作集合硬编码在装配入口**，`ConversationAction` 枚举（`conversation_router.py:30-37`）是**另一套**（含 `workflow.start/choose_domain/workspace.open`，不含 `comic_production/commerce_production`）→ 两套动作词汇，语义漂移。
- `conversation_context.py:103-105`：`ConversationContext.__init__` 的默认 `allowed_actions` 又是**第三套**（`chat/create/clarify/image.generate/image.edit/video.generate`）。
- `web_studio.py:2789-2793`：`dynamic_turn` 判定用**一长串布尔取反**写死（`not (resume_run_id or previous_run_id or review_current or draft or revision_instruction or stage_edits)`），新增请求字段极易忘记加入这个取反清单。
- `projects.py:813-833` `explicit_context_changes` 与 `projects.py:1001-1017` `edit_scopes`：大量**中文字面量正则**（`换|更换|替换…角色|人物…`），与 `projects.py:940-950 starts_new_context`、`projects.py:952-968 action_semantic_cluster` 同属一套关键词表，散落四处、彼此不共享。

**根因**：字段域/动作集合/模式判定以**字面量在多处独立维护**，违反 AI_CODE_RULES §2 "禁止平行实现" 与 One Core 原则。字段域不一致正是 `ERR-3053EA5D`（"继承字段不属于导演上下文范围"）的直接成因：**动态回合可能把细粒度叶子写入 `reference.fields`，而 `allowed` 的粗粒度拼装与其命名空间对不齐**。经日志溯源，用户报错截图 `图像生成失败 · ERR-91A181FF`（与 `ERR-3053EA5D` 同 trace `trace-b378ca7aeb8f`）**是同一缺陷的同一 fatal**，仅被前端错误归因为"图片制作失败"——即本问题实际产出了两个用户可见错误码，均随字段域声明化一并消除（P12 根治 + P16 文案归因）。

---

## 2. 设计目标与原则（可迁移的 WorkBuddy 记忆理念）

参照 WorkBuddy / Claude Code 底层记忆设计的可迁移要点，映射到 Kantoku：

| WorkBuddy 理念 | Kantoku 对应设计 | 解决的问题 |
| --- | --- | --- |
| **分层记忆**：用户级 / 项目级 / 会话级 | 新增 MemoryScope 三层：`user`（跨会话偏好）、`project`（作品 Brief/设定/Bible = 现有 CreativeContextLock 的正当归属）、`session`（会话轨迹 = ConversationContext 的正当归属） | 污染（层间不再混存）、继承失败（层间各自独立预算） |
| **自动注入 profile（只读）** | 用户级事实以**只读投影**注入，明确 `readonly=true`，模型不得改写，服务器强制校验 | 污染（模型选择被误升为用户事实） |
| **明文强制规则文件** | 领域声明文件（见 §3.3）声明字段域/动作集合，替代散落字面量 | 硬编码 |
| **工作区日志增量追加** | 会话轨迹只允许**追加**，压缩只产生新摘要版本（parent_artifact_id 链），不覆盖 | 污染（可回滚）、继承失败 |
| **预算化注入** | 三层各自独立预算，不再共享单一 18000 预算 | 继承失败（长对话挤压） |
| **事实抽取必须带来源引用** | 沿用并**加强** `_validate_facts`，含历史复用的再校验 | 污染 |
| **超限蒸馏归档** | 压缩失败**降级为只读截断归档**，绝不硬失败 | 继承失败 |
| **版本漂移可见、可释放、可迁移** | 锁引入显示状态枚举 + 显式 release/migrate 操作 | 锁死 |

**核心原则**：

1. **身份优先于版本**（Identity over Version）：锁的匹配基于"稳定身份"（`conversation_id` + `project_id` + Brief 的 `brief_id` 血统），版本号只用于**检测漂移并触发迁移**，不再用于**决定是否可用**。
2. **失效必须显式**（Fail Loud, Not Silent）：任何 `return None` 的静默失效改为返回带 `status` 的锁对象或抛出可分类的 `ToolError`，并写入 Runtime Event。
3. **追加不改写**（Append-only Trajectory）：会话记忆与摘要只追加新版本，历史可回读。
4. **声明单一真相**（Single Declaration）：字段域/动作集合/模式判定收敛到一处声明，其余全部引用。
5. **降级优于失败**（Degrade, Don't Fail）：记忆装配的读取超限、压缩超预算等，降级为"有界截断 + 显式提示"，不阻断用户请求。

---

## 3. 核心机制重构方案

### 3.1 ConversationContext：prepare / remember 改进

#### 3.1.1 数据结构：引入分层 envelope

在 `conversation_context.py` 新增（不改现有 dataclass 名称，保持兼容）：

```python
class MemoryLayer(StrEnum):
    USER = "user"        # 只读 profile 事实，跨会话
    PROJECT = "project"  # 作品设定/Bible 引用（只读投影，不拥有内容）
    SESSION = "session"  # 会话轨迹：任务进度、历史决策

class LayeredMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user: list[MemoryFact] = Field(default_factory=list, max_length=20)     # 只读
    project: dict[str, Any] = Field(default_factory=dict)                   # 只读引用
    session: list[TaskProgress] = Field(default_factory=list, max_length=12)
```

- `user` 层只读：`prepare()` 注入时打标 `"readonly": True`；`_validate_facts` 拒绝任何对 `user` 层 `value` 的**新增或改写**（只能引用已存在的 user fact）。
- `project` 层只是**引用**（`{project_id, brief_id, brief_version, director_version, lock_status}`），不复制 Bible 内容——Bible 内容仍在 `domains/comic/prompts` 的 Prompt Artifact（`projects.py:645-657` 现有契约）。
- `session` 层承载现有 `TaskProgress`。

#### 3.1.2 prepare：三层独立预算 + 各层装配

替换 `conversation_context.py:183-284` 的单预算装配：

```
user_budget   = settings.context_user_tokens    (新增，默认 1500)
project_budget= settings.context_project_tokens (新增，默认 2000)
session_budget= settings.context_token_budget - user - project  (沿用现有 18000 上限)
```

- **user 层**：一次性装配（变化极少），命中缓存则复用。
- **project 层**：由调用方注入的只读 project 引用（`web_studio.py` 已有 `creative_context`，改为只投影 `{project_id,brief_id,brief_version,director_version,lock_status}`，**移除 `request`、`locked_bibles` 的整对象塞入**——见 §3.1.4 污染修复）。
- **session 层**：保留现有 recent/older 压缩逻辑，但压缩失败时**降级**（见 3.1.3）。

**污染隔离的强制门**（修复 §1.2）：

- 候选召回（`conversation_context.py:271-277`）改为：**未 `read_required` 的候选才允许进 `evidence` 的 `recalled`；`read_required=True` 的候选进 `available`（仅 id/kind），不进模型可见文本**。只有当 `interpret` 通过 `reads` 真正读取后，才从 `available` 移入 `recalled`。
- `base` 序列化时，`summary` 只注入 `overview`（叙述性），`tasks` 拆到 `session.tasks` 并**逐事实带 `source` + `origin`**，模型不得跨 task 复用（`_validate_facts` 已强制 task_id 匹配，保留）。
- system 消息新增显式约束句："`user` 层为只读事实，不得改写；`project` 层为引用，不得推定内容；`session` 层轨迹仅作参考。"

#### 3.1.3 超限与读取：降级而非失败（修复 §1.1）

- `conversation_context.py:335-336` `reads` 超限：由**抛错**改为**降级**——保留已读内容，丢弃后续 `reads`，在 `PreparedContext` 增加 `degraded: list[str]` 字段记录 `"reads_truncated"`，并在 system 消息末尾追加"部分历史未能读取，如需要请明确指定范围"。请求**继续执行**。
- `conversation_context.py:196-234` 压缩超预算：由**抛错**改为**截断归档**——对超预算 `compressed` 做确定性裁剪（按 `overview` → 保留 `tasks` 中 `origin=user_request/asset_fact` 的事实 → 丢弃 `model_choice`），若仍超预算则以"截断标记 + 原始消息 ID 列表"作为摘要，`covered` **不推进**（下轮可重压），并记录 `"summary_truncated"` 到 `degraded`。
- `conversation_context.py:233-234`：保留"未替换旧摘要"语义，但不再抛错中断本轮。

#### 3.1.4 prepare / remember 原子化（修复 §1.1 半写入）

- 新增 `ConversationContext.prepare_and_remember(...)`：内部先 `prepare`（纯读，无副作用），成功后再 `remember`。`web_studio.py:2799-2808` 改为：**先 prepare，后落用户消息 + remember**（或在 prepare 失败时对已落消息做补偿标记）。这消除"用户消息已落库但记忆未写"的半写入。
- `remember`（`conversation_context.py:441-448`）保持追加语义，但 `metadata` 增加 `schema_version=2`、`layers` 摘要与 `degraded`，便于回读与审计。旧 `conversation.context` Artifact 保持可读（`prepare` 已按 `through_message_id` 定位，见 `conversation_context.py:131-138`）。

#### 3.1.5 事实抽取：历史复用再校验（修复 §1.2）

- `conversation_context.py:417-429`：`verified` 四元组命中时，**不再无条件 `continue`**。对 `origin in {user_request, asset_fact}` 的历史事实，要求其 `source` 仍在 `evidence` 中且可核验；核验失败则将该事实标 `superseded`（而非静默继承）。
- 新增"来源可信度"校验：`model_choice` 事实**永不允许**作为锁的 Bible 来源（`web_studio.py:2887-2889` 的 `bible_sources` 只接受 `artifact`/`prompt` 来源）。

---

### 3.2 CreativeContextLock：版本耦合解耦（修复 §1.3）

#### 3.2.1 锁的显示状态枚举

新增（`domains/comic/models.py`）：

```python
class ContextLockStatus(StrEnum):
    ACTIVE = "active"          # 身份匹配 + 版本一致
    DRIFTED = "drifted"        # 身份匹配但版本已推进，可迁移
    ORPHANED = "orphaned"      # 身份不匹配（owner Run 丢失/跨会话）
    RELEASED = "released"      # 显式释放
    ABSENT = "absent"          # 从未建立
```

`creative_context_lock()`（`projects.py:591-604`）改为**永不返回裸 None**，而是返回 `(lock | None, ContextLockStatus, meta)` 或结构化对象：

```python
@dataclass
class LockResolution:
    lock: dict | None
    status: ContextLockStatus
    reason: str                 # 人类可读，用于错误与事件
    migratable: bool
```

判定逻辑：

- 身份匹配 = `lock["project_id"] == project_id` 且锁的 `brief_id` 与当前 Brief **同血统**（`current.brief_id == lock_brief_id` 或 `current.parent_brief_id == lock_brief_id`，允许一次 fork 血统延续）。
- 身份匹配 + 版本一致 → `ACTIVE`，返回 lock。
- 身份匹配 + 版本漂移 → `DRIFTED`，`migratable=True`；此时**仍返回 lock 内容**（供迁移），但调用方必须走迁移路径。
- 身份不匹配 → `ORPHANED`，`lock=None`。
- 无记录 → `ABSENT`。

#### 3.2.2 显式迁移（migrate）与显式释放（release）

新增两个显式 API：

- **migrate**（`projects.py`）：`migrate_context_lock(project_id, conversation_id, *, to_brief_version, to_director_version, carried_fields: set[str]) -> dict`
  - 在 `BEGIN IMMEDIATE`（沿用 `projects.py:608-609`）内：读旧锁 → 校验身份血统 → 只迁移 `carried_fields` 声明的域（未声明的域释放）→ 写新版本锁。
  - 语义：**迁移不是"复制旧值"，而是"声明哪些域跨版本延续"**；未声明域对新版本为空（由模型重新决定），避免污染。
- **release**（`projects.py`）：`release_context_lock(project_id, conversation_id, *, released_fields: set[str] | None, reason: str) -> None`
  - `released_fields=None` 表示整锁释放（写 `status=RELEASED` 的墓碑版本，而非删除记录，保证审计链）。
  - 由 `web_studio.py` 在"显式新创作方向"分支（`resolution.relation == "new"`）调用，替代当前 `references=[]` 的隐式丢弃。

#### 3.2.3 fork / replace Brief 路径的锁事务化

`web_studio.py:2949-2973` 修改为：

1. 判定需要 fork/replace 时，先取 `LockResolution`。
2. 若 `DRIFTED`/`ACTIVE`：执行 `migrate_context_lock(..., carried_fields=<本轮未释放的域>)`，**与 fork 在同一个 `BEGIN IMMEDIATE` 事务内**（或紧随其后原子补偿），杜绝 §1.3 步骤 3-5 的窗口。
3. 若 `ORPHANED`/`ABSENT`：不迁移，记 `ABSENT` 事件。
4. `save_context_lock`（`projects.py:606-619`）放宽：不再要求 `snapshot.project.director_version == spec.version` 严格相等，而是要求 **`spec.version` 是当前 Brief 血统下的最新版本**（允许同 Brief 内增量推进），否则抛可分类错误而非静默。

#### 3.2.4 失效可见性

- `_comic_context_lock`（`web_studio.py:3122-3140`）六层门禁各自返回带 `reason` 的 `LockResolution`，并 `logger.info` + `runtime_store.append_event(NODE_PROGRESS, payload={"lock_status": ..., "reason": ...})`。
- 前端可通过公开投影看到"当前会话设定锁：漂移待迁移 / 已释放"，替代当前的静默禁用。

#### 3.2.5 迁移期幂等

- 迁移/释放写墓碑版本（`entity_type='creative_context_lock'`, `version+1`），沿用 `projects.py:659-664` 的"内容变化才写新版本"语义，重复提交同请求幂等（对齐 `comic-context-continuity-feedback.md` §"同一个 fork 请求的重复提交保持幂等"）。

---

### 3.3 消除硬编码：单一声明驱动（修复 §1.4）

#### 3.3.1 新建设定域声明（Domain 层）

在 `domains/comic/` 新增声明模块（**合并自现有分散来源，非平行实现**）：

```python
# domains/comic/context_declaration.py
@dataclass(frozen=True)
class ContextFieldGroup:
    key: str                      # 粗粒度域，如 "character_context"
    label: str                    # 公开标签，如 "character"
    lock_slot: str                # 锁槽位，如 "character"
    director_leaves: tuple[str, ...]   # 对应 director_plan 叶子，如 ("character_presence",)
    cinematography_leaves: tuple[str, ...] = ()
    bible_slot: str | None = None      # 对应 Bible 槽位，如 "character"

CONTEXT_FIELD_GROUPS: tuple[ContextFieldGroup, ...] = (
    ContextFieldGroup("character_context", "character", "character",
                      ("character_presence",), bible_slot="character"),
    ContextFieldGroup("world_context", "world", "world",
                      ("subject_environment_relation",), bible_slot="world"),
    ContextFieldGroup("style_context", "visual_style", "visual_style",
                      ("style_boundary", "color_strategy"), bible_slot="visual_style"),
    ContextFieldGroup("camera_style", "camera_style", "camera_style",
                      (), ("shot_size", "camera_angle", "camera_distance",
                           "spatial_feel", "camera_language", "depth_strategy",
                           "lens_or_spatial_feel", "material_language")),
    ContextFieldGroup("color", "color", "color", (), ("color_relationship",)),
    ContextFieldGroup("lighting", "lighting", "lighting",
                      (), ("lighting", "light_source", "light_direction")),
    ContextFieldGroup("environment", "environment", "environment",
                      ("subject_environment_relation",)),
)

def context_leaves() -> set[str]: ...       # 供 web_studio allowed 校验
def lock_slot_map() -> dict[str, str]: ...  # 供 save_context_lock/locked_bibles
def group_keys() -> frozenset[str]: ...     # 供 scopes/resolve_edit_scopes
def affected_map() -> dict[str, set[str]]: ...  # 供 coordinator 的 affected
```

**收敛点**：`web_studio.py:2851-2852/2896-2901/2921-2922`、`projects.py:653-654/670-672/974-975`、`coordinator.py:2662-2667` **全部改为引用该声明**。字段域从此只有一处真相。

> 注意：`CreativeContextLock` 的六类（character/world/visual_style/camera_style/environment/fixed_attributes，`comic-context-continuity-feedback.md:29`）与继承校验的六域（character_context/world_context/style_context/camera_style/color/lighting）**名称必须对齐**。声明模块须显式给出两套命名的映射（`group_keys()` vs `lock_slot_map()`），根治 `ERR-3053EA5D` 的"继承字段不属于导演上下文范围"。

#### 3.3.2 动作集合声明

- 新增 `core/conversation_actions.py`（或扩展现有 `conversation_router.py`）：**唯一** `ALLOWED_MEMORY_ACTIONS` 与 `ConversationAction` 映射，`conversation_router.py:25-27`、`conversation_context.py:103-105` 默认值均引用之。
- `TurnUnderstanding.action` 的枚举校验（`conversation_context.py:182`、`:331-332`）从该声明派生。

#### 3.3.3 dynamic_turn 判定声明化

`web_studio.py:2789-2793` 改为：

```python
# 声明式："哪些请求字段一旦出现就表示非动态回合"
NON_DYNAMIC_REQUEST_FIELDS = ("resume_run_id", "previous_run_id", "review_current",
                              "draft", "revision_instruction", "stage_edits")
def is_dynamic_turn(request) -> bool:
    return bool(request.independent_plan and request.conversation_id and request.task
                and not any(getattr(request, f) for f in NON_DYNAMIC_REQUEST_FIELDS))
```

新增请求字段时，只需在 `Request` 声明处标注，判定不再散写。

#### 3.3.4 关键词表收敛

- `explicit_context_changes` / `edit_scopes` / `starts_new_context` / `action_semantic_cluster`（`projects.py:813-833/940-968/1001-1017`）的中文正则抽到**同一模块**的规则表，并优先交给模型判定（`check_intent_boundary`、`resolve_edit_scopes` 已是模型驱动）。字面量正则仅作为**离线/无模型回退**，标注 `fallback_only=True`。

---

## 4. 数据与接口变更

### 4.1 Artifact schema

| Artifact `node_id` | 变更 | 兼容策略 |
| --- | --- | --- |
| `conversation.context` | `metadata` 新增 `schema_version=2`、`layers`（user/project/session 摘要）、`status`、`degraded` | 旧记录无 `schema_version` 视为 v1；`prepare` 读取时按 v1 解析（现有 `through_message_id` 定位已兼容，`conversation_context.py:131-138`） |
| `conversation.summary` | `metadata` 新增 `schema_version=2`、`truncated` 标记 | 旧摘要继续可读（`conversation_context.py:166-178`） |

### 4.2 数据库表

| 表 | 变更 | 迁移 |
| --- | --- | --- |
| `comic_entity_versions` | `payload_json` 内 `CreativeContextLock` 新增可选字段 `brief_id`、`status`、`released_fields`、`migrated_from` | **无 ALTER，无新表**（沿用 JSON payload，`projects.py:100-110` 现有结构）；旧锁缺字段按默认值读取（对齐 `comic-context-continuity-feedback.md:29`） |
| 新增索引 | 无需 | 现有 `idx_comic_entity_latest`（`projects.py:107-110`）已覆盖 |

**不需要**新增数据库 schema 版本——`CreativeContextLock` 校验（`projects.py:658`）以 pydantic 加默认字段即可向后兼容。

### 4.3 函数签名变化

| 函数 | 变化 | 兼容 |
| --- | --- | --- |
| `ConversationContext.prepare` | 返回 `PreparedContext` 增加 `degraded` 字段（默认 `[]`） | 新字段有默认值，旧调用不受影响 |
| `ConversationContext` 新增 `prepare_and_remember` | 新方法 | 新增，不改旧接口 |
| `ComicProjectStore.creative_context_lock` | 由返回 `dict|None` 变为返回 `LockResolution`（或保留旧方法 + 新增 `resolve_context_lock`） | **推荐保留 `creative_context_lock` 语义不变**（返回 lock 内容或 None），**新增** `resolve_context_lock` 返回结构化状态，`web_studio` 迁移到新方法；旧调用点（`director.py`、`storyboards.py`）保持 |
| `ComicProjectStore.migrate_context_lock` / `release_context_lock` | 新增 | 新增 |
| `ComicProjectStore.save_context_lock` | 放宽"最新版本"判定，错误分类化 | 行为兼容（原先会抛错的场景改为抛带分类的错误；成功路径不变） |
| `Settings`（`RuntimeSettings`） | 新增 `context_user_tokens`（默认 1500）、`context_project_tokens`（默认 2000） | 新增字段带默认值，配置向后兼容 |

### 4.4 迁移策略

1. **只读先行**：先上线 `resolve_context_lock`（返回状态）与日志/事件，**不改变**现有 `creative_context_lock` 行为，观察真实会话中锁状态分布。
2. **锁迁移开关**：`RuntimeSettings` 增加 `context_lock_migration`（默认 `"off"` → `"on"`），灰度开启 fork 事务化。
3. **旧锁兼容**：无 `brief_id` 的旧锁，按 `snapshot.creative_brief.brief_id` 反推血统；无法反推标 `ORPHANED` 并记事件，不自动迁移。
4. **Artifact v1/v2 共存**：读取按 `metadata.schema_version` 分派，写入只产生 v2。

---

## 5. 逐问题修复清单

| # | 问题 | 修改点 | 涉及文件:行号 | 测试方式 | 验收标准 |
| --- | --- | --- | --- | --- | --- |
| P1 | 记忆继承失败（reads 超限硬失败） | `reads` 超限降级 + `degraded` 记录 | `conversation_context.py:335-336` | 单测：构造 3+ 轮 reads 的会话，断言不抛错且 `degraded==["reads_truncated"]` | 依赖多跳读取的请求能完成；有 `degraded` 标记 |
| P2 | 记忆继承失败（半写入） | `prepare_and_remember` 原子化 | `conversation_context.py:441-448`、`web_studio.py:2799-2808` | 单测：prepare 抛错后断言用户消息未落库或已补偿 | 无"已落消息但无 remember"状态 |
| P3 | 记忆继承失败（压缩超预算硬失败） | 压缩降级为截断归档 | `conversation_context.py:196-234` | 单测：构造超预算摘要，断言返回截断摘要且 `covered` 未推进 | 长对话可持续进行 |
| P4 | 上下文污染（未读候选入上下文） | 候选分离为 `available`/`recalled` | `conversation_context.py:271-277`、`web_studio.py:2345-2353` | 单测：`read_required=True` 候选不出现在 system JSON | system 消息中不含未读候选正文 |
| P5 | 上下文污染（旧 request 塞入） | `creative_context` 只投影引用 | `web_studio.py:2974-2985`、`coordinator.py:1325-1326` | 单测：断言 payload 无上一轮 `request` 全文 | 导演输入不含上轮 task 正文 |
| P6 | 上下文污染（历史事实复利） | 历史事实复用再校验 | `conversation_context.py:417-429` | 单测：篡改历史 user_request 值，断言被标 `superseded` | 污染事实不跨轮继承 |
| P7 | 上下文污染（层混存） | 三层预算 + 只读 user 层 | `conversation_context.py:183-284` | 单测：user 事实尝试改写被拒 | user 层不可改写 |
| P8 | 锁死（静默 None） | `resolve_context_lock` 返回状态 | `projects.py:591-604`、`web_studio.py:3122-3140` | 单测：版本漂移返回 `DRIFTED` 且 `migratable` | 失效可见，有事件 |
| P9 | 锁死（fork 无迁移） | fork/replace 事务化迁移 | `web_studio.py:2949-2973`、`projects.py:606-619` | 集成：fork 后锁状态为 `ACTIVE`（迁移成功） | fork 后仍可继承未释放域 |
| P10 | 锁死（无法显式释放） | `release_context_lock` + `relation=new` 调用 | `projects.py`（新增）、`web_studio.py:2830` | 单测：`new` 请求后锁为 `RELEASED` | 普通消息可显式释放 |
| P11 | 硬编码（字段域三处不一致） | `context_declaration.py` 单一声明 | `web_studio.py:2851-2852/2896-2901/2921-2922`、`projects.py:653-654/670-672/974-975`、`coordinator.py:2662-2667` | 单测：断言各引用点取值一致 | 字段域只有一处真相 |
| P12 | ERR-3053EA5D（继承字段超范围） | allowed 由声明生成（含叶子） | `web_studio.py:2851-2859` | 回归：动态回合细粒度叶子不再报错 | 不再出现"继承字段不属于导演上下文范围" |
| P13 | 硬编码（动作集合三套） | 动作声明单源 | `conversation_router.py:25-27/30-37`、`conversation_context.py:103-105` | 单测：断言集合来自同一常量 | 动作词汇一致 |
| P14 | 硬编码（dynamic_turn） | 声明式判定 | `web_studio.py:2789-2793` | 单测：新增请求字段不影响判定 | 判定不随字段增删漂移 |
| P15 | ERR-91A181FF（误判为生图失败） | 经日志溯源确认为 P12 的表现，非独立 Provider 问题；修复 P12（Module B）即消除 | `web_studio.py:2859-2860`（P12 修复点） | 集成：修复 P12 后重试原 trace `trace-b378ca7aeb8f` 的请求 | 不再出现 `继承字段不属于导演上下文范围`，ERR-91A181FF 消失 |
| P16 | 错误归因文案误导（把导演阶段 fatal 显示为"图片制作失败"） | 前端兜底文案按 Run 真实失败节点归因；后端 `failure` 已含 `error_kind`/`safe_message`，应透传而非丢弃 | `frontend/src/components/DirectorWorkspace.vue:594`、后端 `observability.py:140-149` | 集成：构造导演阶段失败 Run，断言前端显示真实原因 | 导演方案阶段失败时显示"继承字段…"等真实原因，不再出现"图片制作失败"的错误归因 |

> **ERR-91A181FF 说明（已由日志更正）**：该错误与「继承字段不属于导演上下文范围」是**同一次请求、同一 trace `trace-b378ca7aeb8f`**，调用链终点为 `web_studio.py:2860` 的 `raise ToolError("继承字段不属于导演上下文范围")`。即 `ERR-91A181FF` **就是 P12 缺陷的表现**，与 Provider / 生图工具（`tools/image_gen.py`）**毫无关系**，不再列入 Module E。
>
> 真实因果链：dynamic_turn 请求 → `understanding.sources[].fields` 含越界字段 → `web_studio.py:2859` 校验失败 → `:2860` fatal → Run 标 failed 且 `run.error` 为空 → 前端 `DirectorWorkspace.vue:594` 兜底文案 `run.error ?? '图片制作失败'` 显示为"图片制作失败" → 用户误判"生图坏了"。这是**两个缺陷叠加**：根因 P12（字段域错配且 fatal 而非降级，Module B 覆盖）+ 放大器 P16（前端错误归因文案，需单独修）。

---

## 6. 风险评估与回归防线

### 6.1 并发（SQLite / BEGIN IMMEDIATE）

- 锁迁移/释放沿用 `projects.py:608-609` 的 `BEGIN IMMEDIATE`，与 `save_context_lock`、`confirm_director`（`projects.py:744-746`）共用同一写锁，避免交叉事务。
- fork + migrate 必须在**同一事务**内，否则重演 §1.3 窗口。`_connect` 上下文管理器（`projects.py:57-76`）已保证 commit/rollback，迁移逻辑放在同一 `with self._connect()` 块内。
- 新增事件写入 `runtime_store.append_event` 属不同 SQLite 连接，须在锁事务**提交后**执行，避免跨库死锁（对齐 `projects.py:767` 的"先持有写锁验证，再提交后写审批"经验）。

### 6.2 审计链

- 释放写**墓碑版本**而非删除，保证 `comic_entity_versions` 全历史可回溯（`projects.py:100-106` 主键含 version）。
- 每次锁状态变化（`DRIFTED`/`RELEASED`/迁移）写 Runtime Event，payload 含 `conversation_id/project_id/director_version/brief_version/status/reason`，对齐 AI_CODE_RULES §7 字段表。
- 绝不把 API Key / 私有思维链写入事件（§7）。

### 6.3 既有 Run / Artifact 兼容

- **不改** `DirectorSpec` / `PromptCompiler` schema（对齐 `comic-context-continuity-feedback.md:45`）。
- 旧 `conversation.context` / `conversation.summary` Artifact 按 `schema_version` 缺省=v1 读取。
- 旧锁无 `brief_id` 按血统反推，无法反推则 `ORPHANED`，**不自动迁移**、**不删除**。
- `coordinator.py:640-652/705-714` 两处 `save_context_lock` 调用保持签名兼容（放宽判定，不破坏成功路径）。
- EX-09 边界：本设计**不新增** Orchestrator/Runtime；锁迁移只在 `ComicProjectStore` 内完成，不动 Run 生命周期。

### 6.4 回归防线（分层测试）

| 层 | 覆盖 |
| --- | --- |
| 单测 | `conversation_context.py` 的 prepare/remember 各分支；`context_declaration` 一致性 |
| 集成 | `tests/test_dynamic_director_context.py`、`tests/test_comic_context_continuity.py`、`tests/test_comic_director_drafts.py` 扩展：fork 后迁移、`new` 后释放、漂移可见 |
| 契约 | 断言三处字段域引用值一致（防回归硬编码） |
| 离线 | 全部使用离线模型替身（对齐 `comic-context-continuity-feedback.md:125`），不调用付费模型 |

### 6.5 回滚

- 新机制以配置开关（`context_lock_migration`、分层预算默认值）控制；开关关闭时行为回退到当前实现（保留旧方法入口）。
- 墓碑记录可被旧代码忽略（旧 `creative_context_lock` 读最新版本，若最新为 `RELEASED` 墓碑则自然返回 None，等价旧语义），**向前兼容不劣化**。

---

## 7. 实施顺序建议（对齐 AI_CODE_RULES §10 分模块）

1. **Module A（只读可见性）**：`resolve_context_lock` + 事件 + 日志，不改变行为。
2. **Module B（声明单源）**：`context_declaration.py` + 替换五处硬编码 + 契约测试（修复 P11-P14，直接消除 `ERR-3053EA5D` 与同源的 `ERR-91A181FF`）。
3. **Module C（记忆分层与污染隔离）**：三层预算 + 候选分离 + 历史事实再校验 + 降级（修复 P1-P7）。
4. **Module D（锁迁移与释放）**：`migrate`/`release` + fork 事务化 + 开关灰度（修复 P8-P10）。
5. **Module E（错误归因与可观测性修正）**：P16 前端兜底文案按真实失败节点归因 + 后端 `failure` 的 `error_kind`/`safe_message` 透传（`ERR-91A181FF` 随 Module B 的 P12 一并消除）。

每个 Module 独立"修改 → 测试 → 人工验收 → commit"，前一模块未验收不进入下一模块。
