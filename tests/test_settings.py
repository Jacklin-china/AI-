from __future__ import annotations

import traceback
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from kantoku.config import ConfigError
from kantoku.config import settings as settings_module
from kantoku.config.settings import AppSettings, ImageSettings, LlmSettings, get_settings

VALID_YAML = """
app:
  name: kantoku-agent
  log_level: INFO
llm:
  base_url: https://primary.example/v1
  model_chat: primary-model
  api_key_env: TEST_CHAT_KEY
  chat_extra_body:
    thinking:
      type: disabled
  vision_base_url: https://vision.example/v1
  model_vision: vision-model
  vision_api_key_env: TEST_VISION_KEY
  vision_extra_body:
    enable_thinking: false
  vision_max_tokens: 512
  vision_max_image_bytes: 10485760
  fallback_base_url: https://fallback.example/v1
  fallback_model_chat: fallback-model
  fallback_api_key_env: TEST_FALLBACK_KEY
  fallback_extra_body: {}
  temperature: 0.7
  structured_temperature: 0.2
  max_tokens: 2048
  timeout_s: 0.001
  retry: 2
image:
  provider: fake-provider
  base_url: https://image.example/v1
  model: image-model
  region: test-region
  service: image-service
  api_version: "2026-01-01"
  submit_action: SubmitTask
  query_action: GetResult
  access_key_env: TEST_IMAGE_ACCESS_KEY
  secret_key_env: TEST_IMAGE_SECRET_KEY
  api_key_env: TEST_OPENAI_KEY
  quality: medium
  output_format: png
  width: 2560
  height: 1440
  force_single: true
  prompt_max_chars: 800
  timeout_s: 60
  query_retry: 2
  query_backoff_s: 2
  output_dir: data/images
budget:
  accounting_utc_offset_hours: 8
  image_credit_cny: 0.10
  image_estimated_credits_per_call: 3
  image_daily_cny: 5.0
  image_project_cny: null
  image_episode_cny: 20.5
  image_shot_cny: 1.5
  image_max_concurrency: 1
  token_daily_limit: 200000
storage:
  sqlite_path: data/test.db
"""


@pytest.fixture(autouse=True)
def clear_settings_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings_module, "ENV_PATH", tmp_path / ".env")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_required_fields_have_no_python_defaults() -> None:
    with pytest.raises(ValidationError):
        AppSettings()
    assert LlmSettings.model_fields["base_url"].is_required()
    assert LlmSettings.model_fields["timeout_s"].annotation is float


def test_qwen_image_settings_need_no_volcengine_signing_fields(tmp_path: Path) -> None:
    settings = ImageSettings.model_validate({
        "provider": "alibaba-qwen-image",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-image-3.0",
        "api_key_env": "DASHSCOPE_API_KEY",
        "width": 2560, "height": 1440, "force_single": True,
        "prompt_max_chars": 4000, "timeout_s": 600,
        "query_retry": 2, "query_backoff_s": 2,
        "output_dir": tmp_path,
    })
    assert settings.access_key_env == ""
    assert settings.secret_key_env == ""
    assert settings.api_key_env == "DASHSCOPE_API_KEY"


def test_require_env_returns_value_and_rejects_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CHAT_KEY", "fake-key")
    assert settings_module._require_env("TEST_CHAT_KEY") == "fake-key"

    monkeypatch.delenv("TEST_MISSING_KEY", raising=False)
    with pytest.raises(ConfigError, match="TEST_MISSING_KEY"):
        settings_module._require_env("TEST_MISSING_KEY")


def test_read_yaml_falls_back_to_example(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_local = tmp_path / "settings.yaml"
    example = tmp_path / "settings.example.yaml"
    example.write_text("app:\n  name: fallback\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "CONFIG_PATH", missing_local)
    monkeypatch.setattr(settings_module, "EXAMPLE_PATH", example)

    assert settings_module._read_yaml()["app"]["name"] == "fallback"


def test_get_settings_validates_all_sections_and_caches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr(settings_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(settings_module, "EXAMPLE_PATH", tmp_path / "missing-example.yaml")

    first = get_settings()
    second = get_settings()

    assert first is second
    assert first.llm.model_chat == "primary-model"
    assert first.llm.chat_extra_body == {"thinking": {"type": "disabled"}}
    assert first.llm.vision_extra_body == {"enable_thinking": False}
    assert first.llm.timeout_s == 0.001
    assert first.image.model == "image-model"
    assert first.image.force_single is True
    assert first.image.api_key_env == "TEST_OPENAI_KEY"
    assert first.image.quality == "medium"
    assert first.image.output_dir == Path("data/images")
    assert first.budget.image_daily_cny == Decimal("5.0")
    assert first.budget.accounting_utc_offset_hours == 8
    assert first.budget.image_credit_cny == Decimal("0.10")
    assert first.budget.image_estimated_credits_per_call == 3
    assert first.storage.sqlite_path == Path("data/test.db")


def test_get_settings_converts_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("app:\n  name: incomplete\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(settings_module, "EXAMPLE_PATH", tmp_path / "missing-example.yaml")

    with pytest.raises(ConfigError, match="配置内容不合法"):
        get_settings()


def test_dotenv_is_read_without_mutating_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_FILE_KEY", raising=False)
    (tmp_path / ".env").write_text("TEST_FILE_KEY=fake-from-file\n", encoding="utf-8")
    assert settings_module._require_env("TEST_FILE_KEY") == "fake-from-file"
    assert "TEST_FILE_KEY" not in settings_module.os.environ
    monkeypatch.setenv("TEST_FILE_KEY", "fake-from-process")
    assert settings_module._require_env("TEST_FILE_KEY") == "fake-from-process"
    monkeypatch.setenv("TEST_FILE_KEY", "  ")
    with pytest.raises(ConfigError):
        settings_module._require_env("TEST_FILE_KEY")


def test_image_signing_credentials_reject_internal_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(VALID_YAML, encoding="utf-8")
    monkeypatch.setattr(settings_module, "CONFIG_PATH", config_path)
    settings = get_settings()

    monkeypatch.setenv("TEST_IMAGE_ACCESS_KEY", "A K-invalid")
    monkeypatch.setenv("TEST_IMAGE_SECRET_KEY", "valid-secret")
    with pytest.raises(ConfigError, match="Access Key ID 格式不合法"):
        settings.image.access_key()

    monkeypatch.setenv("TEST_IMAGE_ACCESS_KEY", "valid-access")
    monkeypatch.setenv("TEST_IMAGE_SECRET_KEY", "secret with-space")
    with pytest.raises(ConfigError, match="Secret Access Key 格式不合法"):
        settings.image.secret_key()


@pytest.mark.parametrize(
    "old,new",
    [
        ("timeout_s: 0.001", "timeout_s: 0"),
        ("timeout_s: 0.001", "timeout_s: .inf"),
        ("retry: 2", "retry: -1"),
        ("retry: 2", "retry: true"),
        ("image_daily_cny: 5.0", "image_daily_cny: -1"),
        ("accounting_utc_offset_hours: 8", "accounting_utc_offset_hours: 15"),
        ("accounting_utc_offset_hours: 8", "accounting_utc_offset_hours: true"),
        ("image_credit_cny: 0.10", "image_credit_cny: 0"),
        (
            "image_estimated_credits_per_call: 3",
            "image_estimated_credits_per_call: true",
        ),
        ("max_tokens: 2048", "max_tokens: 0"),
        ("width: 2560", "width: true"),
        ("force_single: true", "force_single: 1"),
        ("query_retry: 2", "query_retry: -1"),
        ("prompt_max_chars: 800", "prompt_max_chars: 0"),
    ],
)
def test_invalid_operating_limits_fail_at_startup(
    old: str,
    new: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(VALID_YAML.replace(old, new), encoding="utf-8")
    monkeypatch.setattr(settings_module, "CONFIG_PATH", path)
    with pytest.raises(ConfigError):
        get_settings()


@pytest.mark.parametrize(
    "raw",
    [
        "app: [DO-NOT-LOG-THIS,",
        VALID_YAML.replace("timeout_s: 0.001", "timeout_s: DO-NOT-LOG-THIS"),
        "[]",
        "false",
    ],
)
def test_config_failure_does_not_echo_input(
    raw: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(settings_module, "CONFIG_PATH", path)
    with pytest.raises(ConfigError) as caught:
        get_settings()
    assert "DO-NOT-LOG-THIS" not in "".join(traceback.format_exception(caught.value))
