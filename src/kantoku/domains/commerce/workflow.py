"""明确标记为 Mock 的 Commerce 验证工作流。"""

from __future__ import annotations

from kantoku.core.runtime.graph import (
    END,
    START,
    ConditionalEdge,
    RuntimeContext,
    WorkflowDefinition,
    WorkflowNode,
)
from kantoku.core.runtime.models import ArtifactType

from .models import CommerceState

WORKFLOW_ID = "commerce.mock.v1"


def build_commerce_workflow() -> WorkflowDefinition[CommerceState]:
    """构建不连接 Ozon、1688 或任何付费 API 的 Mock Workflow。"""

    def research(state: CommerceState, _context: RuntimeContext) -> dict[str, object]:
        return {"research": [{
            "title": f"Mock candidate for {state.requirement}",
            "source": "mock",
            "mock": True,
        }]}

    def supplier(_state: CommerceState, _context: RuntimeContext) -> dict[str, object]:
        return {"supplier": {"name": "Mock Supplier", "source": "mock", "mock": True}}

    def approval(_state: CommerceState, context: RuntimeContext) -> dict[str, object]:
        if context.approval_decision is None:
            raise RuntimeError("approval decision is missing")
        return {"approval_decision": context.approval_decision.value}

    def image(state: CommerceState, context: RuntimeContext) -> dict[str, object]:
        location = f"mock://commerce/{context.run_id}/product-image.png"
        context.store.create_artifact(
            type=ArtifactType.IMAGE,
            run_id=context.run_id,
            node_id=context.node_id,
            source="commerce.mock",
            location=location,
            metadata={"mock": True, "requirement": state.requirement},
        )
        return {"product_image": location}

    nodes = {
        "mock_product_research": WorkflowNode("mock_product_research", research),
        "mock_supplier": WorkflowNode("mock_supplier", supplier),
        "human_approval": WorkflowNode(
            "human_approval",
            approval,
            requires_approval=True,
            approval_request=lambda state: {
                "kind": "commerce_mock_review",
                "mock": True,
                "requirement": state.requirement,
                "research": state.research,
                "supplier": state.supplier,
            },
        ),
        "generate_product_image": WorkflowNode("generate_product_image", image),
        "qc": WorkflowNode(
            "qc", lambda _state, _context: {
                "qc_result": {"status": "mock_passed", "mock": True}
            }
        ),
    }
    edges = {
        START: "mock_product_research",
        "mock_product_research": "mock_supplier",
        "mock_supplier": "human_approval",
        "human_approval": ConditionalEdge(
            lambda state: state.approval_decision or "reject",
            {"approve": "generate_product_image", "reject": END,
             "request_revision": END},
        ),
        "generate_product_image": "qc",
        "qc": END,
    }
    return WorkflowDefinition(
        id=WORKFLOW_ID,
        domain="commerce",
        state_type=CommerceState,
        nodes=nodes,
        edges=edges,
        services={"mock": True},
    )

