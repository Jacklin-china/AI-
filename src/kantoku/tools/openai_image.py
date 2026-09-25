"""OpenAI-compatible 同步生图适配器；结果先原子落盘再返回。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.request import urlopen

from loguru import logger
from openai import APIStatusError, OpenAI

from kantoku.config import ConfigError, ToolError, get_settings
from kantoku.config.settings import ROOT, ImageSettings
from kantoku.core.tracing import Trace, write_trace
from kantoku.schemas.media import ImageGenerationResult
from kantoku.tools.image_gen import ProviderAccessResult, RequestIdentityValue

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MIN_PIXELS = 655_360
_MAX_PIXELS = 8_294_400
_MAX_EDGE = 3_840
_ALLOWED_QUALITY = {"low", "medium", "high", "auto"}


class _ImagesResource(Protocol):
    def generate(self, **kwargs: Any) -> Any: ...


class _ModelsResource(Protocol):
    def retrieve(self, model: str) -> Any: ...


class _OpenAIClient(Protocol):
    images: _ImagesResource
    models: _ModelsResource


class OpenAIImageProvider:
    """以单图、同步、不可自动重提的方式接入 GPT-Image。"""

    def __init__(
        self,
        *,
        settings: ImageSettings | None = None,
        client: _OpenAIClient | None = None,
        trace_writer: Callable[[Trace], None] | None = None,
    ) -> None:
        self.settings = settings or get_settings().image
        self.model_id = self.settings.model
        self.output_dir = self._output_dir(self.settings.output_dir)
        self._validate_configuration()
        # Authentication is checked before reservation, not during application startup.
        self._client = client
        self._client_lock = threading.Lock()
        self._trace_writer = write_trace if client is None else trace_writer

    def _client_for_request(self) -> _OpenAIClient:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = OpenAI(
                        base_url=self.settings.base_url,
                        api_key=self.settings.api_key(),
                        timeout=self.settings.timeout_s,
                        max_retries=0,
                    )
        return self._client

    @staticmethod
    def _output_dir(configured: Path) -> Path:
        return configured if configured.is_absolute() else ROOT / configured

    def _validate_configuration(self) -> None:
        endpoint = urlsplit(self.settings.base_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise ConfigError("生图 API 地址不合法")
        if endpoint.username is not None or endpoint.password is not None or endpoint.fragment:
            raise ConfigError("生图 API 地址不允许包含凭据或片段")
        width, height = self.settings.width, self.settings.height
        pixels = width * height
        if width % 16 or height % 16:
            raise ConfigError("生图输出宽高必须是 16 的倍数")
        if self.settings.provider == "alibaba-qwen-image":
            if not 512**2 <= pixels <= 2048**2 or not 1 / 8 <= width / height <= 8:
                raise ConfigError("Qwen-Image-3.0 输出尺寸超出支持范围")
        elif (max(width, height) > _MAX_EDGE
              or not _MIN_PIXELS <= pixels <= _MAX_PIXELS
              or not 1 / 3 <= width / height <= 3):
            raise ConfigError("GPT-Image 输出尺寸或宽高比超出支持范围")
        if self.settings.quality not in _ALLOWED_QUALITY:
            raise ConfigError("GPT-Image quality 配置不受支持")
        if self.settings.output_format != "png":
            raise ConfigError("当前工作台仅支持 GPT-Image 输出 PNG")
        if not self.settings.force_single:
            raise ConfigError("GPT-Image 生产调用必须保持单图模式")

    def generation_identity(self) -> Mapping[str, RequestIdentityValue]:
        """让模型、尺寸和质量共同参与本地幂等指纹。"""
        return {
            "provider": self.settings.provider,
            "base_url": self.settings.base_url,
            "model": self.settings.model,
            "width": self.settings.width,
            "height": self.settings.height,
            "quality": self.settings.quality,
            "output_format": self.settings.output_format,
            "force_single": self.settings.force_single,
        }

    def check_access(self) -> ProviderAccessResult:
        """读取模型信息验证密钥和端点；不调用生成接口。"""
        try:
            self._client_for_request().models.retrieve(self.model_id)
        except APIStatusError as error:
            authenticated = False if error.status_code in {401, 403} else None
            return ProviderAccessResult(
                authenticated=authenticated,
                service_ready=False,
                code=error.status_code,
                message="模型访问检查失败",
                request_id=getattr(error, "request_id", None),
            )
        except Exception as error:
            return ProviderAccessResult(
                authenticated=None,
                service_ready=False,
                code=type(error).__name__,
                message="模型访问状态无法确认",
                request_id=None,
            )
        return ProviderAccessResult(
            authenticated=True,
            service_ready=True,
            code=200,
            message="model accessible",
            request_id=None,
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
        if reference_urls and self.settings.provider != "alibaba-qwen-image":
            raise ToolError("参考图需要走图片编辑接口，当前单镜生成暂不接受 URL")
        if seed is not None:
            raise ToolError("当前生图接口不支持固定 seed")

    def submit(
        self,
        *,
        prompt: str,
        shot_no: int,
        client_request_id: str,
        reference_urls: Sequence[str],
        seed: int | None,
    ) -> str:
        """执行一次同步生成；网络异常由上层记为 unknown，禁止自动重提。"""
        self.validate_request(
            prompt=prompt,
            shot_no=shot_no,
            reference_urls=reference_urls,
            seed=seed,
        )
        digest = hashlib.sha256(client_request_id.encode("utf-8")).hexdigest()[:24]
        provider_job_id = f"openai-{digest}"
        qwen_compatible = self.settings.provider == "alibaba-qwen-image"
        started_at = perf_counter()
        ok = False
        error_name: str | None = None
        try:
            request: dict[str, Any] = {
                "model": self.model_id,
                "prompt": prompt.strip(),
                "n": 1,
                "size": f"{self.settings.width}x{self.settings.height}",
                "extra_headers": {"Idempotency-Key": client_request_id},
            }
            if qwen_compatible and reference_urls:
                request["extra_body"] = {
                    "image": (reference_urls[0] if len(reference_urls) == 1
                              else list(reference_urls)),
                }
            if not qwen_compatible:
                request.update(
                    quality=self.settings.quality,
                    output_format=self.settings.output_format,
                    response_format="b64_json",
                )
            response = self._client_for_request().images.generate(**request)
            data = getattr(response, "data", None)
            if not isinstance(data, list) or len(data) != 1:
                raise ToolError("生图接口未返回唯一图片，费用需要人工对账")
            encoded = getattr(data[0], "b64_json", None)
            if isinstance(encoded, str) and encoded:
                try:
                    image_bytes = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as error:
                    raise ToolError("生图图片内容无法解码，费用需要人工对账") from error
                self._save_image(provider_job_id, image_bytes)
            elif qwen_compatible and isinstance(getattr(data[0], "url", None), str):
                # Persist a returned URL before download so a failed download can resume.
                self._save_pending_url(provider_job_id, data[0].url)
            else:
                raise ToolError("生图结果没有可恢复的图片数据，费用需要人工对账")
            ok = True
            return provider_job_id
        except Exception as error:
            error_name = self._safe_error(error)
            if isinstance(error, ToolError):
                raise
            raise ToolError(
                "生图结果未确认",
                detail=f"{error_name}；请核对平台账单，系统不会自动重提",
            ) from error
        finally:
            self._write_trace(
                started_at=started_at,
                shot_no=shot_no,
                ok=ok,
                error=error_name,
            )

    def query(self, provider_job_id: str) -> ImageGenerationResult:
        """同步 API 无远端任务查询；只读取已保存图片或原响应 URL。"""
        path = self._path_for(provider_job_id)
        pending_url = self._pending_url_path(provider_job_id)
        if not path.is_file() and pending_url.is_file():
            try:
                payload = json.loads(pending_url.read_text(encoding="utf-8"))
                self._save_image(provider_job_id, self._download_url(payload["url"]))
                pending_url.unlink()
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ToolError(
                    "生图结果下载未完成，保留原供应商结果供继续查询",
                    detail=type(error).__name__,
                ) from error
        if path.is_file():
            return ImageGenerationResult(
                path=path,
                provider_job_id=provider_job_id,
                status="succeeded",
                actual_fen=None,
                error=None,
            )
        return ImageGenerationResult(
            path=None,
            provider_job_id=provider_job_id,
            status="unknown",
            actual_fen=None,
            error="GPT-Image 同步结果文件缺失，需要人工核对账单",
        )

    def _path_for(self, provider_job_id: str) -> Path:
        if not provider_job_id.startswith("openai-") or len(provider_job_id) != 31:
            raise ToolError("GPT-Image 本地任务 ID 不合法")
        return self.output_dir / f"gpt-image-{provider_job_id.removeprefix('openai-')}.png"

    def _pending_url_path(self, provider_job_id: str) -> Path:
        return self._path_for(provider_job_id).with_suffix(".url.json")

    def _save_pending_url(self, provider_job_id: str, url: str) -> None:
        self._check_result_url(url)
        path = self._pending_url_path(provider_job_id)
        temporary = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"url": url}), encoding="utf-8")
            temporary.replace(path)
        except OSError as error:
            raise ToolError("供应商图片地址保存失败", detail=type(error).__name__) from error

    def _download_url(self, url: str) -> bytes:
        self._check_result_url(url)
        with urlopen(url, timeout=self.settings.timeout_s) as response:
            content = response.read(20 * 1024 * 1024 + 1)
        if len(content) > 20 * 1024 * 1024:
            raise ToolError("供应商图片超过本地大小上限")
        return content

    @staticmethod
    def _check_result_url(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".aliyuncs.com")
            or parsed.username is not None or parsed.password is not None
        ):
            raise ToolError("供应商图片地址不可信，费用需要人工对账")

    def _save_image(self, provider_job_id: str, image_bytes: bytes) -> Path:
        if not image_bytes.startswith(_PNG_SIGNATURE):
            raise ToolError("GPT-Image 返回的图片不是有效 PNG 数据，费用需要人工对账")
        path = self._path_for(provider_job_id)
        temporary = path.with_suffix(".png.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file():
                if path.read_bytes() != image_bytes:
                    raise ToolError("同一 GPT-Image 请求返回不同图片，已保留原文件")
                return path
            temporary.write_bytes(image_bytes)
            temporary.replace(path)
        except ToolError:
            raise
        except OSError as error:
            raise ToolError("GPT-Image 图片保存失败", detail=type(error).__name__) from error
        return path

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        if isinstance(error, APIStatusError):
            return f"{type(error).__name__}(status={error.status_code})"
        return type(error).__name__

    def _write_trace(
        self,
        *,
        started_at: float,
        shot_no: int,
        ok: bool,
        error: str | None,
    ) -> None:
        if self._trace_writer is None:
            return
        try:
            self._trace_writer(
                Trace(
                    ts=datetime.now(UTC).isoformat(),
                    kind="image.submit",
                    model=self.model_id,
                    in_tokens=0,
                    out_tokens=0,
                    latency_ms=max(0, round((perf_counter() - started_at) * 1000)),
                    cost_fen=None,
                    ok=ok,
                    shot_no=shot_no,
                    error=error,
                    usage_reported=False,
                )
            )
        except Exception as trace_error:
            logger.error("GPT-Image trace 写入失败：{}", type(trace_error).__name__)


class AlibabaQwenImageProvider(OpenAIImageProvider):
    """Qwen-Image-3.0 的 OpenAI Images 兼容适配，复用单次提交与本地恢复。"""

    def _validate_configuration(self) -> None:
        super()._validate_configuration()
        if self.settings.provider != "alibaba-qwen-image":
            raise ConfigError("Qwen 生图供应商标识不正确")
        if self.model_id != "qwen-image-3.0":
            raise ConfigError("当前 Qwen 适配器只支持 qwen-image-3.0")
        if urlsplit(self.settings.base_url).path.rstrip("/") != "/compatible-mode/v1":
            raise ConfigError("Qwen 生图地址必须是 OpenAI 兼容接口 /compatible-mode/v1")
        if self.settings.api_key_env != "DASHSCOPE_API_KEY":
            raise ConfigError("Qwen 生图必须使用 DASHSCOPE_API_KEY")

    def check_access(self) -> ProviderAccessResult:
        """兼容接口没有可靠的免计费生图探针，不能把模型查询冒充可用性验证。"""
        return ProviderAccessResult(
            authenticated=None,
            service_ready=False,
            code="UNVERIFIED",
            message="Qwen 生图没有可靠的免计费探针；需单次受预算保护的生成验收",
            request_id=None,
        )

    def validate_request(
        self, *, prompt: str, shot_no: int,
        reference_urls: Sequence[str], seed: int | None,
    ) -> None:
        super().validate_request(
            prompt=prompt, shot_no=shot_no,
            reference_urls=reference_urls, seed=seed,
        )
        if self.settings.provider != "alibaba-qwen-image":
            raise ConfigError("Qwen 生图供应商标识不正确")
        if len(reference_urls) > 3 or any(
            not isinstance(value, str)
            or not (value.startswith("data:image/") or value.startswith("https://"))
            or len(value) > 13_500_000
            for value in reference_urls
        ):
            raise ToolError("Qwen 参考图仅支持最多三张 HTTPS 或 Base64 图片")
        base_url = self.settings.base_url.strip()
        endpoint = urlsplit(base_url)
        if (
            not base_url
            or endpoint.scheme != "https"
            or not endpoint.netloc
            or "/compatible-mode/v1" not in endpoint.path
            or endpoint.query
        ):
            raise ConfigError("Qwen 生图需配置有效的 HTTPS OpenAI 兼容 base_url")
        if self._client is None:
            self.settings.api_key()
