from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError
from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageParam

from kantoku.config import ConfigError, LLMError
from kantoku.config import settings as settings_module
from kantoku.config.settings import Settings
from kantoku.core import llm as llm_module


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "app": {"name": "test", "log_level": "DEBUG"},
            "llm": {
                "base_url": "https://primary.example/v1",
                "model_chat": "primary-model",
                "api_key_env": "TEST_CHAT_KEY",
                "chat_extra_body": {"thinking": {"type": "disabled"}},
                "vision_base_url": "https://vision.example/v1",
                "model_vision": "vision-model",
                "vision_api_key_env": "TEST_VISION_KEY",
                "vision_extra_body": {"enable_thinking": False},
                "vision_max_tokens": 512,
                "vision_max_image_bytes": 10485760,
                "fallback_base_url": "https://fallback.example/v1",
                "fallback_model_chat": "fallback-model",
                "fallback_api_key_env": "TEST_FALLBACK_KEY",
                "fallback_extra_body": {"provider": "fallback"},
                "temperature": 0.7,
                "structured_temperature": 0.2,
                "max_tokens": 2048,
                "timeout_s": 0.001,
                "retry": 2,
            },
            "image": {
                "provider": "fake-provider",
                "base_url": "https://image.example/v1",
                "model": "image-model",
                "region": "test-region",
                "service": "image-service",
                "api_version": "2026-01-01",
                "submit_action": "SubmitTask",
                "query_action": "GetResult",
                "access_key_env": "TEST_IMAGE_ACCESS_KEY",
                "secret_key_env": "TEST_IMAGE_SECRET_KEY",
                "width": 2560,
                "height": 1440,
                "force_single": True,
                "prompt_max_chars": 800,
                "timeout_s": 60,
                "query_retry": 2,
                "query_backoff_s": 2,
                "output_dir": "data/images",
            },
            "budget": {
                "accounting_utc_offset_hours": 8,
                "image_credit_cny": "0.10",
                "image_estimated_credits_per_call": 3,
                "image_daily_cny": "5.0",
                "image_project_cny": None,
                "image_episode_cny": None,
                "image_shot_cny": None,
                "image_max_concurrency": 1,
                "token_daily_limit": 200000,
            },
            "storage": {"sqlite_path": "data/test.db"},
        }
    )


def _response(content: str = "你好") -> SimpleNamespace:
    message = ChatCompletionMessage(role="assistant", content=content)
    usage = SimpleNamespace(prompt_tokens=12, completion_tokens=7)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


@pytest.fixture(autouse=True)
def llm_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MagicMock:
    monkeypatch.setattr(settings_module, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("TEST_CHAT_KEY", "fake-primary-key")
    monkeypatch.setenv("TEST_VISION_KEY", "fake-vision-key")
    monkeypatch.setenv("TEST_FALLBACK_KEY", "fake-fallback-key")
    monkeypatch.setattr(llm_module, "get_settings", _settings)
    trace_writer = MagicMock()
    monkeypatch.setattr(llm_module, "write_trace", trace_writer)
    return trace_writer


def test_chat_builds_client_and_returns_complete_message(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
) -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = _response()
    openai_factory = MagicMock(return_value=client)
    monkeypatch.setattr(llm_module, "OpenAI", openai_factory)
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": "你好"}]

    message = llm_module.chat(messages)

    assert message.content == "你好"
    openai_factory.assert_called_once_with(
        base_url="https://primary.example/v1",
        api_key="fake-primary-key",
        timeout=0.001,
        max_retries=0,
    )
    request = client.chat.completions.create.call_args.kwargs
    assert request["model"] == "primary-model"
    assert request["messages"] == messages
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    trace = llm_environment.call_args.args[0]
    assert trace.ok is True
    assert trace.in_tokens == 12
    assert trace.out_tokens == 7
    client.close.assert_called_once_with()


def test_vision_chat_uses_isolated_model_endpoint_and_output_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = _response('{"watermark": false}')
    openai_factory = MagicMock(return_value=client)
    monkeypatch.setattr(llm_module, "OpenAI", openai_factory)
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": "看图"}]

    message = llm_module.vision_chat(messages, response_format={"type": "json_object"})

    assert message.content == '{"watermark": false}'
    openai_factory.assert_called_once_with(
        base_url="https://vision.example/v1",
        api_key="fake-vision-key",
        timeout=0.001,
        max_retries=0,
    )
    request = client.chat.completions.create.call_args.kwargs
    assert request["model"] == "vision-model"
    assert request["max_tokens"] == 512
    assert request["response_format"] == {"type": "json_object"}
    assert request["extra_body"] == {"enable_thinking": False}


def test_reasoning_model_can_omit_temperature_and_use_completion_token_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    settings.llm.chat_use_temperature = False
    settings.llm.chat_max_tokens_parameter = "max_completion_tokens"
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings)
    client = MagicMock()
    client.chat.completions.create.return_value = _response()
    monkeypatch.setattr(llm_module, "OpenAI", MagicMock(return_value=client))

    llm_module.chat([{"role": "user", "content": "测试推理模型参数"}])

    request = client.chat.completions.create.call_args.kwargs
    assert "temperature" not in request
    assert "max_tokens" not in request
    assert request["max_completion_tokens"] == 2048


def test_chat_retries_connection_error_with_increasing_delay(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
) -> None:
    client = MagicMock()
    connection_error = APIConnectionError(request=MagicMock())
    client.chat.completions.create.side_effect = [
        connection_error,
        connection_error,
        _response(),
    ]
    monkeypatch.setattr(llm_module, "OpenAI", MagicMock(return_value=client))
    sleep_mock = MagicMock()
    monkeypatch.setattr(llm_module, "sleep", sleep_mock)

    message = llm_module.chat([{"role": "user", "content": "重试"}])

    assert message.content == "你好"
    assert client.chat.completions.create.call_count == 3
    assert sleep_mock.call_args_list == [call(2.0), call(4.0)]
    records = [item.args[0] for item in llm_environment.call_args_list]
    assert [record.ok for record in records] == [False, False, True]
    assert [record.usage_reported for record in records] == [False, False, True]
    assert all(record.kind == "llm.chat.attempt" for record in records)


def test_chat_uses_fallback_for_missing_primary_model(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
) -> None:
    primary_client = MagicMock()
    response = MagicMock(status_code=404, request=MagicMock(), headers={})
    primary_client.chat.completions.create.side_effect = APIStatusError(
        "model not found",
        response=response,
        body=None,
    )
    fallback_client = MagicMock()
    fallback_client.chat.completions.create.return_value = _response("备用成功")
    openai_factory = MagicMock(side_effect=[primary_client, fallback_client])
    monkeypatch.setattr(llm_module, "OpenAI", openai_factory)

    message = llm_module.chat([{"role": "user", "content": "降级"}])

    assert message.content == "备用成功"
    assert openai_factory.call_count == 2
    fallback_request = fallback_client.chat.completions.create.call_args.kwargs
    assert fallback_request["model"] == "fallback-model"
    assert fallback_request["extra_body"] == {"provider": "fallback"}
    assert [item.args[0].ok for item in llm_environment.call_args_list] == [False, True]


def test_chat_wraps_authentication_error_without_retry_or_fallback(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
) -> None:
    client = MagicMock()
    response = MagicMock(status_code=401, request=MagicMock(), headers={})
    authentication_error = AuthenticationError(
        "invalid key",
        response=response,
        body=None,
    )
    client.chat.completions.create.side_effect = authentication_error
    openai_factory = MagicMock(return_value=client)
    monkeypatch.setattr(llm_module, "OpenAI", openai_factory)
    sleep_mock = MagicMock()
    monkeypatch.setattr(llm_module, "sleep", sleep_mock)

    with pytest.raises(LLMError, match="主模型调用失败") as caught:
        llm_module.chat([{"role": "user", "content": "鉴权"}])

    assert caught.value.__cause__ is authentication_error
    assert openai_factory.call_count == 1
    trace = llm_environment.call_args.args[0]
    assert trace.ok is False
    assert trace.error == "AuthenticationError(status=401)"
    sleep_mock.assert_not_called()


def test_chat_keeps_missing_key_as_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_CHAT_KEY")
    openai_factory = MagicMock()
    monkeypatch.setattr(llm_module, "OpenAI", openai_factory)

    with pytest.raises(ConfigError, match="TEST_CHAT_KEY"):
        llm_module.chat([{"role": "user", "content": "缺密钥"}])

    openai_factory.assert_not_called()


@pytest.mark.parametrize("empty_choices", [True, False])
def test_empty_response_is_failed_trace_not_success(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
    empty_choices: bool,
) -> None:
    response = _response("")
    if empty_choices:
        response.choices = []
    client = MagicMock()
    client.chat.completions.create.return_value = response
    monkeypatch.setattr(llm_module, "OpenAI", MagicMock(return_value=client))
    with pytest.raises(LLMError):
        llm_module.chat([{"role": "user", "content": "测试"}])
    assert llm_environment.call_count == 1
    assert llm_environment.call_args.args[0].ok is False
    assert llm_environment.call_args.args[0].in_tokens == 12
    client.close.assert_called_once()


def test_close_and_trace_failure_cannot_discard_success(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
) -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = _response()
    client.close.side_effect = RuntimeError("private-close-detail")
    llm_environment.side_effect = RuntimeError("private-trace-detail")
    monkeypatch.setattr(llm_module, "OpenAI", MagicMock(return_value=client))
    assert llm_module.chat([{"role": "user", "content": "测试"}]).content == "你好"
    assert client.chat.completions.create.call_count == 1


def test_cli_llm_and_sqlite_integration_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kantoku.config import logging_setup
    from kantoku.core import tracing
    from kantoku.shells import cli

    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(logging_setup, "get_settings", _settings)
    monkeypatch.setattr(tracing, "_database_path", lambda: tmp_path / "integration.db")
    monkeypatch.setattr(llm_module, "write_trace", tracing.write_trace)
    response = MagicMock(status_code=401, request=MagicMock(), headers={})
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        *[_response(f"模拟第 {i} 轮") for i in range(1, 6)],
        AuthenticationError("private-provider-detail", response=response, body=None),
    ]
    monkeypatch.setattr(llm_module, "OpenAI", MagicMock(return_value=client))
    inputs = iter(["一", "二", "三", "四", "五", "失败", "/traces", "exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    try:
        assert cli.run(["--chat"]) == 0
    finally:
        logging_setup.logger.remove()
    records = tracing.recent_traces(10)
    assert len(records) == 6
    assert sum(record.ok for record in records) == 5
    assert records[0].ok is False
    captured = capsys.readouterr()
    assert "模拟第 5 轮" in captured.out
    assert "[trace]" in captured.out
    assert "成本=未知" in captured.out
    assert "private-provider-detail" not in captured.err


@pytest.mark.parametrize(
    "usage,reported",
    [
        (None, False),
        (SimpleNamespace(prompt_tokens=0, completion_tokens=0), True),
        (SimpleNamespace(prompt_tokens=-1, completion_tokens=3), False),
        (SimpleNamespace(prompt_tokens=True, completion_tokens=3), False),
    ],
)
def test_unknown_usage_is_distinct_from_reported_zero(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
    usage: SimpleNamespace | None,
    reported: bool,
) -> None:
    response = _response()
    response.usage = usage
    client = MagicMock()
    client.chat.completions.create.return_value = response
    monkeypatch.setattr(llm_module, "OpenAI", MagicMock(return_value=client))
    assert llm_module.chat([{"role": "user", "content": "测试"}]).content == "你好"
    trace = llm_environment.call_args.args[0]
    assert trace.ok is True
    assert trace.usage_reported is reported
    assert trace.in_tokens == trace.out_tokens == 0


def test_timeout_exhaustion_records_every_attempt_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
) -> None:
    client = MagicMock()
    client.chat.completions.create.side_effect = APITimeoutError(request=MagicMock())
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(llm_module, "OpenAI", factory)
    monkeypatch.setattr(llm_module, "sleep", MagicMock())
    with pytest.raises(LLMError, match="主模型和备用模型调用均失败"):
        llm_module.chat([{"role": "user", "content": "测试"}])
    assert factory.call_count == 2
    assert client.chat.completions.create.call_count == 6
    records = [item.args[0] for item in llm_environment.call_args_list]
    assert len(records) == 6
    assert all(not record.ok and record.usage_reported is False for record in records)
    assert all(record.error == "APITimeoutError" for record in records)


def test_interruption_is_recorded_and_never_retried(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
) -> None:
    client = MagicMock()
    client.chat.completions.create.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(llm_module, "OpenAI", MagicMock(return_value=client))
    with pytest.raises(KeyboardInterrupt):
        llm_module.chat([{"role": "user", "content": "测试"}])
    assert client.chat.completions.create.call_count == 1
    assert llm_environment.call_args.args[0].error == "KeyboardInterrupt"
    assert llm_environment.call_args.args[0].usage_reported is False
    client.close.assert_called_once()


def test_attempt_latency_excludes_retry_sleep(
    monkeypatch: pytest.MonkeyPatch,
    llm_environment: MagicMock,
) -> None:
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        APIConnectionError(request=MagicMock()),
        _response(),
    ]
    monkeypatch.setattr(llm_module, "OpenAI", MagicMock(return_value=client))
    monkeypatch.setattr(llm_module, "perf_counter", MagicMock(side_effect=[0, 0.1, 2.1, 2.3]))
    monkeypatch.setattr(llm_module, "sleep", MagicMock())
    llm_module.chat([{"role": "user", "content": "测试"}])
    assert [item.args[0].latency_ms for item in llm_environment.call_args_list] == [100, 200]
