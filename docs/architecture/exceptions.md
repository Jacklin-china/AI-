# Kantoku 架构例外清单

建立日期：2026-10-05；基线 HEAD `c24cb81` 和当前工作区。依据：[架构审计](audit-report.md)、[AI 修改规范](../engineering/AI_CODE_RULES.md)。

这是已存在、暂不迁移的具体债务清单，不是对新代码的豁免。本模块冻结架构，只登记、设边界和退出条件；不移动这些业务文件，不改变接口、状态、默认流程或用户数据。

## 使用及变更规则

- 新代码遵守 Core / Domain / Capability / Provider / UI 边界，不能因为旧例外存在而复制或扩展它。
- 普通修复优先复用既有接口，保持兼容；若需要增加越界依赖，先说明必要性并要求独立治理范围确认。
- 不把整个 `tools/`、`adapters/` 或 `capabilities/video/` 目录设为白名单；只登记下表的具体职责问题。
- 每项保留兼容原因、冻结范围、退出条件及拟治理模块；负责人暂记“待人工指定”，不虚构责任归属或截止日期。
- 退出必须有调用方清单、数据/接口兼容方案、相关测试及人工验收。禁止先删旧实现或清空历史，再声称完成迁移。
- 本清单是人工治理依据，未自动接入 CI；结构检查通过不代表所有例外已消除。

## 现有例外

| ID | 文件及既有债务 | 暂时保留原因 | 新代码边界 / 冻结范围 | 退出条件 / 拟模块 |
| --- | --- | --- | --- | --- |
| EX-01 | `src/kantoku/core/conversations.py::IntentPlanner` 含 Comic/Commerce/Ozon/SKU 路由语义 | 对话和现有 planner 依赖其输出 | 不再向 Core 追加领域词表、业务分支或领域服务 | 经现有路由注入领域规则，行为对照及恢复测试通过；Module 1 Core 边界 |
| EX-02 | `src/kantoku/core/llm.py` 持有 OpenAI-compatible 文本 SDK/传输 | 唯一共享文本出口及阶段适配器依赖 chat | 不新增第二个 LLM 网关；不在 Core 追加供应商业务规则 | 保留 chat 兼容门面，逐步委托明确能力/协议契约，错误和请求关联兼容；Module 1 |
| EX-03 | `src/kantoku/core/budget.py` 的 project/episode/shot_no 和图片计价/结果耦合 | 旧账本、图片请求及 Commerce 兼容身份依赖 | 不新建 Domain Budget；不删旧列/账单，不把 unknown 当 failed | 同一账本增量迁移通用身份/计价，备份恢复及历史对照通过；Module 1，状态变更另属 Module 4 |
| EX-04 | `src/kantoku/core/runtime/models.py::ArtifactType.LISTING` | 已有 Commerce Artifact 读取与渲染依赖 | 不继续为每个业务追加 Core 枚举；领域信息使用既有 metadata/renderer | 通用扩展契约和旧记录兼容验收后迁移；Module 1/4 |
| EX-05 | `src/kantoku/domains/comic/services.py::StudioComicServices.provider_for` 与 workflow 对具体 Provider/服务的识别 | 旧 `comic.production.v1`、Studio/首页生产契约保留 | 不接入新 Comic 外部 Prompt 默认路径，不新增 Domain 直接 Provider 调用 | 沿用共享能力并在组装入口注入；旧 Run/请求/未知账单恢复兼容；Module 1 |
| EX-06 | `src/kantoku/tools/image_gen.py` 共享生图执行、`openai_image.py`/`jimeng.py` 具体协议仍在 tools | 多入口已复用同一 gen_image、预算认领和查询 | 不复制新 ImageService，不重写 Qwen，不为目录整齐移动文件 | 契约和调用者先确认，原接口/供应商请求幂等保持；单独批准能力边界治理 |
| EX-07 | `src/kantoku/adapters/commerce/product_image.py` 将 Commerce 输入映射到 legacy 镜头预算身份，并依赖 tools.gen_image | Domain Port + Adapter 本身合法；兼容原图能力及账本身份 | 不把整个 adapters 目录判为违规；不借此直接从 Domain 调供应商 | 随 EX-03/06 调整适配，不更改 Commerce 工作流和真实/Mock 标记；Module 1 |
| EX-08 | `src/kantoku/capabilities/video/service.py` 并发提交认领及未知费用归零风险；当前 provider 仅 Mock | 现有协议和测试需要；尚无真实视频提供方 | 真实 Provider 上线前阻断；不将 Mock 称为真实；本模块不开发 Video | 复用预算原子认领，未知费用不归零，失败/恢复/并发验收；未来独立能力启用模块 |
| EX-09 | ComicDirectorCoordinator、GraphRuntime、Shell tracked action 分别维护通用 Run 生命周期 | 已有导演恢复、节点业务门禁及轻量操作依赖 | 不新建 Orchestrator/Agent/Runtime；不继续复制取消/事件/恢复实现 | 在既有 Runtime 钩子上收敛，保留导演业务阶段及事件兼容；独立编排治理 |
| EX-10 | `core/tracing.py` 持久化表缺 trace/run/artifact/request 关联；日志各入口字段不齐 | 旧 trace 表、CLI 和现有日志消费依赖 | 不新建 Trace 系统，不伪造缺失 ID，不把知识 hash 说成因果证明 | 同一表增量迁移及端到端关联证明；Module 2 Trace 统一 |
| EX-11 | `tools/prompt_factory.py`、`tools/studio.py`、Shell enhancer、`capabilities/creative.py` 与 Comic Compiler 输入契约分散；Persona/Recipe 历史存储 | 首页、CLI、Studio 和旧数据尚有入口 | 新 Comic Prompt 固定复用 DirectorSpec → 原 PromptCompiler → Core Artifact；禁止 PromptBuilder/Helper/Manager/Generator 平行入口 | 契约矩阵、调用方/旧数据迁移和输出/来源对照通过；Module 3 Prompt 链路 |
| EX-12 | `domains/comic/prompts.py` 确认/上传投影遍历大量历史 Run 和 IMAGE Artifact | 保留已验收确认指纹/版本及外部上传关联 | 不用缓存或新表另存互相竞争的确认状态，不修改本模块 Prompt Pipeline | 现有 Store 受约束查询/索引、大历史与过期版本验证；独立批准 Prompt 查询治理 |
| EX-13 | `domains/comic/skills.py::comic_skills` 与文件 manifest 重复声明 compose/archive ID | Web 仅用 SkillLoader，但外部/动态调用未完全证明不存在 | 不新建 Registry 或复制 Skill；仅保留兼容候选 | 确认外部调用后收敛同 ID 定义，Registry/manifest 回归；独立卫生模块 |
| EX-14 | 自由字符串 Artifact status、前端重复状态及跨对象投影 | 现有 API/历史记录依赖，实体状态含义不同 | 不全库替换 pending/waiting，不合并 Provider/Budget/Run 状态 | 先验收转换表、等待原因、终态/恢复及旧 API 兼容；Module 4 状态治理 |
| EX-15 | `frontend/src/corePresentation.ts` 的 Commerce 呈现；部分测试源码抽取及跨测试夹具引用 | 现有 UI 和离线契约测试有效 | 本模块不改 UI/测试；不为整改复制组件或 presenter | 复用既有 domains presenter，独立行为测试/夹具整理；后续维护性模块 |

## 非例外与已隔离风险

`_tail_errors.py` 的无证据释放 unknown 账单不属于可接受的架构例外。原文件已原样隔离为 `scripts/archive/_tail_errors.py.disabled`；禁止执行、加载、恢复或将其作为对账模板，详见 [Module 0 风险报告](governance-module-0.md)。

SkillLoader、SkillRegistry、ComicContextBuilder、DirectorSpec/来源追踪、现有 Prompt Artifact 以及统一 Runtime/Artifact/Budget 均保留；不因登记例外另建替代系统。前端静态 bundle 是必要交付物，不按重复源码处理。

## 后续顺序

2026-10-06 用户通过 Module 0 整体验收并授权提交；最新顺序为：Module 1 Prompt 链路（EX-11/EX-12）→ Module 2 Trace 统一（trace_id/run_id/artifact_id/skill_version/prompt_version，EX-10）→ Module 3 状态模型（EX-14，先建转换表）→ Module 4 Core 边界（EX-01～EX-04，兼容迁移）。表内早期拟模块编号作为初始审计记录，当前优先级以本节为准。

当前暂停 EX-01～EX-04 全面重构。Prompt 治理复用现有 Compiler，不改变默认外部图片流程，不修改 Image Provider 或 Core Runtime。每步单独修改、测试、人工验收、commit；后续开发范围以用户明确指令为准。
