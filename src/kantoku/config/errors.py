"""项目统一异常体系。"""


class KantokuError(Exception):
    """所有可向项目上层传递的异常基类。"""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message}｜{self.detail}"
        return self.message


class ConfigError(KantokuError):
    """配置文件缺失、字段不合法，或环境变量（密钥）未设置。"""


class LLMError(KantokuError):
    """LLM 调用失败：超时、重试耗尽、返回结构不符合预期。"""


class SchemaError(KantokuError):
    """原始返回只保存在属性中，禁止通过 str 或日志泄露剧本正文。"""

    def __init__(self, message: str, *, detail: str | None = None, raw: str | None = None) -> None:
        super().__init__(message, detail=detail)
        self.raw = raw


class BudgetError(KantokuError):
    """超出预算红线。调用方必须明确告知用户，禁止默默跳过。"""


class TracingError(KantokuError):
    """Trace 数据库初始化、写入或读取失败。"""


class ToolError(KantokuError):
    """工具注册、参数解析或执行失败。"""
