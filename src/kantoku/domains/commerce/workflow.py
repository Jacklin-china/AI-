"""Commerce Pack v0.1 完整工作流。"""

from __future__ import annotations

from collections.abc import Callable
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
from kantoku.core.skills import SkillRegistry

from .models import Candidate, CommerceState
from .ports import MarketplacePort, SourcePort

WORKFLOW_ID = "commerce.production.v1"
Translator = Callable[[dict[str, Any], str], dict[str, Any]]
QcChecker = Callable[[CommerceState], bool]


def build_commerce_workflow(
    source: SourcePort,
    marketplace: MarketplacePort,
    skills: SkillRegistry,
    translator: Translator,
    qc_checker: QcChecker | None = None,
) -> WorkflowDefinition[CommerceState]:
    """构建与 Adapter 实现无关的 Commerce Workflow。"""

    def artifact(
        context: RuntimeContext,
        type: ArtifactType,
        source_name: str,
        payload: Any,
        *,
        location: str | None = None,
    ) -> None:
        context.store.create_artifact(
            type=type,
            run_id=context.run_id,
            node_id=context.node_id,
            source=source_name,
            location=location,
            metadata={"payload": payload, "mock": True},
        )

    def requirement(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        artifact(context, ArtifactType.DOCUMENT, "commerce.requirement", {
            "requirement": state.requirement, "locale": state.locale,
        })
        return {}

    def source_search(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        candidates = [item.model_dump(mode="json") for item in source.fetch(state.requirement)]
        artifact(context, ArtifactType.JSON, "commerce.source.mock", candidates)
        return {"candidates": candidates, "candidate_decision": None}

    def candidate_analysis(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        ordered = sorted(state.candidates, key=lambda item: int(item["cost_fen"]))
        analysis = {
            "recommended_id": ordered[0]["id"],
            "candidate_count": len(ordered),
            "basis": "lowest_mock_cost",
            "mock": True,
        }
        artifact(context, ArtifactType.REPORT, "commerce.analysis", analysis)
        return {"analysis": analysis}

    def candidate_approval(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        decision = context.approval_decision
        if decision is None:
            raise RuntimeError("candidate approval decision is missing")
        update: dict[str, Any] = {"candidate_decision": decision.value}
        if decision is ApprovalDecision.APPROVE:
            selected = context.approval_response.get("candidate_id")
            update["selected_candidate_id"] = selected or state.analysis["recommended_id"]
        elif decision is ApprovalDecision.REQUEST_REVISION:
            update["candidate_revision"] = state.candidate_revision + 1
        return update

    def sku_selection(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        candidate = _candidate(state)
        sku = candidate.skus[0]
        artifact(context, ArtifactType.JSON, "commerce.sku", {
            "candidate_id": candidate.id, "sku": sku, "mock": True,
        })
        return {"selected_sku": sku}

    def pricing(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        candidate = _candidate(state)
        result = skills.execute(
            "commerce.calculate_pricing",
            {"cost_fen": candidate.cost_fen, "margin_rate": 0.25,
             "marketplace_fee_rate": 0.15},
            {}, store=context.store, run_id=context.run_id, node_id=context.node_id,
        )
        result["mock"] = True
        artifact(context, ArtifactType.REPORT, "commerce.pricing", result)
        return {"pricing": result}

    def listing_draft(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        candidate = _candidate(state)
        listing = {
            "title": candidate.title,
            "description": f"Mock listing for {state.requirement}",
            "sku": state.selected_sku,
            "price_fen": state.pricing["price_fen"],
            "mock": True,
            "revision": state.publish_revision,
        }
        artifact(context, ArtifactType.LISTING, "commerce.listing", listing)
        return {"listing": listing, "publish_decision": None}

    def localize(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        result = skills.execute(
            "commerce.localize_listing",
            {"listing": state.listing, "locale": state.locale},
            {"translator": translator}, store=context.store,
            run_id=context.run_id, node_id=context.node_id,
        )
        localized = dict(result["localized_listing"])
        localized["mock"] = True
        artifact(context, ArtifactType.LISTING, "commerce.localization", {
            "locale": state.locale, "listing": localized,
        })
        return {"localized_listing": localized}

    def asset_generation(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        item = context.store.create_artifact(
            type=ArtifactType.IMAGE,
            run_id=context.run_id,
            node_id=context.node_id,
            source="commerce.image.mock",
            location=f"mock://commerce/{context.run_id}/main-{state.rework_count}.png",
            metadata={"mock": True, "rework_count": state.rework_count},
            version=state.rework_count + 1,
        )
        return {"asset_artifact_id": item.id}

    def qc(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        passed = (qc_checker or (lambda _state: True))(state)
        report = {
            "passed": passed,
            "mock": True,
            "asset_artifact_id": state.asset_artifact_id,
            "rework_count": state.rework_count,
        }
        artifact(context, ArtifactType.REPORT, "commerce.qc.mock", report)
        return {"qc_result": report, "qc_passed": passed}

    def rework(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        count = state.rework_count + 1
        artifact(context, ArtifactType.REPORT, "commerce.rework", {
            "from": state.rework_count, "to": count, "mock": True,
        })
        return {
            "rework_count": count,
            "asset_artifact_id": None,
            "qc_result": None,
            "qc_passed": None,
        }

    def publish_approval(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        decision = context.approval_decision
        if decision is None:
            raise RuntimeError("publish approval decision is missing")
        update: dict[str, Any] = {"publish_decision": decision.value}
        if decision is ApprovalDecision.REQUEST_REVISION:
            update["publish_revision"] = state.publish_revision + 1
        return update

    def create_marketplace_draft(
        state: CommerceState, context: RuntimeContext
    ) -> dict[str, Any]:
        draft = marketplace.create_draft(state.localized_listing or {})
        payload = draft.model_dump(mode="json")
        artifact(context, ArtifactType.LISTING, "commerce.marketplace.mock", payload)
        return {"marketplace_draft": payload}

    def publish_draft(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        draft = marketplace.submit_draft(str(state.marketplace_draft["id"]))
        payload = draft.model_dump(mode="json")
        artifact(context, ArtifactType.REPORT, "commerce.publish.mock", payload)
        return {"marketplace_draft": payload}

    nodes = {
        "requirement": WorkflowNode("requirement", requirement),
        "source_search": WorkflowNode("source_search", source_search),
        "candidate_analysis": WorkflowNode("candidate_analysis", candidate_analysis),
        "candidate_approval": WorkflowNode(
            "candidate_approval", candidate_approval, requires_approval=True,
            approval_request=lambda state: {
                "kind": "candidate_approval", "candidates": state.candidates,
                "analysis": state.analysis, "revision": state.candidate_revision,
                "mock": True,
            },
        ),
        "sku_selection": WorkflowNode("sku_selection", sku_selection),
        "pricing": WorkflowNode("pricing", pricing),
        "listing_draft": WorkflowNode("listing_draft", listing_draft),
        "localize": WorkflowNode("localize", localize),
        "asset_generation": WorkflowNode("asset_generation", asset_generation),
        "qc": WorkflowNode("qc", qc),
        "rework": WorkflowNode("rework", rework),
        "publish_approval": WorkflowNode(
            "publish_approval", publish_approval, requires_approval=True,
            approval_request=lambda state: {
                "kind": "publish_approval", "listing": state.localized_listing,
                "qc": state.qc_result, "revision": state.publish_revision,
                "mock": True,
            },
        ),
        "create_marketplace_draft": WorkflowNode(
            "create_marketplace_draft", create_marketplace_draft
        ),
        "publish_draft": WorkflowNode("publish_draft", publish_draft),
    }
    edges = {
        START: "requirement",
        "requirement": "source_search",
        "source_search": "candidate_analysis",
        "candidate_analysis": "candidate_approval",
        "candidate_approval": ConditionalEdge(
            lambda state: state.candidate_decision or "reject",
            {"approve": "sku_selection", "request_revision": "source_search", "reject": END},
        ),
        "sku_selection": "pricing",
        "pricing": "listing_draft",
        "listing_draft": "localize",
        "localize": "asset_generation",
        "asset_generation": "qc",
        "qc": ConditionalEdge(
            lambda state: (
                "pass" if state.qc_passed
                else "rework" if state.rework_count < state.max_reworks
                else "exhausted"
            ),
            {"pass": "publish_approval", "rework": "rework", "exhausted": END},
        ),
        "rework": "asset_generation",
        "publish_approval": ConditionalEdge(
            lambda state: state.publish_decision or "reject",
            {"approve": "create_marketplace_draft",
             "request_revision": "listing_draft", "reject": END},
        ),
        "create_marketplace_draft": "publish_draft",
        "publish_draft": END,
    }
    return WorkflowDefinition(
        id=WORKFLOW_ID,
        domain="commerce",
        state_type=CommerceState,
        nodes=nodes,
        edges=edges,
        services={"source": source, "marketplace": marketplace, "mock": True},
    )


def _candidate(state: CommerceState) -> Candidate:
    for raw in state.candidates:
        candidate = Candidate.model_validate(raw)
        if candidate.id == state.selected_candidate_id:
            return candidate
    raise RuntimeError("selected Commerce candidate is missing")
