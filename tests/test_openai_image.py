"""GPT-Image 适配器离线测试：单次提交、原子落盘与安全恢复。"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kantoku.config import ConfigError, ToolError
from kantoku.config.settings import ImageSettings
from kantoku.tools.openai_image import OpenAIImageProvider

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _settings(tmp_path: Path, **changes: object) -> ImageSettings:
    values: dict[str, object] = {
        "provider": "openai-gpt-image",
        "base_url": "https://api.openai.com/v1",
        "model": "test-gpt-image",
        "region": "unused",
        "service": "unused",
        "api_version": "unused",
        "submit_action": "unused",
        "query_action": "unused",
        "access_key_env": "UNUSED_AK",
        "secret_key_env": "UNUSED_SK",
        "api_key_env": "TEST_OPENAI_KEY",
        "quality": "medium",
        "output_format": "png",
        "width": 2560,
        "height": 1440,
        "force_single": True,
        "prompt_max_chars": 4000,
        "timeout_s": 60,
        "query_retry": 0,
        "query_backoff_s": 2,
        "output_dir": tmp_path / "images",
    }
    values.update(changes)
    return ImageSettings.model_validate(values)


def _client(image_bytes: bytes = _PNG) -> SimpleNamespace:
    images = SimpleNamespace(
        generate=MagicMock(
            return_value=SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(image_bytes).decode())]
            )
        )
    )
    models = SimpleNamespace(retrieve=MagicMock(return_value=SimpleNamespace(id="test-gpt-image")))
    return SimpleNamespace(images=images, models=models)


def test_submit_calls_image_api_once_and_query_only_reads_saved_file(tmp_path: Path) -> None:
    client = _client()
    traces = []
    provider = OpenAIImageProvider(
        settings=_settings(tmp_path), client=client, trace_writer=traces.append
    )

    job_id = provider.submit(
        prompt="自然纪实摄影，雨天街口的祖孙",
        shot_no=1,
        client_request_id="video-1-shot-1-v1",
        reference_urls=(),
        seed=None,
    )
    result = provider.query(job_id)

    assert result.status == "succeeded"
    assert result.path is not None and result.path.read_bytes() == _PNG
    client.images.generate.assert_called_once_with(
        model="test-gpt-image",
        prompt="自然纪实摄影，雨天街口的祖孙",
        n=1,
        size="2560x1440",
        quality="medium",
        output_format="png",
        response_format="b64_json",
        extra_headers={"Idempotency-Key": "video-1-shot-1-v1"},
    )
    assert client.images.generate.call_count == 1
    assert traces[0].ok is True
    assert traces[0].usage_reported is False


def test_invalid_request_never_reaches_paid_endpoint(tmp_path: Path) -> None:
    client = _client()
    provider = OpenAIImageProvider(settings=_settings(tmp_path), client=client)

    with pytest.raises(ToolError, match="参考图"):
        provider.submit(
            prompt="test",
            shot_no=1,
            client_request_id="request-1",
            reference_urls=("https://example.com/ref.png",),
            seed=None,
        )
    with pytest.raises(ToolError, match="seed"):
        provider.validate_request(prompt="test", shot_no=1, reference_urls=(), seed=7)

    client.images.generate.assert_not_called()


def test_malformed_image_is_unknown_safe_and_does_not_expose_prompt(tmp_path: Path) -> None:
    client = _client(b"not-png")
    traces = []
    provider = OpenAIImageProvider(
        settings=_settings(tmp_path), client=client, trace_writer=traces.append
    )

    with pytest.raises(ToolError, match="有效 PNG") as caught:
        provider.submit(
            prompt="PRIVATE-PROMPT",
            shot_no=2,
            client_request_id="request-2",
            reference_urls=(),
            seed=None,
        )

    assert "PRIVATE-PROMPT" not in str(caught.value)
    assert traces[0].ok is False
    assert traces[0].error == "ToolError"


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 2559},
        {"height": 700},
        {"quality": "max"},
        {"output_format": "jpeg"},
        {"force_single": False},
        {"base_url": "not-a-url"},
    ],
)
def test_invalid_provider_configuration_stops_before_network(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(ConfigError):
        OpenAIImageProvider(settings=_settings(tmp_path, **changes), client=_client())
