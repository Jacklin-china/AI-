"""生图任务编排：先预占、只提交一次、查询后按真实账单结算。"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from loguru import logger
from pydantic import ValidationError

from kantoku.config import BudgetError, ToolError
from kantoku.config.observability import current_run_id
from kantoku.core.budget import (
    ReservationRequest,
    claim_submission,
    estimate_image_fen,
    get_reservation,
    load_generation_result,
    mark_outcome,
    mark_submitted,
    release,
    reserve,
    save_generation_result,
    settle,
)
from kantoku.schemas.media import ImageGenerationResult

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
RequestIdentityValue = str | int | bool


@dataclass(frozen=True, slots=True)
class ProviderAccessResult:
    """不创建任务的供应商接入诊断结果。"""

    authenticated: bool | None
    service_ready: bool
    code: str | int | None
    message: str | None
    request_id: str | None


class ImageProvider(Protocol):
    """真实与本地假供应商共同遵守的最小异步接口。"""

    model_id: str

    def generation_identity(self) -> Mapping[str, RequestIdentityValue]:
        """返回会改变生成结果或账单归属的非敏感配置。"""

    def check_access(self) -> ProviderAccessResult:
        """验证供应商鉴权；不得创建任务或产生生成费用。"""

    def validate_request(
        self,
        *,
        prompt: str,
        shot_no: int,
        reference_urls: Sequence[str],
        seed: int | None,
    ) -> None:
        """在预算预占和网络请求之前拒绝确定无效的参数。"""

    def submit(
        self,
        *,
        prompt: str,
        shot_no: int,
        client_request_id: str,
        reference_urls: Sequence[str],
        seed: int | None,
    ) -> str:
        """提交一次付费任务并返回供应商任务 ID。"""

    def query(self, provider_job_id: str) -> ImageGenerationResult:
        """查询已提交任务；查询可以重试，但不得重新提交。"""


class LocalFakeImageProvider:
    """不联网、不收费的 W4 编排验证器，输出一张有效占位 PNG。"""

    def __init__(self, output_dir: Path, *, model_id: str, actual_fen: int) -> None:
        if type(actual_fen) is not int or actual_fen < 0:
            raise ToolError("假供应商实际费用必须是非负整数分")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ToolError("假供应商模型标识不能为空")
        self.output_dir = output_dir
        self.model_id = model_id.strip()
        self.actual_fen = actual_fen
        self.submit_count = 0
        self._shots: dict[str, int] = {}

    def generation_identity(self) -> Mapping[str, RequestIdentityValue]:
        """让本地验证也遵守与真实供应商相同的幂等契约。"""
        return {"provider": "local-fake", "model": self.model_id}

    def check_access(self) -> ProviderAccessResult:
        """本地假供应商始终可用，且没有网络副作用。"""
        return ProviderAccessResult(
            authenticated=True,
            service_ready=True,
            code=10000,
            message="local fake ready",
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
            raise ToolError("假供应商收到空 Prompt")
        if type(shot_no) is not int or shot_no <= 0:
            raise ToolError("假供应商收到无效生图请求")
        if any(not isinstance(url, str) or not url.strip() for url in reference_urls):
            raise ToolError("假供应商收到无效参考图")
        if seed is not None and (type(seed) is not int or seed < 0):
            raise ToolError("假供应商收到无效 seed")

    def submit(
        self,
        *,
        prompt: str,
        shot_no: int,
        client_request_id: str,
        reference_urls: Sequence[str],
        seed: int | None,
    ) -> str:
        self.validate_request(
            prompt=prompt,
            shot_no=shot_no,
            reference_urls=reference_urls,
            seed=seed,
        )
        self.submit_count += 1
        digest = hashlib.sha256(client_request_id.encode("utf-8")).hexdigest()[:16]
        provider_job_id = f"fake-{digest}"
        self._shots[provider_job_id] = shot_no
        return provider_job_id

    def query(self, provider_job_id: str) -> ImageGenerationResult:
        shot_no = self._shots.get(provider_job_id)
        if shot_no is None:
            raise ToolError("假供应商找不到任务")
        path = self.output_dir / f"shot-{shot_no:03d}-{provider_job_id}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_ONE_PIXEL_PNG)
        except OSError as error:
            raise ToolError("假图保存失败", detail=type(error).__name__) from error
        return ImageGenerationResult(
            path=path,
            provider_job_id=provider_job_id,
            status="succeeded",
            actual_fen=self.actual_fen,
            error=None,
        )


def _unknown_result(provider_job_id: str | None, error: BaseException) -> ImageGenerationResult:
    detail = str(error) if isinstance(error, ToolError) else type(error).__name__
    if provider_job_id is None:
        detail = f"NEEDS_RECONCILIATION：{detail}"
    return ImageGenerationResult(
        path=None,
        provider_job_id=provider_job_id,
        status="unknown",
        actual_fen=None,
        error=f"{detail}，需查询或人工对账",
    )


def _record_result(
    reservation_id: str,
    provider_job_id: str,
    result: ImageGenerationResult,
) -> ImageGenerationResult:
    if result.provider_job_id not in {None, provider_job_id}:
        raise ToolError("供应商查询结果的任务 ID 不一致")
    normalized = result.model_copy(update={"provider_job_id": provider_job_id})
    logger.bind(component="image-generation", generation_request_id=reservation_id,
                provider_task_id=provider_job_id).info("result status={}", normalized.status)
    mark_outcome(
        reservation_id,
        normalized.status,
        provider_job_id=provider_job_id,
    )
    save_generation_result(reservation_id, normalized)
    if normalized.path is not None:
        logger.bind(component="image-generation", generation_request_id=reservation_id,
                    provider_task_id=provider_job_id).info("download path={}", normalized.path)
    if normalized.actual_fen is not None:
        settle(reservation_id, normalized.actual_fen)
        logger.bind(
            component="image-generation", generation_request_id=reservation_id,
            provider_task_id=provider_job_id,
        ).info("settle actual_fen={}", normalized.actual_fen)
    return normalized


def prepare_image_reservation(
    prompt: str,
    shot_no: int,
    *,
    project: str,
    episode: str,
    client_request_id: str,
    provider: ImageProvider,
    est_fen: int | None = None,
    reference_urls: Sequence[str] = (),
    seed: int | None = None,
) -> ReservationRequest:
    """校验一镜并生成带指纹的预算请求，不联网、不修改台账。"""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolError("生图 Prompt 不能为空")
    provider.validate_request(
        prompt=prompt,
        shot_no=shot_no,
        reference_urls=reference_urls,
        seed=seed,
    )
    estimated_fen = estimate_image_fen() if est_fen is None else est_fen
    try:
        fingerprint_payload = {
            "generation": dict(provider.generation_identity()),
            "prompt": prompt.strip(),
            "reference_urls": [url.strip() for url in reference_urls],
            "seed": seed,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError) as error:
        raise ToolError("生图供应商的生成身份无效", detail=type(error).__name__) from error
    try:
        return ReservationRequest(
            reservation_id=client_request_id,
            job=f"image.generate:{fingerprint}",
            project=project,
            episode=episode,
            shot_no=shot_no,
            kind="image",
            est_fen=estimated_fen,
            model=provider.model_id,
            run_id=current_run_id(),
            provider=str(provider.generation_identity().get("provider", "unknown")),
            idempotency_key=client_request_id,
        )
    except ValidationError as error:
        raise BudgetError("生图预占参数无效", detail=type(error).__name__) from error


def gen_image(
    prompt: str,
    shot_no: int,
    *,
    project: str,
    episode: str,
    client_request_id: str,
    provider: ImageProvider,
    est_fen: int | None = None,
    reference_urls: Sequence[str] = (),
    seed: int | None = None,
) -> ImageGenerationResult:
    """生成单张图；重复调用复用确定结果，未知请求不会重新提交。"""
    request = prepare_image_reservation(
        prompt,
        shot_no,
        project=project,
        episode=episode,
        client_request_id=client_request_id,
        provider=provider,
        est_fen=est_fen,
        reference_urls=reference_urls,
        seed=seed,
    )
    client_request_id = request.reservation_id
    reservation = reserve(**request.model_dump())
    provider_name = str(provider.generation_identity().get("provider", "unknown"))
    event = logger.bind(component="image-generation", generation_request_id=client_request_id,
                        idempotency_key=client_request_id, provider=provider_name)
    event.info("reserve status={} est_fen={}", reservation.status, reservation.est_fen)
    if reservation.status != "reserved":
        saved = load_generation_result(client_request_id)
        if saved is not None:
            if saved.status != "unknown" and saved.path is not None and not saved.path.is_file():
                raise ToolError("原图片文件已缺失，请核对原任务文件；不会自动付费重生成")
            if saved.status != "unknown":
                return saved
        if reservation.status == "released":
            raise BudgetError("原请求未提交且预算已释放；请重新确认费用后创建新生成请求")
        if reservation.provider_job_id is not None:
            event.bind(provider_task_id=reservation.provider_job_id).info("poll existing task")
            return reconcile_image(client_request_id, provider=provider)
        event.warning("needs_reconciliation provider_task_id missing status={}", reservation.status)
        return ImageGenerationResult(
            path=None,
            provider_job_id=None,
            status="unknown",
            actual_fen=None,
            error="NEEDS_RECONCILIATION：原任务缺少供应商任务 ID，请人工查账；不会重新提交",
        )

    if not claim_submission(client_request_id):
        event.info("submit already claimed by another worker")
        return ImageGenerationResult(
            status="unknown", error="该请求已由另一执行者领取，请查询原任务"
        )
    try:
        event.info("submit once")
        provider_job_id = provider.submit(
            prompt=prompt,
            shot_no=shot_no,
            client_request_id=client_request_id,
            reference_urls=reference_urls,
            seed=seed,
        )
        if not isinstance(provider_job_id, str) or not provider_job_id.strip():
            raise ToolError("供应商未返回任务 ID")
        provider_job_id = provider_job_id.strip()
        mark_submitted(client_request_id, provider_job_id=provider_job_id)
        event.bind(provider_task_id=provider_job_id).info("submitted")
    except (Exception, KeyboardInterrupt) as error:
        mark_outcome(client_request_id, "unknown")
        event.warning("submit outcome unknown exception={}", type(error).__name__)
        result = _unknown_result(None, error)
        save_generation_result(client_request_id, result)
        if isinstance(error, KeyboardInterrupt):
            raise
        return result

    try:
        event.bind(provider_task_id=provider_job_id).info("poll")
        result = provider.query(provider_job_id)
    except (Exception, KeyboardInterrupt) as error:
        mark_outcome(
            client_request_id,
            "unknown",
            provider_job_id=provider_job_id,
        )
        result = _unknown_result(provider_job_id, error)
        event.bind(provider_task_id=provider_job_id).warning(
            "poll outcome unknown exception={}", type(error).__name__
        )
        save_generation_result(client_request_id, result)
        if isinstance(error, KeyboardInterrupt):
            raise
        return result
    return _record_result(client_request_id, provider_job_id, result)


def reconcile_image(
    client_request_id: str,
    *,
    provider: ImageProvider,
) -> ImageGenerationResult:
    """只查询既有任务并结算，不产生新的付费提交。"""
    reservation = get_reservation(client_request_id)
    if reservation is None:
        raise BudgetError("找不到需要对账的生图任务")
    if reservation.model != provider.model_id:
        raise BudgetError("当前生图模型与原任务不一致，请恢复原模型配置后查询")
    saved = load_generation_result(client_request_id)
    if saved is not None and saved.status != "unknown":
        return saved
    if reservation.status == "settled":
        raise BudgetError("生图任务已经结算")
    if reservation.status == "released":
        raise BudgetError("生图任务预算已经释放")
    if reservation.provider_job_id is None:
        logger.bind(component="image-generation", generation_request_id=client_request_id).warning(
            "needs_reconciliation provider_task_id missing status={}", reservation.status
        )
        return ImageGenerationResult(
            path=None,
            provider_job_id=None,
            status="unknown",
            actual_fen=None,
            error="NEEDS_RECONCILIATION：供应商任务 ID 未返回，需要人工查账",
        )
    try:
        logger.bind(component="image-generation", generation_request_id=client_request_id,
                    provider_task_id=reservation.provider_job_id).info("poll existing task")
        result = provider.query(reservation.provider_job_id)
    except (Exception, KeyboardInterrupt) as error:
        result = _unknown_result(reservation.provider_job_id, error)
        save_generation_result(client_request_id, result)
        if isinstance(error, KeyboardInterrupt):
            raise
        return result
    return _record_result(client_request_id, reservation.provider_job_id, result)


def release_failed_image(client_request_id: str) -> None:
    """供应商已明确确认失败且不扣费后，释放对应预占。"""
    release(client_request_id)
