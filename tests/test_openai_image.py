"""GPT-Image 适配器离线测试：单次提交、原子落盘与安全恢复。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import pytest
from openai import OpenAI

from kantoku.config import ConfigError, ToolError
from kantoku.config import settings as settings_module
from kantoku.config.settings import ImageSettings
from kantoku.tools.openai_image import AlibabaQwenImageProvider, OpenAIImageProvider

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_QWEN_BASE_URL = "https://ws-043re2e5ss4pje8w.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


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


def test_qwen_url_result_is_recoverable_without_resubmission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    client.images.generate.return_value = SimpleNamespace(
        data=[SimpleNamespace(url="https://dashscope-result-sz.oss-cn-shenzhen.aliyuncs.com/test.png")]
    )
    settings = _settings(
        tmp_path, provider="alibaba-qwen-image", model="qwen-image-3.0",
        base_url=_QWEN_BASE_URL,
        api_key_env="DASHSCOPE_API_KEY",
    )
    provider = AlibabaQwenImageProvider(settings=settings, client=client)
    job_id = provider.submit(
        prompt="一张方形卡通头像", shot_no=1,
        client_request_id="qwen-once-1", reference_urls=(), seed=None,
    )
    assert provider._pending_url_path(job_id).is_file()
    client.images.generate.assert_called_once_with(
        model="qwen-image-3.0", prompt="一张方形卡通头像", n=1,
        size="2560x1440", extra_headers={"Idempotency-Key": "qwen-once-1"},
    )
    downloads = iter([OSError("temporary network failure"), _PNG])

    def download(_url: str) -> bytes:
        result = next(downloads)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(OpenAIImageProvider, "_download_url", lambda self, url: download(url))
    with pytest.raises(ToolError, match="下载未完成"):
        provider.query(job_id)
    assert provider._pending_url_path(job_id).is_file()

    restarted = AlibabaQwenImageProvider(settings=settings, client=client)
    result = restarted.query(job_id)
    assert result.status == "succeeded"
    assert result.path is not None and result.path.read_bytes() == _PNG
    assert not restarted._pending_url_path(job_id).exists()
    assert client.images.generate.call_count == 1


def test_qwen_edit_sends_reference_through_generations_extra_body(tmp_path: Path) -> None:
    client = _client()
    provider = AlibabaQwenImageProvider(
        settings=_settings(
            tmp_path, provider="alibaba-qwen-image", model="qwen-image-3.0",
            base_url=_QWEN_BASE_URL, api_key_env="DASHSCOPE_API_KEY",
        ),
        client=client,
    )
    reference = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    provider.submit(
        prompt="保留背景，把熊大换成熊二", shot_no=1,
        client_request_id="qwen-edit-once", reference_urls=(reference,), seed=None,
    )
    client.images.generate.assert_called_once_with(
        model="qwen-image-3.0", prompt="保留背景，把熊大换成熊二", n=1,
        size="2560x1440", extra_headers={"Idempotency-Key": "qwen-edit-once"},
        extra_body={"image": reference},
    )


def test_qwen_provider_rejects_wrong_model_or_auth_configuration(tmp_path: Path) -> None:
    client = _client()
    base = {
        "provider": "alibaba-qwen-image",
        "model": "qwen-image-3.0",
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": _QWEN_BASE_URL,
    }
    for change in (
        {"model": "qwen-image-2.0"},
        {"api_key_env": "VOLC_ACCESSKEY"},
        {"base_url": "https://visual.volcengineapi.com"},
    ):
        with pytest.raises(ConfigError):
            AlibabaQwenImageProvider(
                settings=_settings(tmp_path, **{**base, **change}), client=client,
            )
    client.images.generate.assert_not_called()


def test_qwen_missing_key_does_not_block_startup_or_reach_paid_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(settings_module, "ENV_PATH", tmp_path / ".env")
    settings = _settings(
        tmp_path, provider="alibaba-qwen-image", model="qwen-image-3.0",
        base_url=_QWEN_BASE_URL,
        api_key_env="DASHSCOPE_API_KEY",
    )

    provider = AlibabaQwenImageProvider(settings=settings)
    with pytest.raises(ConfigError, match="DASHSCOPE_API_KEY"):
        provider.validate_request(
            prompt="方形头像", shot_no=1, reference_urls=(), seed=None,
        )
    assert not settings.output_dir.exists()


@pytest.mark.parametrize("base_url", ["", "https://example.com/v1", "http://example.com/compatible-mode/v1"])
def test_qwen_rejects_empty_or_non_compatible_url_before_paid_submit(
    tmp_path: Path, base_url: str,
) -> None:
    client = _client()
    with pytest.raises(ConfigError):
        provider = AlibabaQwenImageProvider(
            settings=_settings(
                tmp_path, provider="alibaba-qwen-image", model="qwen-image-3.0",
                base_url=base_url, api_key_env="DASHSCOPE_API_KEY",
            ),
            client=client,
        )
        provider.submit(
            prompt="方形头像", shot_no=1, client_request_id="invalid-endpoint",
            reference_urls=(), seed=None,
        )
    client.images.generate.assert_not_called()


def test_qwen_b64_result_uses_images_generations_endpoint(tmp_path: Path) -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json={"created": 1, "data": [{"b64_json": base64.b64encode(_PNG).decode()}]},
        )

    http_client = httpx2.Client(transport=httpx2.MockTransport(handle))
    client = OpenAI(base_url=_QWEN_BASE_URL, api_key="test-key", http_client=http_client)
    provider = AlibabaQwenImageProvider(
        settings=_settings(
            tmp_path, provider="alibaba-qwen-image", model="qwen-image-3.0",
            base_url=_QWEN_BASE_URL, api_key_env="DASHSCOPE_API_KEY",
        ),
        client=client,
    )

    job_id = provider.submit(
        prompt="方形头像", shot_no=1, client_request_id="qwen-b64-once",
        reference_urls=(), seed=None,
    )
    result = provider.query(job_id)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == f"{_QWEN_BASE_URL}/images/generations"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert json.loads(requests[0].content)["model"] == "qwen-image-3.0"
    assert result.path is not None and result.path.read_bytes() == _PNG


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
