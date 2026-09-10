# 父异常：总错误基类
class KantokuError(Exception):
    def __init__(self, message: str, *, detail: str | None = None) :
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) :
        if self.detail:
            return f"{self.message}｜{self.detail}"
        return self.message

# 子异常：各个细分业务错误
class ConfigError(KantokuError):
    """配置文件缺失、字段不合法，或环境变量（密钥）未设置。"""

class LLMError(KantokuError):
    """LLM 调用失败：超时、重试耗尽、返回结构不符合预期。"""

class SchemaError(KantokuError):
    """结构化输出解析失败。必须携带原始返回，否则线上无法排查。"""

class BudgetError(KantokuError):
    """超出预算红线。调用方必须明确告知用户，禁止默默跳过。"""
