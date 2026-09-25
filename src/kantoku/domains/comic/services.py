"""Comic 能力适配器：复用现有真实 Studio、QC、Review 与 Archive。"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from kantoku.config import ExternalJobPending, ToolError
from kantoku.core import budget
from kantoku.perception.qc import qc_image
from kantoku.perception.review import (
    build_rework_plan,
    decide_rework,
    get_rework_item,
    load_qc_prediction,
    record_human_review,
    record_qc_prediction,
)
from kantoku.schemas.qc import HumanQcLabel, QcResult
from kantoku.tools.archive import archive_reviewed_image
from kantoku.tools.image_gen import ImageProvider
from kantoku.tools.studio import create_task, execute_task, list_tasks

from .models import ComicState


class ComicWorkflowServices(Protocol):
    """Runtime 所需的 Comic 领域能力端口。"""

    def prepare(self, state: ComicState) -> Mapping[str, Any]: ...

    def generate(self, state: ComicState) -> Mapping[str, Any]: ...

    def qc(self, state: ComicState) -> Mapping[str, Any]: ...

    def review(
        self, state: ComicState, decision: str, response: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def rework(self, state: ComicState) -> Mapping[str, Any]: ...

    def archive(self, state: ComicState) -> Mapping[str, Any]: ...


class StudioComicServices:
    """现有真实能力的生产适配器；付费生成仍由预算控制器保护。"""

    def __init__(self, provider: ImageProvider) -> None:
        self.provider = provider

    def prepare(self, state: ComicState) -> Mapping[str, Any]:
        """付费前先保存不可变任务。"""
        task = create_task(
            state.project, state.prompt, state.shot_no, state.estimate_fen,
            request_id=state.request_id,
        )
        return {"request_id": task.request_id}

    def generate(self, state: ComicState) -> Mapping[str, Any]:
        """调用现有幂等生图入口；失败时释放未提交的预占，避免泄漏占用预算。"""
        if state.request_id is None:
            raise ToolError("Comic Run 尚未准备生成任务")
        task = next((item for item in list_tasks() if item.request_id == state.request_id), None)
        if task is None:
            raise ToolError("找不到 Comic 生成任务", detail=state.request_id)
        try:
            result = execute_task(task, provider=self.provider, confirmed=state.confirmed)
        except Exception:
            reservation = budget.get_reservation(state.request_id)
            if reservation is not None and reservation.status == "reserved":
                budget.release(state.request_id)
            raise
        if result.path is None:
            reason = (result.error or "供应商未返回图片，请稍后查询原任务").strip()
            if result.status == "unknown":
                raise ExternalJobPending(
                    reason[:120],
                    needs_reconciliation="NEEDS_RECONCILIATION" in reason,
                )
            raise ToolError(f"图片生成未完成：{reason[:120]}")
        return {
            "image_path": str(result.path),
            "provider_job_id": result.provider_job_id,
        }

    def qc(self, state: ComicState) -> Mapping[str, Any]:
        """调用现有 VLM QC 并持久化预测。"""
        if state.request_id is None or state.image_path is None:
            raise ToolError("Comic Run 尚未生成图片")
        existing = load_qc_prediction(state.request_id)
        result = existing or qc_image(
            Path(state.image_path),
            target_platform=state.target_platform,
            genre=state.genre,
            target_audience=state.target_audience,
            cinematography_requirements=state.cinematography_requirements,
            key_message=state.prompt,
            visual_style=state.visual_style,
            confirm_paid=state.confirmed,
        )
        if existing is None:
            record_qc_prediction(state.request_id, result)
        passed = not result.broken_hands and not result.watermark and result.composition_ok
        return {"qc_result": result.model_dump(mode="json"), "qc_passed": passed}

    def review(
        self, state: ComicState, decision: str, response: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """把通用 Approval 适配为现有不可变人工 QC 记录。"""
        if state.request_id is None or state.image_path is None or state.qc_result is None:
            raise ToolError("Comic Run 缺少人工终审所需资料")
        approved = decision == "approve"
        failures = list(response.get("failure_reasons", []))
        if not approved and not failures:
            failures = ["other"]
        try:
            result = QcResult.model_validate(state.qc_result)
            label = HumanQcLabel.model_validate({
                "id": f"review-{state.request_id}",
                "image_path": Path(state.image_path),
                "target_platform": state.target_platform,
                "genre": state.genre,
                "target_audience": state.target_audience,
                "key_message": state.prompt,
                "visual_style": state.visual_style,
                "cinematography_requirements": state.cinematography_requirements,
                "cinematography_notes": str(response.get("notes", result.reason)),
                "review_seconds": response.get("review_seconds"),
                "result": result,
                "approved": approved,
                "failure_reasons": failures,
            })
        except ValidationError as error:
            raise ToolError("Comic 人工审批信息与 QC 结果冲突") from error
        record_human_review(state.request_id, label)
        if decision == "reject":
            decide_rework(state.request_id, "cancelled")
        return {"approval_decision": decision}

    def rework(self, state: ComicState) -> Mapping[str, Any]:
        """从现有返工队列生成下一版任务，但不在此节点付费。"""
        if state.request_id is None:
            raise ToolError("Comic Run 缺少原请求 ID")
        item = get_rework_item(state.request_id)
        if item is None:
            raise ToolError("人工修改请求没有生成返工项")
        plan = build_rework_plan(item)
        prompt = "\n".join([
            state.prompt,
            "定向返工（只修改已确认的问题）：",
            *plan.correction_directives,
            "必须保留：",
            *plan.preserve_constraints,
        ])
        target_id = f"studio-{secrets.token_hex(16)}"
        task = create_task(
            state.project, prompt, state.shot_no, state.estimate_fen,
            request_id=target_id,
        )
        decide_rework(state.request_id, "approved", target_request_id=target_id)
        return {
            "request_id": task.request_id,
            "prompt": prompt,
            "image_path": None,
            "provider_job_id": None,
            "qc_result": None,
            "qc_passed": None,
            "approval_decision": None,
            "rework_count": state.rework_count + 1,
        }

    def archive(self, state: ComicState) -> Mapping[str, Any]:
        """调用现有图片归档。"""
        if state.request_id is None:
            raise ToolError("Comic Run 缺少归档请求 ID")
        if state.execution_mode == "fast" and state.qc_passed and state.image_path:
            source = Path(state.image_path)
            if not source.is_file():
                raise ToolError("快速创作图片文件缺失，不能创建 Artifact")
            return {"archive_path": str(source)}
        archived = archive_reviewed_image(state.request_id)
        return {"archive_path": str(archived.image_path)}
