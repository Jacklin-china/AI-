"""火山引擎即梦 4.0 异步生图适配器。"""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from volcengine.visual.VisualService import VisualService

from kantoku.config import ConfigError, ToolError, TracingError, get_settings
from kantoku.config.settings import ROOT, ImageSettings
from kantoku.core.tracing import Trace, write_trace
from kantoku.schemas.media import ImageGenerationResult
from kantoku.tools.image_gen import ProviderAccessResult, RequestIdentityValue

_SUCCESS_CODE = 10000
_PENDING_STATUSES = {"in_queue", "generating"}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class _SdkApiInfo(Protocol):
    query: dict[str, str]


class _SdkCredentials(Protocol):
    service: str
    region: str


class _SdkServiceInfo(Protocol):
    credentials: _SdkCredentials


class _VisualClient(Protocol):
    api_info: dict[str, _SdkApiInfo]
    service_info: _SdkServiceInfo

    def set_ak(self, access_key: str) -> None: ...

    def set_sk(self, secret_key: str) -> None: ...

    def set_host(self, host: str) -> None: ...

    def set_scheme(self, scheme: str) -> None: ...

    def set_connection_timeout(self, timeout_s: float) -> None: ...

    def set_socket_timeout(self, timeout_s: float) -> None: ...

    def common_json_handler(self, action: str, form: dict[str, Any]) -> Any: ...


class VolcengineJimengProvider:
    """提交最多一次；只对不产生新任务的查询做有限重试。"""

    def __init__(
        self,
        *,
        settings: ImageSettings | None = None,
        client: _VisualClient | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        trace_writer: Callable[[Trace], None] | None = None,
    ) -> None:
        self.settings = settings or get_settings().image
        self.model_id = self.settings.model
        self.output_dir = self._output_dir(self.settings.output_dir)
        self._sleeper = sleeper
        self._client = client or VisualService()
        # 注入假客户端的单测默认不接触项目数据库；生产客户端必须记录外部调用。
        self._trace_writer = write_trace if client is None else trace_writer
        self._configure_client(load_credentials=client is None)

    @staticmethod
    def _output_dir(configured: Path) -> Path:
        return configured if configured.is_absolute() else ROOT / configured

    def _configure_client(self, *, load_credentials: bool) -> None:
        self._validate_output_geometry()
        endpoint = urlsplit(self.settings.base_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise ConfigError("生图 API 地址不合法")
        if endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment:
            raise ConfigError("生图 API 地址只能包含协议和主机名")

        self._client.set_scheme(endpoint.scheme)
        self._client.set_host(endpoint.netloc)
        self._client.set_connection_timeout(self.settings.timeout_s)
        self._client.set_socket_timeout(self.settings.timeout_s)
        self._client.service_info.credentials.service = self.settings.service
        self._client.service_info.credentials.region = self.settings.region
        for action in self.actions:
            api_info = self._client.api_info.get(action)
            if api_info is None:
                raise ConfigError("官方生图接口动作不受当前 SDK 支持", detail=action)
            api_info.query["Action"] = action
            api_info.query["Version"] = self.settings.api_version
        if load_credentials:
            self._client.set_ak(self.settings.access_key())
            self._client.set_sk(self.settings.secret_key())

    def _validate_output_geometry(self) -> None:
        """在联网前执行即梦 4.0 的面积和宽高比硬限制。"""
        area = self.settings.width * self.settings.height
        ratio = self.settings.width / self.settings.height
        if not 1024**2 <= area <= 4096**2:
            raise ConfigError("生图宽高乘积超出即梦 4.0 支持范围")
        if not 1 / 3 <= ratio <= 3:
            raise ConfigError("生图宽高比超出即梦 4.0 支持范围")

    @property
    def actions(self) -> tuple[str, str]:
        return self.settings.submit_action, self.settings.query_action

    def generation_identity(self) -> Mapping[str, RequestIdentityValue]:
        """提供生成与对账所需的非敏感配置，参与本地幂等指纹。"""
        return {
            "provider": self.settings.provider,
            "base_url": self.settings.base_url,
            "model": self.settings.model,
            "region": self.settings.region,
            "service": self.settings.service,
            "api_version": self.settings.api_version,
            "submit_action": self.settings.submit_action,
            "query_action": self.settings.query_action,
            "width": self.settings.width,
            "height": self.settings.height,
            "force_single": self.settings.force_single,
        }

    def check_access(self) -> ProviderAccessResult:
        """用不存在的任务 ID 验证签名和服务权限，不创建生图任务。"""
        probe_id = "0000000000000000000"
        response = self._call_json(
            self.settings.query_action,
            {"req_key": self.settings.model, "task_id": probe_id},
            operation="鉴权探针",
            trace_kind="access_probe",
        )
        code, message, request_id = self._diagnostic_values(response)
        auth_failed = code in {
            100024,
            100025,
            100026,
            "InvalidAuthorization",
            "InvalidCredential",
            "InvalidSecretToken",
        }
        if auth_failed:
            return ProviderAccessResult(
                authenticated=False,
                service_ready=False,
                code=code,
                message=message,
                request_id=request_id,
            )
        data = response.get("data")
        status = data.get("status") if isinstance(data, dict) else None
        service_ready = code == _SUCCESS_CODE and status in {"not_found", "expired"}
        return ProviderAccessResult(
            authenticated=True if service_ready else None,
            service_ready=service_ready,
            code=code,
            message=message,
            request_id=request_id,
        )

    def validate_request(
        self,
        *,
        prompt: str,
        shot_no: int,
        reference_urls: Sequence[str],
        seed: int | None,
    ) -> None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ToolError("生图 Prompt 不能为空")
        if len(prompt) > self.settings.prompt_max_chars:
            raise ToolError(
                "生图 Prompt 超过供应商限制",
                detail=f"当前 {len(prompt)} 字符，上限 {self.settings.prompt_max_chars}",
            )
        if type(shot_no) is not int or shot_no <= 0:
            raise ToolError("镜号必须是正整数")
        if len(reference_urls) > 10:
            raise ToolError("参考图 URL 最多允许 10 个")
        for url in reference_urls:
            if not isinstance(url, str) or not url.strip():
                raise ToolError("参考图 URL 必须是非空字符串")
            parsed = urlsplit(url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ToolError("参考图 URL 必须是完整的 HTTP 或 HTTPS 地址")
            if parsed.username is not None or parsed.password is not None:
                raise ToolError("参考图 URL 不允许包含用户名或密码")
        if seed is not None and (type(seed) is not int or seed < 0):
            raise ToolError("seed 必须是非负整数")

    def submit(
        self,
        *,
        prompt: str,
        shot_no: int,
        client_request_id: str,
        reference_urls: Sequence[str],
        seed: int | None,
    ) -> str:
        """只调用一次 SDK 提交方法；client_request_id 由本地台账防重。"""
        self.validate_request(
            prompt=prompt,
            shot_no=shot_no,
            reference_urls=reference_urls,
            seed=seed,
        )
        body: dict[str, Any] = {
            "req_key": self.settings.model,
            "prompt": prompt.strip(),
            "width": self.settings.width,
            "height": self.settings.height,
            "force_single": self.settings.force_single,
        }
        if reference_urls:
            body["image_urls"] = [url.strip() for url in reference_urls]
        if seed is not None:
            body["seed"] = seed

        response = self._call_json(
            self.settings.submit_action,
            body,
            operation="提交",
            trace_kind="submit",
            shot_no=shot_no,
        )
        code = response.get("code")
        data = response.get("data")
        if code != _SUCCESS_CODE or not isinstance(data, dict):
            raise ToolError("即梦任务提交未确认", detail=self._diagnostic(response))
        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ToolError("即梦任务提交未返回任务 ID")
        return task_id.strip()

    def query(self, provider_job_id: str) -> ImageGenerationResult:
        """查询最多 retry+1 次；排队未完成不会触发新的付费提交。"""
        if not isinstance(provider_job_id, str) or not provider_job_id.strip():
            raise ToolError("即梦任务 ID 不能为空")
        task_id = provider_job_id.strip()
        for attempt in range(self.settings.query_retry + 1):
            try:
                response = self._call_json(
                    self.settings.query_action,
                    {"req_key": self.settings.model, "task_id": task_id},
                    operation="查询",
                    trace_kind="query",
                )
            except Exception as error:
                if attempt >= self.settings.query_retry:
                    if isinstance(error, ToolError):
                        raise
                    raise ToolError(
                        "即梦任务查询失败", detail=type(error).__name__
                    ) from error
                self._wait_before_retry(attempt)
                continue

            result, pending = self._parse_query_response(task_id, response)
            if not pending or attempt >= self.settings.query_retry:
                return result
            self._wait_before_retry(attempt)
        raise ToolError("即梦任务查询状态异常")

    def _wait_before_retry(self, attempt: int) -> None:
        self._sleeper(self.settings.query_backoff_s * (2**attempt))

    def _call_json(
        self,
        action: str,
        body: dict[str, Any],
        *,
        operation: str,
        trace_kind: str,
        shot_no: int | None = None,
    ) -> dict[str, Any]:
        """调用一次 SDK，并只把服务端安全诊断字段写入错误和 trace。"""
        started = time.perf_counter()
        try:
            raw = self._client.common_json_handler(action, body)
            response = self._response_mapping(raw, operation=operation)
        except Exception as error:
            response = self._response_from_exception(error)
            if response is not None:
                self._write_trace(
                    trace_kind=trace_kind,
                    started=started,
                    ok=response.get("code") == _SUCCESS_CODE,
                    error=(
                        None
                        if response.get("code") == _SUCCESS_CODE
                        else self._diagnostic(response)
                    ),
                    shot_no=shot_no,
                )
                return response
            detail = type(error).__name__
            self._write_trace(
                trace_kind=trace_kind,
                started=started,
                ok=False,
                error=detail,
                shot_no=shot_no,
            )
            raise ToolError(f"即梦{operation}请求失败", detail=detail) from error

        ok = response.get("code") == _SUCCESS_CODE
        self._write_trace(
            trace_kind=trace_kind,
            started=started,
            ok=ok,
            error=None if ok else self._diagnostic(response),
            shot_no=shot_no,
        )
        return response

    def _write_trace(
        self,
        *,
        trace_kind: str,
        started: float,
        ok: bool,
        error: str | None,
        shot_no: int | None,
    ) -> None:
        """记录外部调用；埋点故障不得抹掉已返回的供应商任务 ID。"""
        if self._trace_writer is None:
            return
        trace = Trace(
            ts=datetime.now(UTC).isoformat(),
            kind=f"image.{trace_kind}",
            model=self.model_id,
            in_tokens=0,
            out_tokens=0,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            cost_fen=None,
            ok=ok,
            shot_no=shot_no,
            error=error,
            usage_reported=None,
        )
        try:
            self._trace_writer(trace)
        except TracingError:
            # 供应商结果比埋点更重要，尤其提交成功后不能因 trace 失败丢失任务 ID。
            return

    @staticmethod
    def _response_from_exception(error: Exception) -> dict[str, Any] | None:
        """兼容官方 SDK 把 JSON 错误包装成 ``b'...'`` 字符串的行为。"""
        text = str(error).strip()
        if not text:
            return None
        candidate: object = text
        if text.startswith(("b'", 'b"')):
            try:
                candidate = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                return None
        if isinstance(candidate, bytes):
            try:
                candidate = candidate.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not isinstance(candidate, str):
            return None
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _diagnostic_values(
        response: dict[str, Any] | None,
    ) -> tuple[str | int | None, str | None, str | None]:
        """取得结构化安全字段；不返回请求体、AK 或 SK。"""
        if response is None:
            return None, None, None
        metadata = response.get("ResponseMetadata")
        nested_error = metadata.get("Error") if isinstance(metadata, dict) else None
        code = response.get("code")
        message = response.get("message")
        request_id = response.get("request_id")
        if isinstance(nested_error, dict):
            code = nested_error.get("Code", nested_error.get("CodeN", code))
            message = nested_error.get("Message", message)
        if isinstance(metadata, dict):
            request_id = metadata.get("RequestId", request_id)
        safe_code = code if isinstance(code, (str, int)) and not isinstance(code, bool) else None
        safe_message = message if isinstance(message, str) and message else None
        safe_request_id = request_id if isinstance(request_id, str) and request_id else None
        return safe_code, safe_message, safe_request_id

    @classmethod
    def _diagnostic(cls, response: dict[str, Any] | None) -> str:
        """只格式化非敏感错误字段，禁止把请求体、AK 或 SK 写入日志。"""
        code, message, request_id = cls._diagnostic_values(response)
        fields = (("code", code), ("message", message), ("request_id", request_id))
        parts = [
            f"{name}={value}"
            for name, value in fields
            if value is not None and value != ""
        ]
        return "；".join(parts) if parts else "供应商未返回错误码或请求号"

    @staticmethod
    def _response_mapping(response: Any, *, operation: str) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise ToolError(f"即梦任务{operation}响应格式错误")
        return response

    def _parse_query_response(
        self,
        task_id: str,
        response: dict[str, Any],
    ) -> tuple[ImageGenerationResult, bool]:
        code = response.get("code")
        data = response.get("data")
        if code != _SUCCESS_CODE or not isinstance(data, dict):
            return (
                ImageGenerationResult(
                    provider_job_id=task_id,
                    status="unknown",
                    error=f"即梦查询未确认：{self._diagnostic(response)}",
                ),
                False,
            )
        status = data.get("status")
        if status in _PENDING_STATUSES:
            return (
                ImageGenerationResult(
                    provider_job_id=task_id,
                    status="unknown",
                    error="即梦任务仍在排队或生成中",
                ),
                True,
            )
        if status in {"not_found", "expired"}:
            return (
                ImageGenerationResult(
                    provider_job_id=task_id,
                    status="unknown",
                    error="即梦任务不存在或已过期，需要人工核对账单",
                ),
                False,
            )
        if status != "done":
            return (
                ImageGenerationResult(
                    provider_job_id=task_id,
                    status="unknown",
                    error="即梦返回了未识别的任务状态",
                ),
                False,
            )

        encoded_images = data.get("binary_data_base64")
        if not isinstance(encoded_images, list) or len(encoded_images) != 1:
            return (
                ImageGenerationResult(
                    provider_job_id=task_id,
                    status="failed",
                    error="即梦任务完成但没有得到唯一图片，费用待对账",
                ),
                False,
            )
        encoded = encoded_images[0]
        if not isinstance(encoded, str) or not encoded:
            raise ToolError("即梦图片内容格式错误")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ToolError("即梦图片内容无法解码") from error
        path = self._save_image(task_id, image_bytes)
        return (
            ImageGenerationResult(
                path=path,
                provider_job_id=task_id,
                status="succeeded",
                actual_fen=None,
                error=None,
            ),
            False,
        )

    def _save_image(self, task_id: str, image_bytes: bytes) -> Path:
        if not image_bytes.startswith(_PNG_SIGNATURE):
            raise ToolError("即梦返回的图片不是有效 PNG 数据")
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
        path = self.output_dir / f"jimeng-{digest}.png"
        temporary = path.with_suffix(".png.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file():
                if path.read_bytes() != image_bytes:
                    raise ToolError("同一即梦任务返回了不同图片，已保留原文件")
                return path
            temporary.write_bytes(image_bytes)
            temporary.replace(path)
        except ToolError:
            raise
        except OSError as error:
            raise ToolError("即梦图片保存失败", detail=type(error).__name__) from error
        return path
