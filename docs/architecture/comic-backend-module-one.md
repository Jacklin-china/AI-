# Comic 后端模块一：语义与素材底座

范围：用户已批准的三模块计划中的第一模块。第二模块为图片分析/改图闭环，第三模块为故事组装/导出。本模块不修改前端源码、样式和静态包，不启动 WorkBuddy 接力。

## 设计与复用

- `constraints.py` 是 Comic 领域的原文到视觉要求转换及覆盖核验，不是第二套 Memory 或 Prompt 编译器。Core `MemoryFact` 仍保留并严格核验原文；模型调用从 Shell 注入现有共享文本出口。
- `media.py` 保存直接导入图片的内容版本、选择和参考用途。现有资产 Store 处理角色/场景/风格资产，现有 Prompt 上传入口依赖 Shot/Prompt；直接图片具有不同生命周期，不能伪造空 Shot。图片仍是 Core IMAGE Artifact，关系仍进入 `comic_entity_versions`，不新增业务表或依赖。
- Prompt 继续经过原 Compiler 和 Store。完整模型输出 Schema 同时声明摘要、四段和负向正文，避免指令要求六字段而 Schema 只给四字段。新输出记录 `comic-prompt-4`；旧记录不改写。
- 新动态 Brief 使用 `constraint_contract_version=2`，Prompt 在现有 `context_sources` 中声明 `semantic-contexts-2`。固定的 Schema、版本、权限和容量是协议约束；人物、动作、视觉内容和继承判断由语义输入决定，不按验收例子的词汇匹配。

## 语义契约

`CreativeConstraint` 包含 requirement、target、scope、source_ref、source_quote、origin、trust、status、verification。requirement 表示要求的意思；exact 的 literal_value 单独保存实际显示文字，不能把委托说明打印到画面上。用户要求、资产事实、模型选择分开记录；只有活跃用户要求进入 Brief 的用户硬约束列表。

本轮已读的用户事实加入口已保存的当前原文参与提取。独立核验检查提取是否完整、忠实及范围是否正确。服务端核验连续原文位置，拒绝未知来源。相同来源和相同提取输入的成功证据可复用；仅来源 ID 相同不能覆盖新增内容。纠正/撤回在当前任务状态生效，新任务不带入未选旧约束。

普通视觉要求允许准确改写。exact 只用于用户明确指定的字面内容，由已核验的原文片段提供值；Compiler 将其放进 Shot 的受保护指定文字字段。模型继续设计位置、构图和摄影，不能改写指定文字。手工删掉指定文字会被拦截。

新动态完整 Prompt 不调用关键词搬移、非 Shot 关键词禁入或局部继承补判。旧兼容协议仍保留原契约。新协议允许不适用的段落为空，整份为空仍拒绝。导演显式声明 characters_present，false 时人物身份/姿态/表情留 null；旧记录未声明时保持兼容，并保留原审核指纹。

最终审核返回覆盖状态和冻结正文的字段位置，服务端提取原文作为证据。审核凭据绑定要求、正向正文和负向正文；编辑后不能靠客户端提交 pass 凭据跳过核验。未核验输出、已返回但不完整的输出、供应商失败与结果未知分别停止或走原有一次结构修复；结果未知不自动重提。

每次新语义调用检查既有上下文容量，所有必需要求受数量和长度约束。超过容量停止，不删除必需证据。有效提取与失败诊断写入内部 JSON Artifact/日志，内部诊断不作为下一轮创作事实。

## 图片契约

`ImageVersion` 固定 image_id、version、project_id、conversation_id、artifact_id、sha256、MIME、filename、parent、instruction_artifact_id 和 provenance。原始字节不变；追加改图版本不覆盖源文件，也不自动选择新版。

选择单独有 selection_version；参考绑定单独有 reference_version。参考 role 为 identity、style、composition，可多选，必须指明 target 和明确的图片版本。上传、列表和绑定不调用模型。参考输入标注 not_analyzed，不声称已经看到图片细节；视觉观察留给第二模块。

同聊天其他合法作品的图片可通过显式绑定关联；不隐式继承，也不允许跨聊天。追加同一 image_id 不能转移归属。新图片链可显式指向同聊天旧作品的父版本，原链保持不变。

读取创作依据时核验聊天/作品归属、Artifact 类型/状态/生命周期、来源版本和原文件 hash。更新参考用途后，旧导演不能继续编译 Prompt；不适用的旧审核不会自动变成 pass。导演及 Prompt 的来源信息保留精确参考快照，Prompt 返回 reference_attachments 供前端接文件。

新写请求必须有 request_id 及 expected_project_version；追加图片、选择、参考还校验各自版本。业务修订、Artifact、作品 CAS 和请求结果同事务保存。相同请求返回原结果；相同 ID 携带不同参数、并发竞争和过期版本报错。文件写入复用 RuntimeManager 的原子出口；事务失败只清理本次文件。

## API

所有入口沿用 Studio 验证和错误出口；GET 要提供当前 conversation_id，不调用模型。

| 方法 | 路径 | 主要内容 |
| --- | --- | --- |
| POST | `/api/comic/projects/{id}/images` | filename、data_url、conversation_id、request_id、expected_project_version；追加时指定 image_id、expected_image_version、parent |
| GET | `/api/comic/projects/{id}/images` | 最新内容版本和独立 selection |
| GET | `/api/comic/images/{id}/versions` | 不可变版本链 |
| PUT | `/api/comic/images/{id}/selection` | version、expected_selection_version 及公共写字段 |
| GET / PUT | `/api/comic/projects/{id}/references` | bindings（image、roles、target）；写入需 expected_reference_version 及公共写字段 |

图片上传沿用 5 MB、PNG/JPEG/WebP 编码和文件签名边界，只给导图路由扩大请求体上限。此处不是视觉质量或完整解码验收。

## 验收与限制

新用例集中在 `tests/test_comic_semantic_media.py`，保持 12 个核心场景，覆盖原文/改写、虚构证据、撤回换题、指定文字、审核缓存、无人物/空段、直接导入、重试重启、版本与选择、并发、范围/生命周期及真实 HTTP/导演输入。既有记忆测试只验证读取和继承，使用明确的语义边界替身；不能把它们当成真实语义验收。

真实验收复用 `scripts/verify_director_pipeline.py --suite semantic-media`，四个主题为人物肖像、机械吉祥物、无人建筑和指定文字。`--natural` 检查真实专业自然入口，`--case` 只复验受影响主题。历史失败保留；不可绕开审核把候选算成成功。没有外部生图、改图或视频调用，不能据此宣称实际图像质量通过。

自然入口的隔离 QA 只有在真实 Critic pass 后模拟确认按钮，记录 `qa_fixture_after_actual_critic_pass`；它验证确认协议，不代表用户对生产作品的人工艺术验收。

三模块工程模型验收共用预算身份 `comic-backend-three-modules-20261012`，累计上限 2 元。账目在受管理的 `data/paid-qa/budget-*.json`，先按配置单价预占上界，有实际用量再结算；未知用量保留上界并停止。不更换预算 ID 重新获得额度。报告/隔离数据库不进入 Git 或交付包。

本模块的后端接口可验收，页面接入由前端负责。第二模块尚未实现视觉观察/发现项/改图指令，第三模块尚未实现故事组装/导出。需先完成本模块人工验收及独立提交，再进入下一模块；混合工作区不得整批提交。

## 2026-10-12 验收记录

- 12 个新核心场景、62 个相关回归合计 74 项通过；追加 3 项导演兼容/依据检查通过。后续原样文字契约调整后又检查受影响的 24 项，用例通过，不跑全仓数百项。
- 结构检查和受影响文件的静态检查通过。标准 `python -m kantoku serve` 在 RuntimeManager 隔离会话中启动，首页 200；退出后端口关闭、数据库无 WAL/SHM，隔离目录清理完成。
- 四个真实文本语义主题均通过，分阶段报告留存。人物/吉祥物/建筑的成功记录在 `data/paid-qa/semantic-media-live-c244705170294ff490915267306d0801/report.json`（该报告还保留当时指定文字未通过的记录）；指定文字的最终自然入口成功记录在 `data/paid-qa/semantic-media-live-00ad044c7bf144cc9178e8d6d779349e/report.json`。失败没有伪装为 pass，所有修复都针对协议/结构，不添加案例名称或字词判断。
- 包含失败实测在内的累计配置单价估算为 1.156008 元，2 元上限内剩余 0.843992 元。未调用真实图片/视频模型，未跑前端构建，未提交混合工作区。
- 人工验收待用户确认；本轮不是整套三模块完成。

## 历史召回容量修复（2026-10-12）

用户人工修改时的 `ERR-BC9E1E2A` 在 Core 历史补读阶段触发。对同一聊天的两份真实方案做只读复现：正文估算 4452 + 5019 = 9471 tokens，超过局部召回上限 6000；重新理解的完整输入估算 12977，仍低于总上限 18000。失败那轮未持久化模型读取清单，因此该组合证明容量分配缺陷，不冒称已还原供应商当时的来源选择。

- 归属 Core；继续复用 `ConversationContext` 和注入的原读取器。`sources`、新增事实及展示产物的依赖优先补读，可使用剩余总容量；探索性正文继续受召回额度限制。正文读入后重新理解，三轮补读及一次最终理解不变。
- 发送前先裁可选目录、未选正文和非必需近期窗口，必需正文保持完整。已经读取的消息/来源直接复用；被移出模型输入的正文仍留在本轮核验缓存，重新选择时须再次进入理解输入。重复读取不重复计费调用；错误版本、会话归属、未来内容和结果未知的原保护不变。
- 现有 `context` / 内部 JSON Artifact 追加 `budget`，原日志记录阶段、来源身份、请求/实际版本、估算容量和 Trace。容量错误附安全诊断，不包含来源正文或私有推理。保持原请求字段，不修改配置额度、数据表、费用/审批策略或前端。
- 本轮新增 7 个核心回归场景，含实际导演输入检查；相关三组测试共 **60 项通过**。范围/锚点测试继续检查真实读取范围，同时验证近期正文复用，不再要求重复装配。验证命令：`uv run --no-sync python -B -m pytest tests/test_conversation_context.py tests/test_dynamic_director_context.py tests/test_comic_semantic_media.py -q -o addopts=''`。
- 上述真实正文的只读复验通过：两次离线理解输入估算 3045 / 12977，最终输入 10929；生产写入和付费调用均为 0。实际导演输入测试使用其他题材，选中字段进入，未选背景正文排除；它不代表真实模型语义质量或图片质量验收。
- 受影响文件的 ruff、差异空白和结构检查通过。标准 CLI 入口在 RuntimeManager 隔离会话启动，首页 HTTP 200；停止后端口关闭，自有目录与侧文件已清理。无新增/删除文件、依赖或数据库迁移；未运行前端构建，未提交混合工作区。

人工复验：用户已确认人工测试没有问题，授权先处理测试清理再进入模块二。旧残留永久删除受自动审批限制，用户随后明确选择先继续模块二并保留清单。共享工作区仍有他人改动，本轮没有将其整批提交；模块二范围见独立交付记录。
