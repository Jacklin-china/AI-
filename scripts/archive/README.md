# 隔离的维护脚本证据

此目录仅供只读审阅，不是运行、导入或自动发现脚本的入口。禁止执行文件、恢复 `.py` 后缀、使用 runpy/SourceFileLoader 加载或批量扫描执行。

| 文件 | 原路径 | 原样 SHA-256 | 隔离原因 |
| --- | --- | --- | --- |
| `_tail_errors.py.disabled` | `scripts/_tail_errors.py`（当时未被 Git 跟踪） | `7450d4d9e9e4d8124cb14a66a717b143bf2d5eda29a8e2e15f93283445997be6` | 导入即批量把 unknown 账单改为 failed 并释放，没有明确拒绝证据 |

2026-10-05 在源码、Skill、测试、动态加载入口、启动配置和项目本地 hook 中未发现调用。随后只移动这一个文件并追加 `.disabled` 后缀；原始内容/hash 保留，没有执行脚本，没有调用预算服务或修改业务账单。

`.disabled` 后缀阻止普通 Python 模块发现及正常 `.py` 脚本扫描；它不是操作系统级执行沙箱。显式把文件交给 Python/自定义 loader 仍可能执行，因此不可重新启用。后续受控对账须独立设计逐条证据、只读预览、备份、合法账本转换和授权。

详细可达性证据及验证结果见 [Module 0 风险报告](../../docs/architecture/governance-module-0.md)。
