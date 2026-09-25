"""Comic Generate → QC → Approval → Archive/Rework 工作流。"""

from __future__ import annotations

from typing import Any

from kantoku.capabilities.video import VideoGenerationRequest, VideoService
from kantoku.config import get_settings
from kantoku.core.budget import attach_image_artifact
from kantoku.core.runtime.graph import (
    END,
    START,
    ConditionalEdge,
    RuntimeContext,
    WorkflowDefinition,
    WorkflowNode,
)
from kantoku.core.runtime.models import ApprovalDecision, ArtifactType

from .models import ComicState
from .services import ComicWorkflowServices, StudioComicServices

WORKFLOW_ID = "comic.production.v1"


def build_comic_workflow(
    service: ComicWorkflowServices,
    *,
    video_service: VideoService | None = None,
    video_enabled: bool = False,
) -> WorkflowDefinition[ComicState]:
    """构建复用现有生产能力的 Comic Workflow。"""

    def call(name: str, state: ComicState, _context: RuntimeContext) -> dict[str, Any]:
        method = getattr(service, name)
        return dict(method(state))

    def review(state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        decision = context.approval_decision
        if decision is None and state.execution_mode == "fast" and state.qc_passed:
            # Automatic QC pass is not a human review; do not record a fake label.
            return {"approval_decision": ApprovalDecision.APPROVE.value}
        if decision is None:
            raise RuntimeError("approval decision is missing")
        return dict(service.review(state, decision.value, context.approval_response))

    def approve_cost(_state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        decision = context.approval_decision
        if decision is None:
            return {"confirmed": True, "cost_decision": "approve"}
        return {
            "confirmed": decision is ApprovalDecision.APPROVE,
            "cost_decision": decision.value,
        }

    def archive(state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        update = dict(service.archive(state))
        artifact = context.store.create_artifact(
            type=ArtifactType.IMAGE,
            run_id=context.run_id,
            node_id=context.node_id,
            source="comic.archive",
            location=update.get("archive_path"),
            metadata={"request_id": state.request_id, "domain": "comic"},
        )
        update["image_artifact_id"] = artifact.id
        if isinstance(service, StudioComicServices) and state.request_id is not None:
            attach_image_artifact(state.request_id, artifact.id)
        return update

    def video(state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        if video_service is None or state.image_artifact_id is None:
            raise RuntimeError("video capability is not configured")
        artifact = video_service.generate(VideoGenerationRequest(
            request_id=f"video-{context.run_id}-{state.request_id}",
            run_id=context.run_id,
            node_id=context.node_id,
            image_artifact_id=state.image_artifact_id,
            prompt=state.prompt,
            project=state.project,
            shot_no=state.shot_no,
            config={"optional": True},
        ))
        return {"video_artifact_id": artifact.id}

    def approval_route(state: ComicState) -> str:
        if state.approval_decision == ApprovalDecision.APPROVE:
            return "approve"
        if (
            state.approval_decision == ApprovalDecision.REQUEST_REVISION
            and state.rework_count < state.max_reworks
        ):
            return "revise"
        return "reject"

    nodes = {
        "prepare": WorkflowNode("prepare", lambda s, c: call("prepare", s, c)),
        "cost_approval": WorkflowNode(
            "cost_approval", approve_cost, requires_approval=True,
            approval_when=lambda state: not state.confirmed,
            approval_request=lambda state: {
                "kind": "cost_approval", "estimate_fen": state.estimate_fen,
                "unit_fen": state.estimate_fen,
                "image_count": state.image_count,
                "total_fen": state.estimate_fen * state.image_count,
                "project": state.project, "provider": get_settings().image.provider,
                "message": (
                    "批准后才会调用付费生图服务。"
                    if state.image_count <= 1
                    else f"识别到 {state.image_count} 张需求；当前每次任务生成 1 张，"
                         "批准后才会调用付费生图服务，剩余张数将在后续任务中逐张确认。"
                ),
            },
        ),
        "generate": WorkflowNode(
            "generate", lambda s, c: call("generate", s, c), retry_limit=1
        ),
        "qc": WorkflowNode("qc", lambda s, c: call("qc", s, c), retry_limit=1),
        "human_review": WorkflowNode(
            "human_review",
            review,
            requires_approval=True,
            approval_when=lambda state: (
                state.execution_mode != "fast" or not state.qc_passed
            ),
            approval_request=lambda state: {
                "kind": "creative_review",
                "request_id": state.request_id,
                "image_path": state.image_path,
                "qc": state.qc_result,
                "revision": state.rework_count,
            },
        ),
        "rework": WorkflowNode("rework", lambda s, c: call("rework", s, c)),
        "archive": WorkflowNode("archive", archive),
        "video": WorkflowNode("video", video),
    }
    edges = {
        START: "prepare",
        "prepare": "cost_approval",
        "cost_approval": ConditionalEdge(
            lambda state: "approve" if state.cost_decision in {None, "approve"} else "reject",
            {"approve": "generate", "reject": END},
        ),
        "generate": "qc",
        "qc": "human_review",
        "human_review": ConditionalEdge(
            approval_route,
            {"approve": "archive", "revise": "rework", "reject": END},
        ),
        "rework": "generate",
        "archive": ConditionalEdge(
            lambda _state: "enabled" if video_enabled else "disabled",
            {"enabled": "video", "disabled": END},
        ),
        "video": END,
    }
    return WorkflowDefinition(
        id=WORKFLOW_ID,
        domain="comic",
        state_type=ComicState,
        nodes=nodes,
        edges=edges,
        services={"comic": service},
    )
