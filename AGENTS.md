# Kantoku AI Coding Rules

这个文件是 Kantoku 项目的 AI 开发规则。

任何 AI（Work、Cursor、Claude Code 等）修改代码之前必须先阅读此文件。

后续每次修改前，必须按顺序完整阅读本文件、[AI_CODE_RULES.md](docs/engineering/AI_CODE_RULES.md)、[产品原则](docs/product/product-principles.md)、[工程手册](docs/engineering/README.md)，再核对[架构例外清单](docs/architecture/exceptions.md)及实际调用方，并阅读[运行存储规范](docs/engineering/runtime-policy.md)。已有架构例外只允许兼容维护，不得复制、扩展或借机重构；用户当前任务范围优先。

目标：

1. 保持项目结构清晰
2. 防止 AI 生成重复代码
3. 防止项目越来越大
4. 保证代码可以长期维护
5. 保持 Agent 架构稳定


==================================================

# 一、修改代码前必须做的事情

任何修改之前，不允许直接写代码。


必须按照顺序：

1. 阅读项目文档

查看：

docs/engineering/AI_CODE_RULES.md

docs/product/product-principles.md

docs/engineering/README.md

涉及历史边界问题时同时查看 docs/architecture/exceptions.md；例外不得作为新增违规代码的依据。


2. 检查已有代码

必须确认：

- 是否已经存在类似功能
- 是否已经有相同 Service
- 是否已经有相同 API
- 是否已经有相同组件


3. 判断应该修改哪里


修改前必须回答：

这个问题属于：

- 前端 UI？
- 后端 API？
- Agent Core？
- Domain业务？
- AI能力？
- 外部Provider？


不要为了方便，把代码随便放。


==================================================

# 二、禁止创建重复代码


禁止：

创建类似：

xxx_new.py

xxx_old.py

xxx_backup.py

xxx_final.py

xxx_v2.py


例如：

已有：

image_service.py


不能创建：

image_service_new.py


正确：

直接修改原文件。


历史版本交给 Git 保存。


==================================================

# 三、项目目录规则


项目主要分为：

core

domains

capabilities

providers


--------------------------------

## core

存放 Agent 通用能力。


允许：

- Run
- Workflow
- Node
- Approval
- Artifact
- Memory
- Runtime Event


禁止：

出现业务代码。


禁止：

- 商品
- Ozon
- 漫剧
- 电商
- SKU


Core 不知道业务是什么。


--------------------------------

## domains


存放具体业务。


例如：

domains/comic

domains/commerce


这里处理：

- 漫剧流程
- 电商流程
- 业务规则


不要把业务代码放 Core。


--------------------------------

## capabilities


存放通用 AI 能力。


例如：

- 图片生成
- 视频生成
- 文本生成
- Vision


多个业务可以调用。


--------------------------------

## providers


连接外部服务。


例如：

- OpenAI
- DeepSeek
- Ark
- 即梦


Provider 只负责调用接口。


不要在 Provider 里面写业务逻辑。


==================================================

# 四、禁止项目垃圾文件


以下文件禁止保留：

Python:

__pycache__

*.pyc

.venv


前端:

node_modules

dist


运行文件:

logs

outputs

generated

uploads

runtime


数据库:

*.db

*.sqlite


临时文件:

backup

temp

old


这些文件不要进入项目压缩包。


==================================================

# 五、不要随便增加依赖


安装新的 Python 或 npm 包之前：

必须说明：

1. 为什么需要？
2. 现在项目有没有类似功能？
3. 删除后有什么影响？


不要为了一个小功能安装大量依赖。


==================================================

# 六、修改原则


优先顺序：


第一：

修改已有代码


第二：

优化已有结构


第三：

新增文件


禁止：

遇到问题直接重新创建一套系统。


==================================================

# 七、Agent开发规则


Kantoku 使用统一 Agent。


不要创建：

Comic Agent

Commerce Agent

Video Agent


多个业务应该共用：

- 同一个执行框架
- 同一个 Runtime
- 同一个日志系统


差异通过：

Domain

Skill

Capability

实现。


==================================================

# 八、日志要求


所有错误必须可以找到原因。


错误必须记录：

trace_id

error_id

错误位置

错误原因


禁止：

捕获错误以后直接：

except:

pass


禁止：

隐藏错误。


==================================================

# 九、数据安全


禁止：

- 删除用户数据
- 删除历史记录
- 清空数据库
- 覆盖生产数据


修改数据库必须：

- 有备份
- 有迁移方案


==================================================

# 十、完成修改以后必须检查


完成后必须确认：


□ 有没有重复文件

□ 有没有无用代码

□ 有没有新增垃圾文件

□ 有没有新增依赖

□ 测试是否通过

□ 前端是否正常构建

□ 后端是否正常启动


==================================================

# 十一、启动要求


后端必须支持：

python -m kantoku serve


前端必须支持：

npm run dev


不要依赖隐藏步骤。


==================================================

# 十二、AI工作原则


AI 修改 Kantoku 时：

必须：

- 先分析
- 再修改
- 小范围修改
- 保留已有功能


禁止：

- 大规模重写
- 删除核心代码
- 创建重复架构
- 随意改变目录结构


如果不确定：

选择风险最低的方案。
