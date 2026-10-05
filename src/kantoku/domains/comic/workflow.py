"""Comic Generate → QC → Approval → Archive/Rework 工作流。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from typing import Any

from loguru import logger

from kantoku.capabilities.video import VideoGenerationRequest, VideoService
from kantoku.config import ExternalJobPending, ToolError, get_settings
from kantoku.config.logging_setup import redact_secrets
from kantoku.config.observability import public_error
from kantoku.core.budget import attach_image_artifact, load_generation_result
from kantoku.core.conversations import MessageRole, MessageType
from kantoku.core.runtime.graph import (
    END,
    START,
    ConditionalEdge,
    RuntimeContext,
    WorkflowDefinition,
    WorkflowNode,
)
from kantoku.core.runtime.models import ApprovalDecision, ArtifactType, RuntimeEventType, utc_now

from .models import ComicState
from .services import ComicWorkflowServices, StudioComicServices

WORKFLOW_ID = "comic.production.v1"


def build_comic_workflow(
    service: ComicWorkflowServices,
    *,
    video_service: VideoService | None = None,
    video_enabled: bool = False,
    creation_step: Callable[[str, ComicState, RuntimeContext], dict[str, Any]] | None = None,
    director_review_request: Callable[[ComicState], dict[str, Any]] | None = None,
) -> WorkflowDefinition[ComicState]:
    """构建复用现有生产能力的 Comic Workflow。"""

    def call(name: str, state: ComicState, _context: RuntimeContext) -> dict[str, Any]:
        method = getattr(service, name)
        return dict(method(state))

    def plan(name: str, state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        if creation_step is None:
            raise RuntimeError("Comic creative planning is not configured")
        return creation_step(name, state, context)

    def review(state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        decision = context.approval_decision
        if (state.quick_creation or {}).get("auto_create_image") and not state.qc_passed:
            raise ToolError("图片已生成，但视觉检查未通过；请在专业工作区查看检查结果")
        if decision is None and state.execution_mode == "fast" and state.qc_passed:
            # Automatic QC pass is not a human review; do not record a fake label.
            return {"approval_decision": ApprovalDecision.APPROVE.value}
        if decision is None:
            raise RuntimeError("approval decision is missing")
        return dict(service.review(state, decision.value, context.approval_response))

    def approve_cost(state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        if (state.quick_creation or {}).get("auto_create_image") and not state.confirmed:
            raise ToolError(
                "快速创作费用无法安全自动执行；请核实模型价格或使用专业模式",
                detail=f"gate=cost_approval estimate_fen={state.total_estimate_fen} "
                f"limit_cny={get_settings().budget.autonomous_image_auto_cny} "
                f"unpriced_models={state.quick_creation.get('unpriced_models', [])}",
            )
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
            conversation_id=state.conversation_id,
            node_id=context.node_id,
            source="comic.archive",
            location=update.get("archive_path"),
            metadata={
                "request_id": state.request_id,
                "domain": "comic",
                **({"project_id": state.project,
                    "shot_id": state.quick_creation.get("shot_id"),
                    "storyboard_id": state.quick_creation.get("storyboard_id"),
                    "prompt_artifact_id": state.quick_creation.get("prompt_artifact_id"),
                    "prompt_version": state.quick_creation.get("prompt_version")}
                   if (state.quick_creation or {}).get("use_confirmed_director")
                   or (state.quick_creation or {}).get("use_existing_director") else {}),
                **({"origin": "real"} if isinstance(service, StudioComicServices) else {}),
            },
        )
        update["image_artifact_id"] = artifact.id
        logger.bind(run_id=context.run_id, task_id=state.request_id,
                    trace_id=state.trace_id, artifact_id=artifact.id).info("artifact_saved")
        logger.bind(run_id=context.run_id, task_id=state.request_id,
                    trace_id=state.trace_id, artifact_id=artifact.id).info(
            "image_generation_completed artifact_id={}", artifact.id,
        )
        if isinstance(service, StudioComicServices) and state.request_id is not None:
            attach_image_artifact(state.request_id, artifact.id)
        if state.conversation_id:
            context.store.add_conversation_message(
                state.conversation_id,
                role=MessageRole.ASSISTANT,
                type=MessageType.ARTIFACT,
                content="已生成当前镜头图片。",
                run_id=context.run_id,
                event_id=f"quick-image:{context.run_id}",
                artifact_id=artifact.id,
            )
        return update

    def video(state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        if video_service is None or state.image_artifact_id is None:
            raise RuntimeError("video capability is not configured")
        artifact = video_service.generate(
            VideoGenerationRequest(
                request_id=f"video-{context.run_id}-{state.request_id}",
                run_id=context.run_id,
                node_id=context.node_id,
                image_artifact_id=state.image_artifact_id,
                prompt=state.prompt,
                project=state.project,
                shot_no=state.shot_no,
                config={"optional": True},
            )
        )
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

    def workspace_observed(handler: Callable) -> Callable:
        """Observe the existing nodes, without changing Home or execution policy."""
        def execute(state: ComicState, context: RuntimeContext) -> dict[str, Any]:
            creation = state.quick_creation or {}
            if not (creation.get("use_confirmed_director")
                    or creation.get("use_existing_director")):
                return handler(state, context)
            identity = (service.provider.generation_identity()
                        if isinstance(service, StudioComicServices) else {})
            fields = {
                "project_id": state.project, "run_id": context.run_id,
                "shot_id": creation.get("shot_id"), "task_id": state.request_id,
                "trace_id": state.trace_id, "stage": context.node_id,
                "provider": identity.get("provider", get_settings().image.provider),
                "model": (service.provider.model_id if isinstance(service, StudioComicServices)
                          else get_settings().image.model),
                "director_version": creation.get("director_spec_version"),
                "prompt_version": creation.get("prompt_version"),
                "prompt_hash": sha256(state.prompt.encode()).hexdigest(),
            }

            def emit(kind: str, **extra: Any) -> None:
                payload = {**fields, **extra, "kind": kind}
                logger.bind(**payload).info(
                    "{} stage={} shot_id={} artifact_id={} actual_fen={} duration_seconds={}",
                    kind, context.node_id, payload.get("shot_id"), payload.get("artifact_id"),
                    payload.get("actual_fen"), payload.get("duration_seconds"),
                )
                context.store.append_event(context.run_id, RuntimeEventType.NODE_PROGRESS,
                                           node_id=context.node_id, payload=payload)

            emit({"storyboard": "storyboard_selected" if creation.get("shot_id")
                  else "storyboard_generation_started",
                  "prompt": "prompt_compilation_started",
                  "generate": "COMIC_IMAGE_GENERATION_STARTED",
                  "archive": "comic_artifact_save_started"}.get(
                      context.node_id, "comic_production_stage_started"))
            if context.node_id == "storyboard":
                emit("shot_selected" if creation.get("shot_id") else "shot_generation_started")
            try:
                update = dict(handler(state, context))
            except ExternalJobPending:
                # A pending/unknown bill is not a zero-cost failure or permission to retry.
                emit("comic_image_generation_pending", billing_status="unresolved")
                raise
            except Exception as error:
                failure = public_error(error, component="comic-production", **fields)
                result = load_generation_result(state.request_id) if state.request_id else None
                payload = {**fields, **failure, "kind": "COMIC_IMAGE_GENERATION_FAILED",
                           "error": failure["safe_message"],
                           "provider_response": redact_secrets(result.error or "")
                           if result else None,
                           "actual_fen": result.actual_fen if result else None}
                logger.bind(**payload).error(
                    "COMIC_IMAGE_GENERATION_FAILED stage={} shot_id={} error={} "
                    "provider_response={} actual_fen={}", context.node_id, fields["shot_id"],
                    failure["safe_message"], payload["provider_response"], payload["actual_fen"],
                )
                context.store.append_event(context.run_id, RuntimeEventType.NODE_PROGRESS,
                                           node_id=context.node_id, payload=payload)
                raise
            if context.node_id == "storyboard":
                emit("shot_ready",
                     shot_id=update.get("quick_creation", creation).get("shot_id"))
            if context.node_id == "archive":
                result = load_generation_result(state.request_id) if state.request_id else None
                run = context.store.get_run(context.run_id)
                emit("COMIC_IMAGE_GENERATION_COMPLETED", artifact_id=update["image_artifact_id"],
                     actual_fen=result.actual_fen if result else None,
                     duration_seconds=max(0, (utc_now() - run.started_at).total_seconds()))
            return update
        return execute

    nodes = {
        "director": WorkflowNode("director", lambda s, c: plan("director", s, c)),
        "director_gate": WorkflowNode(
            "director_gate", lambda s, c: plan("director_gate", s, c),
            requires_approval=True,
            approval_when=lambda state: bool(state.quick_creation)
            and state.quick_creation.get("director_status") != "failed"
            and not state.quick_creation.get("auto_create_image")
            and not state.quick_creation.get("use_confirmed_director")
            and not (state.quick_creation.get("use_existing_director")
                     and state.quick_creation.get("approval_required") is False),
            approval_request=director_review_request or (lambda state: {
                "kind": "director_review",
                "director_version": (state.quick_creation or {}).get("director_spec_version"),
                "ready": (state.quick_creation or {}).get("director_status") == "completed",
                "message": "导演方案已整理。确认后将生成当前画面，也可以先补充修改方向。",
            }),
        ),
        "storyboard": WorkflowNode("storyboard", lambda s, c: plan("storyboard", s, c)),
        "prompt": WorkflowNode("prompt", lambda s, c: plan("prompt", s, c)),
        "prepare": WorkflowNode("prepare", lambda s, c: call("prepare", s, c)),
        "cost_approval": WorkflowNode(
            "cost_approval",
            approve_cost,
            requires_approval=True,
            approval_when=lambda state: not state.confirmed
            and not (state.quick_creation or {}).get("auto_create_image"),
            approval_request=lambda state: {
                "kind": "cost_approval",
                "estimate_fen": state.total_estimate_fen or state.estimate_fen,
                "unit_fen": state.estimate_fen,
                "image_count": state.image_count,
                "total_fen": state.total_estimate_fen or state.estimate_fen * state.image_count,
                "project": state.project,
                "provider": get_settings().image.provider,
                "unpriced_models": (state.quick_creation or {}).get("unpriced_models", []),
                "message": (
                    "部分模型尚未配置价格，当前报价仅包含已知成本；请先核实费用再批准。"
                    if (state.quick_creation or {}).get("unpriced_models")
                    else "批准后才会调用付费生图服务。"
                    if state.image_count <= 1
                    else f"识别到 {state.image_count} 张需求；当前每次任务生成 1 张，"
                    "批准后才会调用付费生图服务，剩余张数将在后续任务中逐张确认。"
                ),
            },
        ),
        "generate": WorkflowNode("generate", lambda s, c: call("generate", s, c), retry_limit=1),
        "qc": WorkflowNode("qc", lambda s, c: call("qc", s, c), retry_limit=1),
        "human_review": WorkflowNode(
            "human_review",
            review,
            requires_approval=True,
            approval_when=lambda state: (state.execution_mode != "fast" or not state.qc_passed)
            and not (state.quick_creation or {}).get("auto_create_image"),
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
    nodes = {key: replace(node, handler=workspace_observed(node.handler))
             for key, node in nodes.items()}
    edges = {
        START: ConditionalEdge(
            lambda state: "quick" if state.quick_creation else "legacy",
            {"quick": "cost_approval", "legacy": "prepare"},
        ),
        "director": "director_gate",
        "director_gate": ConditionalEdge(
            lambda state: (state.quick_creation or {}).get("director_decision", "approve"),
            {"approve": "storyboard", "revise": "director_gate", "reject": END},
        ),
        "storyboard": "prompt",
        "prompt": "prepare",
        "prepare": ConditionalEdge(
            lambda state: "quick" if state.quick_creation else "legacy",
            {"quick": "generate", "legacy": "cost_approval"},
        ),
        "cost_approval": ConditionalEdge(
            lambda state: (
                ("quick" if state.quick_creation else "approve")
                if state.cost_decision in {None, "approve"}
                else "reject"
            ),
            {"approve": "generate", "quick": "director", "reject": END},
        ),
        "generate": "qc",
        "qc": "human_review",
        "human_review": ConditionalEdge(
            approval_route,
            {"approve": "archive", "revise": "rework", "reject": END},
        ),
        "rework": "generate",
        "archive": ConditionalEdge(
            lambda state: "enabled" if video_enabled and not state.quick_creation else "disabled",
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
