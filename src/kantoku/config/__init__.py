"""公共基础设施：配置 / 异常 / 日志。

- 包内相互导入用**相对路径**：`from .errors import ConfigError`（点开头 = 当前包内）
- 外部模块用绝对路径：`from kantoku.config import ConfigError, LLMError`

为什么把 config / errors / logging_setup 放进同一个包？
它们都是"项目底座"，不属于任何业务模块。集中后一眼能看出哪些是基础设施。
"""

from .errors import (
    BudgetError,
    ConfigError,
    KantokuError,
    LLMError,
    SchemaError,
    ToolError,
    TracingError,
)
from .settings import get_settings

__all__ = [
    "KantokuError",
    "ConfigError",
    "LLMError",
    "SchemaError",
    "BudgetError",
    "TracingError",
    "ToolError",
    "get_settings",
]
