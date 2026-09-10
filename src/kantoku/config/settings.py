from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from kantoku.config.errors import ConfigError

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "settings.yaml"
EXAMPLE_PATH = ROOT / "config" / "settings.example.yaml"


class AppSettings(BaseModel):
    name: str = "kantoku-agent"
    log_level: str = "INFO"


class LlmSettings(BaseModel):
    """LLM 相关配置。字段名必须与 config/settings.yaml 的 llm 段一一对应。"""

    # 主力（文本 / 工具调用）
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    model_chat: str = "doubao-seed-2-1-turbo-260628"
    api_key_env: str = "ARK_API_KEY"

    # 视觉位
    vision_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    model_vision: str = "doubao-seed-2-1-turbo-260628"
    vision_api_key_env: str = "ARK_API_KEY"

    # 降级位
    fallback_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    fallback_model_chat: str = "glm-4.6"
    fallback_api_key_env: str = "BIGMODEL_API_KEY"

    # 调用参数
    temperature: float = 0.7
    structured_temperature: float = 0.2
    max_tokens: int = 2048
    timeout_s: int = 60
    retry: int = 2

    def chat_api_key(self) -> str:
        return _require_env(self.api_key_env)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KANTOKU_",extra="ignore")
    app: AppSettings = AppSettings()
    llm: LlmSettings = LlmSettings()
def _require_env(name: str):
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"环境变量 {name} 未设置",
            detail="请在项目根目录的 .env 里配置，或先执行："
                   f'$env:{name}="你的密钥"（PowerShell）',
        )
        return value
def _read_yaml():
    path=CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    if not path.exists():
        raise ConfigError("找不到配置文件", detail=f"期望路径：{CONFIG_PATH}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

@lru_cache(maxsize=1)
def get_Settings():
    raw = _read_yaml()
    return Settings(
        app=AppSettings(**raw.get("app",{})),
        lmm=LlmSettings(**raw.get("lmm",{}))
    )