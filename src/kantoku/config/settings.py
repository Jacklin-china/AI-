"""读取并校验 YAML 配置，在调用时从环境或 .env 获取密钥。"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "settings.yaml"
EXAMPLE_PATH = ROOT / "config" / "settings.example.yaml"
ENV_PATH = ROOT / ".env"


class AppSettings(BaseModel):
    """应用自身的基础配置。"""

    name: str
    log_level: str


class LlmSettings(BaseModel):
    """LLM 配置；字段名与 YAML 中的 ``llm`` 段保持一致。"""

    base_url: str
    model_chat: str
    api_key_env: str
    chat_extra_body: dict[str, Any]

    vision_base_url: str
    model_vision: str
    vision_api_key_env: str
    vision_extra_body: dict[str, Any]
    vision_max_tokens: int = Field(gt=0, strict=True)
    vision_max_image_bytes: int = Field(gt=0, strict=True)

    fallback_base_url: str
    fallback_model_chat: str
    fallback_api_key_env: str
    fallback_extra_body: dict[str, Any]

    model_config = ConfigDict(allow_inf_nan=False)

    temperature: float = Field(ge=0, le=2)
    structured_temperature: float = Field(ge=0, le=2)
    max_tokens: int = Field(gt=0, strict=True)
    timeout_s: float = Field(gt=0)
    retry: int = Field(ge=0, strict=True)
    retry_backoff_s: float = Field(default=1.0, ge=0)
    chat_use_temperature: bool = True
    chat_max_tokens_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    vision_use_temperature: bool = True
    vision_max_tokens_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    fallback_use_temperature: bool = True
    fallback_max_tokens_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"

    def chat_api_key(self) -> str:
        """读取主聊天模型的密钥。"""
        return _require_env(self.api_key_env)

    def vision_api_key(self) -> str:
        """读取视觉模型的密钥。"""
        return _require_env(self.vision_api_key_env)

    def fallback_api_key(self) -> str:
        """读取备用聊天模型的密钥。"""
        return _require_env(self.fallback_api_key_env)


class BudgetSettings(BaseModel):
    """付费调用的预算和并发上限。"""

    accounting_utc_offset_hours: int = Field(ge=-12, le=14, strict=True)
    image_credit_cny: Decimal = Field(gt=0, allow_inf_nan=False)
    image_estimated_credits_per_call: int = Field(gt=0, strict=True)
    image_estimated_cny_per_call: Decimal | None = Field(
        default=None, gt=0, allow_inf_nan=False
    )
    image_daily_cny: Decimal = Field(ge=0, allow_inf_nan=False)
    image_project_cny: Decimal | None = Field(ge=0, allow_inf_nan=False)
    image_episode_cny: Decimal | None = Field(ge=0, allow_inf_nan=False)
    image_shot_cny: Decimal | None = Field(ge=0, allow_inf_nan=False)
    image_max_concurrency: int = Field(gt=0, strict=True)
    token_daily_limit: int = Field(gt=0, strict=True)


class ImageSettings(BaseModel):
    """官方生图供应商参数；提交动作与查询动作分开配置。"""

    provider: str
    base_url: str
    model: str
    region: str
    service: str
    api_version: str
    submit_action: str
    query_action: str
    access_key_env: str
    secret_key_env: str
    api_key_env: str = "OPENAI_API_KEY"
    quality: str = "medium"
    output_format: str = "png"
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    force_single: bool = Field(strict=True)
    prompt_max_chars: int = Field(gt=0, strict=True)
    timeout_s: float = Field(gt=0, allow_inf_nan=False)
    query_retry: int = Field(ge=0, strict=True)
    query_backoff_s: float = Field(gt=0, allow_inf_nan=False)
    output_dir: Path

    def access_key(self) -> str:
        """仅在真实生图时读取 Access Key。"""
        return _require_compact_credential(self.access_key_env, "Access Key ID")

    def secret_key(self) -> str:
        """仅在真实生图时读取 Secret Key。"""
        return _require_compact_credential(self.secret_key_env, "Secret Access Key")

    def api_key(self) -> str:
        """仅在 OpenAI 生图时读取 API Key。"""
        return _require_compact_credential(self.api_key_env, "API Key")


class StorageSettings(BaseModel):
    """项目数据的存储位置。"""

    sqlite_path: Path


class RuntimeSettings(BaseModel):
    """Graph 与 Batch 的安全上限。"""

    max_reworks: int = Field(default=1, ge=0, le=5, strict=True)
    batch_max_concurrency: int = Field(default=2, gt=0, le=16, strict=True)


class VideoSettings(BaseModel):
    """可选视频能力配置；默认关闭。"""

    enabled: bool = False
    provider: str = "mock"
    model: str = "mock-video-v1"
    timeout_s: float = Field(default=30, gt=0, allow_inf_nan=False)
    retry: int = Field(default=1, ge=0, le=3, strict=True)
    estimated_fen: int = Field(default=1, gt=0, strict=True)
    max_fen: int = Field(default=1, ge=0, strict=True)
    output_dir: Path = Path("data/videos")


class CommerceSettings(BaseModel):
    """Commerce 真实能力开关；外部 Source 与 Marketplace 仍保持 Mock。"""

    image_mode: Literal["mock", "real"] = "mock"
    data_mode: Literal["demo", "production"] = "production"
    text_mode: Literal["mock", "real"] = "mock"
    real_image_acceptance_max_fen: int = Field(default=30, ge=0, strict=True)
    marketplace: str = "Ozon Mock Marketplace"


class Settings(BaseSettings):
    """项目配置总入口；业务代码只通过 ``get_settings`` 获取配置。"""

    model_config = SettingsConfigDict(env_prefix="KANTOKU_", extra="ignore")

    app: AppSettings
    llm: LlmSettings
    image: ImageSettings
    budget: BudgetSettings
    storage: StorageSettings
    runtime: RuntimeSettings = RuntimeSettings()
    video: VideoSettings = VideoSettings()
    commerce: CommerceSettings = CommerceSettings()


def _require_env(name: str) -> str:
    """读取必需的环境变量；缺失时转换为项目配置异常。"""
    value = os.environ.get(name)
    if value is None:
        try:
            value = dotenv_values(ENV_PATH, encoding="utf-8", interpolate=False).get(name)
        except (OSError, UnicodeError):
            raise ConfigError("无法读取 .env 文件") from None
    if not value or not value.strip():
        raise ConfigError(
            f"环境变量 {name} 未设置",
            detail=(
                f'请在项目根目录的 .env 里配置，或先执行：$env:{name}="你的密钥"（PowerShell）'
            ),
        )
    return value


def _require_compact_credential(name: str, label: str) -> str:
    """读取签名凭据并拒绝复制时混入的空格或换行。"""
    value = _require_env(name)
    if any(character.isspace() for character in value):
        raise ConfigError(
            f"{label} 格式不合法",
            detail=f"{name} 含有空白字符；请只复制密钥值，不要复制字段名称",
        )
    return value


def _read_yaml() -> dict[str, Any]:
    """优先读取本地配置，不存在时回退到可提交的示例配置。"""
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    if not path.exists():
        raise ConfigError(
            "找不到配置文件",
            detail=f"期望路径：{CONFIG_PATH}；回退路径：{EXAMPLE_PATH}",
        )

    try:
        with path.open(encoding="utf-8") as file:
            raw = yaml.safe_load(file)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(
            "读取配置文件失败", detail=f"文件：{path}；类型：{type(exc).__name__}"
        ) from None

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件格式错误", detail=f"{path} 的顶层必须是键值映射")
    return raw


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取、校验并缓存整份项目配置。"""
    try:
        return Settings.model_validate(_read_yaml())
    except ValidationError as exc:
        locations = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['type']}"
            for item in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        raise ConfigError("配置内容不合法", detail=locations) from None
