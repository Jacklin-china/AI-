"""即梦官方 SDK 适配器测试；全部使用假客户端，不发网络请求。"""

from __future__ import annotations

from base64 import b64encode
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kantoku.config import ConfigError, ToolError
from kantoku.config.settings import ImageSettings
from kantoku.core.tracing import Trace
from kantoku.tools.jimeng import VolcengineJimengProvider

PNG_BYTES = b"\x89PNG\r\n\x1a\nmock-image"


class _FakeApiInfo:
    def __init__(self, action: str) -> None:
        self.query = {"Action": action, "Version": "old"}


class _FakeVisualClient:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.api_info = {
            "SubmitTask": _FakeApiInfo("SubmitTask"),
            "GetResult": _FakeApiInfo("GetResult"),
        }
        self.service_info = SimpleNamespace(
            credentials=SimpleNamespace(service="old-service", region="old-region")
        )
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.scheme = ""
        self.host = ""
        self.connection_timeout = 0.0
        self.socket_timeout = 0.0

    def set_ak(self, access_key: str) -> None:
        raise AssertionError("注入假客户端时不应读取真实 AK")

    def set_sk(self, secret_key: str) -> None:
        raise AssertionError("注入假客户端时不应读取真实 SK")

    def set_host(self, host: str) -> None:
        self.host = host

    def set_scheme(self, scheme: str) -> None:
        self.scheme = scheme

    def set_connection_timeout(self, timeout_s: float) -> None:
        self.connection_timeout = timeout_s

    def set_socket_timeout(self, timeout_s: float) -> None:
        self.socket_timeout = timeout_s

    def common_json_handler(self, action: str, form: dict[str, Any]) -> Any:
        self.calls.append((action, form))
        response = self.responses[action].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _settings(tmp_path: Path, **updates: object) -> ImageSettings:
    values: dict[str, object] = {
        "provider": "volcengine-jimeng",
        "base_url": "https://visual.example.test",
        "model": "configured-jimeng-model",
        "region": "configured-region",
        "service": "configured-service",
        "api_version": "2026-01-01",
        "submit_action": "SubmitTask",
        "query_action": "GetResult",
        "access_key_env": "TEST_AK",
        "secret_key_env": "TEST_SK",
        "width": 2560,
        "height": 1440,
        "force_single": True,
        "prompt_max_chars": 800,
        "timeout_s": 60.0,
        "query_retry": 2,
        "query_backoff_s": 2.0,
        "output_dir": tmp_path / "images",
    }
    values.update(updates)
    return ImageSettings.model_validate(values)


def test_adapter_uses_config_and_submits_exactly_once(tmp_path: Path) -> None:
    client = _FakeVisualClient(
        {"SubmitTask": [{"code": 10000, "data": {"task_id": "task-1"}}]}
    )
    provider = VolcengineJimengProvider(settings=_settings(tmp_path), client=client)

    task_id = provider.submit(
        prompt="雨夜便利店",
        shot_no=1,
        client_request_id="request-1",
        reference_urls=["https://example.test/persona.png"],
        seed=7,
    )

    assert task_id == "task-1"
    assert len(client.calls) == 1
    action, body = client.calls[0]
    assert action == "SubmitTask"
    assert body == {
        "req_key": "configured-jimeng-model",
        "prompt": "雨夜便利店",
        "width": 2560,
        "height": 1440,
        "force_single": True,
        "image_urls": ["https://example.test/persona.png"],
        "seed": 7,
    }
    assert client.host == "visual.example.test"
    assert client.scheme == "https"
    assert client.connection_timeout == 60.0
    assert client.service_info.credentials.region == "configured-region"
    assert client.api_info["SubmitTask"].query["Version"] == "2026-01-01"


def test_query_retries_only_pending_status_and_saves_one_image(tmp_path: Path) -> None:
    encoded = b64encode(PNG_BYTES).decode("ascii")
    client = _FakeVisualClient(
        {
            "GetResult": [
                {"code": 10000, "data": {"status": "in_queue"}},
                {"code": 10000, "data": {"status": "generating"}},
                {
                    "code": 10000,
                    "data": {"status": "done", "binary_data_base64": [encoded]},
                },
            ]
        }
    )
    waits: list[float] = []
    provider = VolcengineJimengProvider(
        settings=_settings(tmp_path), client=client, sleeper=waits.append
    )

    result = provider.query("task-1")

    assert result.status == "succeeded"
    assert result.actual_fen is None
    assert result.path is not None and result.path.read_bytes() == PNG_BYTES
    assert len(client.calls) == 3
    assert waits == [2.0, 4.0]


def test_query_exception_retries_but_submit_exception_does_not(tmp_path: Path) -> None:
    submit_client = _FakeVisualClient({"SubmitTask": [TimeoutError("submit-timeout")]})
    submit_provider = VolcengineJimengProvider(
        settings=_settings(tmp_path), client=submit_client, sleeper=lambda _: None
    )
    with pytest.raises(ToolError, match="TimeoutError"):
        submit_provider.submit(
            prompt="有效 prompt",
            shot_no=1,
            client_request_id="request-1",
            reference_urls=(),
            seed=None,
        )
    assert len(submit_client.calls) == 1

    query_client = _FakeVisualClient(
        {
            "GetResult": [
                TimeoutError("query-1"),
                TimeoutError("query-2"),
                {"code": 10000, "data": {"status": "not_found"}},
            ]
        }
    )
    query_provider = VolcengineJimengProvider(
        settings=_settings(tmp_path), client=query_client, sleeper=lambda _: None
    )
    result = query_provider.query("task-1")
    assert result.status == "unknown"
    assert len(query_client.calls) == 3


def test_sdk_json_error_keeps_safe_provider_diagnostic_and_trace(tmp_path: Path) -> None:
    error_payload = (
        "b'{\"ResponseMetadata\":{\"RequestId\":\"request-safe-1\","
        "\"Error\":{\"Code\":\"InvalidAuthorization\","
        "\"Message\":\"signature rejected\"}}}'"
    )
    client = _FakeVisualClient({"SubmitTask": [Exception(error_payload)]})
    traces: list[Trace] = []
    provider = VolcengineJimengProvider(
        settings=_settings(tmp_path), client=client, trace_writer=traces.append
    )

    with pytest.raises(ToolError) as caught:
        provider.submit(
            prompt="有效 prompt",
            shot_no=1,
            client_request_id="request-1",
            reference_urls=(),
            seed=None,
        )

    message = str(caught.value)
    assert "InvalidAuthorization" in message
    assert "request-safe-1" in message
    assert len(traces) == 1
    assert traces[0].kind == "image.submit"
    assert traces[0].shot_no == 1
    assert traces[0].ok is False
    assert traces[0].error is not None and "InvalidAuthorization" in traces[0].error


def test_access_check_only_queries_missing_task(tmp_path: Path) -> None:
    client = _FakeVisualClient(
        {
            "GetResult": [
                {
                    "code": 10000,
                    "data": {"status": "not_found"},
                    "request_id": "probe-request-1",
                }
            ]
        }
    )
    provider = VolcengineJimengProvider(settings=_settings(tmp_path), client=client)

    result = provider.check_access()

    assert result.authenticated is True
    assert result.service_ready is True
    assert result.code == 10000
    assert result.request_id == "probe-request-1"
    assert client.calls == [
        (
            "GetResult",
            {
                "req_key": "configured-jimeng-model",
                "task_id": "0000000000000000000",
            },
        )
    ]


def test_access_check_classifies_authentication_rejection(tmp_path: Path) -> None:
    client = _FakeVisualClient(
        {
            "GetResult": [
                {
                    "ResponseMetadata": {
                        "RequestId": "probe-denied-1",
                        "Error": {
                            "Code": "InvalidAuthorization",
                            "Message": "invalid authorization",
                        },
                    },
                }
            ]
        }
    )
    provider = VolcengineJimengProvider(settings=_settings(tmp_path), client=client)

    result = provider.check_access()

    assert result.authenticated is False
    assert result.service_ready is False
    assert result.code == "InvalidAuthorization"
    assert result.request_id == "probe-denied-1"


def test_access_check_keeps_provider_internal_error_inconclusive(tmp_path: Path) -> None:
    client = _FakeVisualClient(
        {
            "GetResult": [
                {
                    "code": 50500,
                    "message": "Internal Error",
                    "request_id": "probe-inconclusive-1",
                }
            ]
        }
    )
    provider = VolcengineJimengProvider(settings=_settings(tmp_path), client=client)

    result = provider.check_access()

    assert result.authenticated is None
    assert result.service_ready is False
    assert result.code == 50500
    assert result.message == "Internal Error"
    assert result.request_id == "probe-inconclusive-1"


def test_invalid_request_and_non_unique_output_are_safe(tmp_path: Path) -> None:
    client = _FakeVisualClient(
        {
            "GetResult": [
                {
                    "code": 10000,
                    "data": {"status": "done", "binary_data_base64": []},
                }
            ]
        }
    )
    provider = VolcengineJimengProvider(
        settings=_settings(tmp_path, prompt_max_chars=5),
        client=client,
        sleeper=lambda _: None,
    )

    with pytest.raises(ToolError, match="超过供应商限制"):
        provider.validate_request(
            prompt="这个提示词超过五个字符",
            shot_no=1,
            reference_urls=(),
            seed=None,
        )
    result = provider.query("task-1")
    assert result.status == "failed"
    assert result.actual_fen is None


@pytest.mark.parametrize(
    ("width", "height", "message"),
    [
        (1000, 1000, "宽高乘积"),
        (4096, 4096 + 1, "宽高乘积"),
        (4096, 1024, "宽高比"),
    ],
)
def test_invalid_output_geometry_fails_before_request(
    tmp_path: Path, width: int, height: int, message: str
) -> None:
    client = _FakeVisualClient({})

    with pytest.raises(ConfigError, match=message):
        VolcengineJimengProvider(
            settings=_settings(tmp_path, width=width, height=height), client=client
        )

    assert client.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "persona.png",
        "file:///private/persona.png",
        "https://user:password@example.test/persona.png",
    ],
)
def test_invalid_reference_url_fails_before_request(tmp_path: Path, url: str) -> None:
    client = _FakeVisualClient({})
    provider = VolcengineJimengProvider(settings=_settings(tmp_path), client=client)

    with pytest.raises(ToolError, match="参考图 URL"):
        provider.validate_request(
            prompt="有效提示词",
            shot_no=1,
            reference_urls=[url],
            seed=None,
        )

    assert client.calls == []


def test_invalid_png_payload_is_not_saved(tmp_path: Path) -> None:
    encoded = b64encode(b"not-a-png").decode("ascii")
    client = _FakeVisualClient(
        {
            "GetResult": [
                {
                    "code": 10000,
                    "data": {"status": "done", "binary_data_base64": [encoded]},
                }
            ]
        }
    )
    provider = VolcengineJimengProvider(settings=_settings(tmp_path), client=client)

    with pytest.raises(ToolError, match="有效 PNG"):
        provider.query("task-1")

    assert list((tmp_path / "images").glob("*")) == []


def test_same_provider_task_cannot_overwrite_a_different_image(tmp_path: Path) -> None:
    changed_png = PNG_BYTES + b"changed"
    client = _FakeVisualClient(
        {
            "GetResult": [
                {
                    "code": 10000,
                    "data": {
                        "status": "done",
                        "binary_data_base64": [b64encode(PNG_BYTES).decode("ascii")],
                    },
                },
                {
                    "code": 10000,
                    "data": {
                        "status": "done",
                        "binary_data_base64": [b64encode(changed_png).decode("ascii")],
                    },
                },
            ]
        }
    )
    provider = VolcengineJimengProvider(settings=_settings(tmp_path), client=client)
    first = provider.query("task-1")

    with pytest.raises(ToolError, match="保留原文件"):
        provider.query("task-1")

    assert first.path is not None
    assert first.path.read_bytes() == PNG_BYTES
