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
_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"


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


def test_qwen_provider_rejects_invalid_configuration(tmp_path: Path) -> None:
    client = _client()
    base = {
        "provider": "alibaba-qwen-image",
        "model": "qwen-image-3.0",
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": _QWEN_BASE_URL,
    }
    for change in (
        {"model": "   "},
        {"provider": "openai-gpt-image"},
        {"api_key_env": ""},
        {"base_url": "https://example.com:invalid/v1"},
        {"base_url": "https://invalid host/v1"},
        {"base_url": "https://[invalid-ipv6/v1"},
        {"base_url": "https://key:secret@example.com/v1"},
        {"base_url": "https://example.com/v1?api_key=secret"},
        {"base_url": "https://example.com/v1#fragment"},
    ):
        with pytest.raises(ConfigError):
            AlibabaQwenImageProvider(
                settings=_settings(tmp_path, **{**base, **change}), client=client,
            )
    client.images.generate.assert_not_called()


def test_qwen_missing_key_is_rejected_without_reaching_paid_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(settings_module, "ENV_PATH", tmp_path / ".env")
    settings = _settings(
        tmp_path, provider="alibaba-qwen-image", model="qwen-image-3.0",
        base_url=_QWEN_BASE_URL,
        api_key_env="DASHSCOPE_API_KEY",
    )

    with pytest.raises(ConfigError, match="DASHSCOPE_API_KEY"):
        AlibabaQwenImageProvider(settings=settings)
    assert not settings.output_dir.exists()


@pytest.mark.parametrize("base_url", ["", "not-a-url", "http://example.com/v1"])
def test_qwen_rejects_invalid_url_before_paid_submit(
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


@pytest.mark.parametrize("model", [
    "qwen-image-3.0", "qwen-image-2.0-pro", "qwen-image-2.1-pro", "future-model-id",
])
@pytest.mark.parametrize("protocol", ["openai-compatible", "dashscope-multimodal"])
@pytest.mark.parametrize("use_reference", [False, True])
def test_qwen_model_is_configured_and_protocol_controls_wire_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str, protocol: str,
    use_reference: bool,
) -> None:
    requests: list[httpx2.Request] = []
    result_url = "https://dashscope-result-sh.oss-cn-shanghai.aliyuncs.com/result.png"

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if protocol == "dashscope-multimodal":
            payload = {"output": {"choices": [
                {"message": {"content": [{"image": result_url}]}}
            ]}, "request_id": "test-request-id"}
        else:
            payload = {"data": [{"url": result_url}]}
        return httpx2.Response(200, json=payload)

    base_url = _DASHSCOPE_BASE_URL if protocol == "dashscope-multimodal" else _QWEN_BASE_URL
    client = OpenAI(
        base_url=base_url, api_key="test-key", max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
    )
    settings = _settings(
        tmp_path, provider="alibaba-qwen-image", protocol=protocol, model=model,
        base_url=base_url, api_key_env="CUSTOM_IMAGE_KEY",
    )
    provider = AlibabaQwenImageProvider(settings=settings, client=client)
    references = ("https://example.com/reference.png",) if use_reference else ()
    try:
        job_id = provider.submit(
            prompt="保留人物和环境关系", shot_no=1,
            client_request_id="protocol-single-submit", reference_urls=references, seed=None,
        )
        assert provider._pending_url_path(job_id).is_file()
        assert len(requests) == 1
        request = requests[0]
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.headers["Idempotency-Key"] == "protocol-single-submit"
        payload = json.loads(request.content)
        assert payload["model"] == model
        if protocol == "dashscope-multimodal":
            assert str(request.url) == (
                f"{base_url}/services/aigc/multimodal-generation/generation"
            )
            assert payload["parameters"] == {"n": 1, "size": "2560*1440"}
            assert payload["input"]["messages"] == [{
                "role": "user", "content": [
                    *[{"image": url} for url in references],
                    {"text": "保留人物和环境关系"},
                ],
            }]
        else:
            assert str(request.url) == f"{base_url}/images/generations"
            assert payload["n"] == 1 and payload["size"] == "2560x1440"
            assert payload.get("image") == (references[0] if references else None)
        monkeypatch.setattr(OpenAIImageProvider, "_download_url", lambda self, url: _PNG)
        restarted = AlibabaQwenImageProvider(settings=settings, client=client)
        result = restarted.query(job_id)
        assert result.status == "succeeded"
        assert result.path is not None and result.path.read_bytes() == _PNG
        assert len(requests) == 1  # Recovery never submits again, for either protocol.
    finally:
        client.close()


@pytest.mark.parametrize("response", [
    {"code": "InvalidParameter", "message": "PRIVATE-DETAIL"},
    {"output": {"choices": []}},
    {"output": {"choices": [{"message": {"content": [
        {"image": "https://example.com/one.png"}, {"image": "https://example.com/two.png"},
    ]}}]}},
    {"output": {"choices": [{"message": {"content": [{"text": "done"}]}}]}},
])
def test_dashscope_invalid_result_never_falls_back_or_reports_success(
    tmp_path: Path, response: dict,
) -> None:
    client = _client()
    client.post = MagicMock(return_value=response)
    traces = []
    provider = AlibabaQwenImageProvider(
        settings=_settings(
            tmp_path, provider="alibaba-qwen-image", protocol="dashscope-multimodal",
            model="qwen-image-2.0-pro", base_url=_DASHSCOPE_BASE_URL,
        ), client=client, trace_writer=traces.append,
    )
    with pytest.raises(ToolError, match="人工对账") as caught:
        provider.submit(
            prompt="PRIVATE-PROMPT", shot_no=1,
            client_request_id="invalid-response", reference_urls=(), seed=None,
        )
    client.post.assert_called_once()
    client.images.generate.assert_not_called()
    assert not provider.output_dir.exists()
    assert traces[0].ok is False and traces[0].model == "qwen-image-2.0-pro"
    assert "PRIVATE" not in str(caught.value)


def test_protocol_switch_changes_identity_but_preserves_legacy_openai_identity(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path, provider="alibaba-qwen-image", model="qwen-image-2.0-pro",
        base_url=_DASHSCOPE_BASE_URL,
    )
    legacy = AlibabaQwenImageProvider(settings=settings, client=_client()).generation_identity()
    explicit = AlibabaQwenImageProvider(
        settings=settings.model_copy(update={"protocol": "openai-compatible"}), client=_client(),
    ).generation_identity()
    native = AlibabaQwenImageProvider(
        settings=settings.model_copy(update={"protocol": "dashscope-multimodal"}), client=_client(),
    ).generation_identity()
    assert explicit == legacy
    assert native == {**legacy, "protocol": "dashscope-multimodal"}


def test_qwen_custom_key_name_and_custom_compatible_endpoint_are_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_IMAGE_KEY", "offline-configured-key")
    provider = AlibabaQwenImageProvider(settings=_settings(
        tmp_path, provider="alibaba-qwen-image", model="future-model-id",
        api_key_env="CUSTOM_IMAGE_KEY", base_url="https://image.example/v1",
    ))
    assert provider.model_id == "future-model-id"
    assert provider._client is None  # Configuration validation does not use the network.


def test_real_qwen_client_uses_configured_model_key_and_disables_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_IMAGE_KEY", "offline-configured-key")
    factory = MagicMock(return_value=_client())
    monkeypatch.setattr("kantoku.tools.openai_image.OpenAI", factory)
    provider = AlibabaQwenImageProvider(settings=_settings(
        tmp_path, provider="alibaba-qwen-image", protocol="dashscope-multimodal",
        model="qwen-image-2.0-pro", base_url=_DASHSCOPE_BASE_URL,
        api_key_env="CUSTOM_IMAGE_KEY",
    ))
    assert provider._client_for_request() is factory.return_value
    factory.assert_called_once_with(
        base_url=_DASHSCOPE_BASE_URL, api_key="offline-configured-key",
        timeout=60, max_retries=0,
    )
    assert provider.model_id == "qwen-image-2.0-pro"


def test_unsupported_protocol_is_rejected_before_network(tmp_path: Path) -> None:
    client = _client()
    with pytest.raises(ConfigError, match="协议不受支持"):
        AlibabaQwenImageProvider(
            settings=_settings(tmp_path, provider="alibaba-qwen-image").model_copy(
                update={"protocol": "unknown-wire-protocol"},
            ), client=client,
        )
    client.images.generate.assert_not_called()


def test_dashscope_requires_its_configured_api_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="/api/v1"):
        AlibabaQwenImageProvider(settings=_settings(
            tmp_path, provider="alibaba-qwen-image", protocol="dashscope-multimodal",
            base_url=_QWEN_BASE_URL,
        ), client=_client())


@pytest.mark.parametrize("protocol", ["openai-compatible", "dashscope-multimodal"])
def test_protocol_error_never_retries_paid_submit_or_switches_protocol(
    tmp_path: Path, protocol: str,
) -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(503, json={"message": "PRIVATE-PROVIDER-DETAIL"})

    base_url = _DASHSCOPE_BASE_URL if protocol == "dashscope-multimodal" else _QWEN_BASE_URL
    client = OpenAI(
        base_url=base_url, api_key="test-key", max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
    )
    provider = AlibabaQwenImageProvider(
        settings=_settings(
            tmp_path, provider="alibaba-qwen-image", protocol=protocol,
            base_url=base_url, model="qwen-image-2.0-pro",
        ), client=client,
    )
    try:
        with pytest.raises(ToolError, match="不会自动重提") as caught:
            provider.submit(
                prompt="PRIVATE-PROMPT", shot_no=1, client_request_id="unknown-billing",
                reference_urls=(), seed=None,
            )
        assert len(requests) == 1
        assert not provider.output_dir.exists()
        assert "PRIVATE" not in str(caught.value)
    finally:
        client.close()


def test_qwen_http_failure_has_traceback_diagnostics_and_stable_error_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from loguru import logger
    from test_image_gen import _settings as budget_settings

    from kantoku.config import logging_setup
    from kantoku.config.observability import request_trace, run_trace
    from kantoku.core import budget
    from kantoku.tools.image_gen import gen_image

    requests = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(
            400, headers={"x-request-id": "qwen-request-400"},
            json={"code": "InvalidParameter", "message": "unsupported size"},
        )

    monkeypatch.setattr(budget, "get_settings", lambda: budget_settings(tmp_path / "budget.db"))
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")
    client = OpenAI(base_url=_DASHSCOPE_BASE_URL, api_key="offline-test-key", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))
    provider = AlibabaQwenImageProvider(settings=_settings(
        tmp_path, provider="alibaba-qwen-image", protocol="dashscope-multimodal",
        base_url=_DASHSCOPE_BASE_URL, model="qwen-image-2.0-pro",
    ), client=client)
    try:
        logging_setup.setup_logging("INFO")
        with request_trace("trace-qwen-failed"), run_trace("run-qwen-failed", "generate"):
            result = gen_image("offline request", 1, project="test", episode="test",
                               client_request_id="image-qwen-400", provider=provider, est_fen=30)
            restored = gen_image("offline request", 1, project="test", episode="test",
                                 client_request_id="image-qwen-400", provider=provider, est_fen=30)
        assert result.status == "failed" and result.path is None
        assert restored.error_id == result.error_id
        assert len(requests) == 1
        assert json.loads(requests[0].content)["model"] == "qwen-image-2.0-pro"
        assert budget.get_reservation("image-qwen-400").status == "released"
    finally:
        client.close()
        logger.remove()
    for output in (capsys.readouterr().err,
                   (tmp_path / "logs/kantoku.log").read_text(encoding="utf-8")):
        for field in ("openai_image.py", "image_gen.py", "BadRequestError", "Traceback",
                      "qwen-request-400", "InvalidParameter", "unsupported size", "400",
                      "latency_ms", "run-qwen-failed", "trace-qwen-failed", result.error_id):
            assert field in output
        assert "offline-test-key" not in output


def test_dashscope_business_error_keeps_public_diagnostics_without_leaking_to_client(
    tmp_path: Path,
) -> None:
    client = _client()
    client.post = MagicMock(return_value={
        "code": "ModelNotFound", "message": "model unavailable", "request_id": "request-business",
    })
    provider = AlibabaQwenImageProvider(settings=_settings(
        tmp_path, provider="alibaba-qwen-image", protocol="dashscope-multimodal",
        base_url=_DASHSCOPE_BASE_URL, model="future-configured-model",
    ), client=client)
    with pytest.raises(ToolError) as caught:
        provider.submit(prompt="request", shot_no=1, client_request_id="business-error",
                        reference_urls=(), seed=None)
    assert caught.value.provider_error_code == "ModelNotFound"
    assert caught.value.provider_error_message == "model unavailable"
    assert caught.value.request_id == "request-business"
    assert "model unavailable" not in str(caught.value)


def test_qwen_output_safety_is_validated_before_submission_not_model_selection(
    tmp_path: Path,
) -> None:
    client = _client()
    provider = AlibabaQwenImageProvider(settings=_settings(
        tmp_path, provider="alibaba-qwen-image", model="future-model-id", force_single=False,
    ), client=client)
    with pytest.raises(ConfigError, match="单图模式"):
        provider.submit(
            prompt="a single image", shot_no=1, client_request_id="unsafe-output",
            reference_urls=(), seed=None,
        )
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
