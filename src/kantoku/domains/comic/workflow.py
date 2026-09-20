"""Comic Generate → QC → Approval → Archive/Rework 工作流。"""

from __future__ import annotations

from typing import Any

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
from .services import ComicWorkflowServices

WORKFLOW_ID = "comic.production.v1"


def build_comic_workflow(service: ComicWorkflowServices) -> WorkflowDefinition[ComicState]:
    """构建复用现有生产能力的 Comic Workflow。"""

    def call(name: str, state: ComicState, _context: RuntimeContext) -> dict[str, Any]:
        method = getattr(service, name)
        return dict(method(state))

    def review(state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        decision = context.approval_decision
        if decision is None:
            raise RuntimeError("approval decision is missing")
        return dict(service.review(state, decision.value, context.approval_response))

    def archive(state: ComicState, context: RuntimeContext) -> dict[str, Any]:
        update = dict(service.archive(state))
        context.store.create_artifact(
            type=ArtifactType.IMAGE,
            run_id=context.run_id,
            node_id=context.node_id,
            source="comic.archive",
            location=update.get("archive_path"),
            metadata={"request_id": state.request_id, "domain": "comic"},
        )
        return update

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
        "generate": WorkflowNode(
            "generate", lambda s, c: call("generate", s, c), retry_limit=1
        ),
        "qc": WorkflowNode("qc", lambda s, c: call("qc", s, c), retry_limit=1),
        "human_review": WorkflowNode(
            "human_review",
            review,
            requires_approval=True,
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
    }
    edges = {
        START: "prepare",
        "prepare": "generate",
        "generate": "qc",
        "qc": "human_review",
        "human_review": ConditionalEdge(
            approval_route,
            {"approve": "archive", "revise": "rework", "reject": END},
        ),
        "rework": "generate",
        "archive": END,
    }
    return WorkflowDefinition(
        id=WORKFLOW_ID,
        domain="comic",
        state_type=ComicState,
        nodes=nodes,
        edges=edges,
        services={"comic": service},
    )

