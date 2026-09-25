"""Commerce Pack v0.1 完整工作流。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from kantoku.config import ToolError
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
from kantoku.core.skills import SkillRegistry

from .models import (
    Candidate,
    CandidateAnalysis,
    CandidateList,
    CommerceState,
    ListingDraft,
    LocalizedListing,
    PricingResult,
    ProductImageBrief,
    ProductImageResult,
    PublishResult,
    QcReport,
    Requirement,
    SkuSelection,
)
from .ports import MarketplacePort, SourcePort
from .product_image import ProductImageCapability

WORKFLOW_ID = "commerce.production.v1"
Translator = Callable[[dict[str, Any], str], dict[str, Any]]
ListingWriter = Callable[[dict[str, Any], str | None], dict[str, Any]]
QcChecker = Callable[[CommerceState], bool]


def build_commerce_workflow(
    source: SourcePort,
    marketplace: MarketplacePort,
    skills: SkillRegistry,
    translator: Translator,
    qc_checker: QcChecker | None = None,
    product_image: ProductImageCapability | None = None,
    listing_writer: ListingWriter | None = None,
    marketplace_name: str = "Ozon Mock Marketplace",
) -> WorkflowDefinition[CommerceState]:
    """构建与 Adapter 实现无关的 Commerce Workflow。"""

    def artifact(
        context: RuntimeContext,
        type: ArtifactType,
        source_name: str,
        payload: Any,
        *,
        location: str | None = None,
        schema_name: str,
        origin: str = "mock",
        status: str = "ready",
        version: int = 1,
    ) -> str:
        item = context.store.create_artifact(
            type=type,
            run_id=context.run_id,
            node_id=context.node_id,
            source=source_name,
            status=status,
            location=location,
            metadata={
                "schema_name": schema_name,
                "schema_version": "1.0",
                "origin": origin,
                "mock": origin == "mock",
                "payload": payload,
            },
            version=version,
        )
        return item.id

    def requirement(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        payload = Requirement(
            query=state.requirement,
            locale=state.locale,
            revision_instruction=state.candidate_revision_instruction,
        )
        artifact(
            context, ArtifactType.DOCUMENT, "commerce.requirement",
            payload.model_dump(mode="json"), schema_name="commerce.requirement",
        )
        return {}

    def source_search(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        if state.data_mode == "production":
            raise ToolError(
                "BLOCKED_REAL_SOURCE：当前没有连接真实商品数据源，因此不会编造候选商品"
            )
        query = state.requirement
        if state.candidate_revision_instruction:
            query = f"{query}；修改要求：{state.candidate_revision_instruction}"
        candidates = [item.model_dump(mode="json") for item in source.fetch(query)]
        payload = CandidateList.model_validate({
            "items": candidates,
            "query": query,
            "revision_instruction": state.candidate_revision_instruction,
        })
        artifact(
            context, ArtifactType.JSON, "commerce.source.mock",
            payload.model_dump(mode="json"), schema_name="commerce.candidate_list",
        )
        return {"candidates": candidates, "candidate_decision": None, "mock": True}

    def candidate_analysis(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        ordered = sorted(state.candidates, key=lambda item: int(item["cost_fen"]))
        analysis = CandidateAnalysis.model_validate({
            "recommended_id": ordered[0]["id"],
            "candidate_count": len(ordered),
            "basis": "按采购成本升序并结合人工修改要求",
            "revision_instruction": state.candidate_revision_instruction,
            "mock": True,
        })
        payload = analysis.model_dump(mode="json")
        artifact(
            context, ArtifactType.REPORT, "commerce.analysis", payload,
            schema_name="commerce.candidate_analysis",
        )
        return {"analysis": payload}

    def candidate_approval(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        auto_mock = state.execution_mode == "fast" and state.data_mode == "demo"
        decision = context.approval_decision or (
            ApprovalDecision.APPROVE if auto_mock else None
        )
        if decision is None:
            raise RuntimeError("candidate approval decision is missing")
        update: dict[str, Any] = {"candidate_decision": decision.value}
        if decision is ApprovalDecision.APPROVE:
            selected = context.approval_response.get("candidate_id")
            update["selected_candidate_id"] = selected or state.analysis["recommended_id"]
        elif decision is ApprovalDecision.REQUEST_REVISION:
            update["candidate_revision"] = state.candidate_revision + 1
            update["candidate_revision_instruction"] = _revision_instruction(
                context.approval_response
            )
        return update

    def sku_selection(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        candidate = _candidate(state)
        sku = candidate.skus[0]
        payload = SkuSelection(
            candidate_id=candidate.id, sku=sku, mock=candidate.mock
        ).model_dump(mode="json")
        artifact(
            context, ArtifactType.JSON, "commerce.sku", payload,
            schema_name="commerce.sku_selection",
        )
        return {"selected_sku": sku}

    def pricing(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        candidate = _candidate(state)
        result = skills.execute(
            "commerce.calculate_pricing",
            {"cost_fen": candidate.cost_fen, "margin_rate": 0.25,
             "marketplace_fee_rate": 0.15},
            {}, store=context.store, run_id=context.run_id, node_id=context.node_id,
        )
        payload = PricingResult(
            cost_fen=candidate.cost_fen,
            price_fen=int(result["price_fen"]),
            currency=str(result["currency"]),
            margin_rate=0.25,
            marketplace_fee_rate=0.15,
            mock=True,
        ).model_dump(mode="json")
        artifact(
            context, ArtifactType.REPORT, "commerce.pricing", payload,
            schema_name="commerce.pricing_result",
        )
        return {"pricing": payload}

    def listing_draft(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        candidate = _candidate(state)
        facts: dict[str, Any] = {
            "title": candidate.title,
            "sku": state.selected_sku,
            "price_fen": state.pricing["price_fen"],
            "currency": state.pricing["currency"],
            "locale": "zh-CN",
            "revision": state.publish_revision,
            "product_requirement": state.requirement,
        }
        if listing_writer is None:
            description = (
                f"{state.requirement}。SKU {state.selected_sku}，适用于跨境电商商品详情。"
            )
            if state.publish_revision_instruction:
                description += f" 修改要求：{state.publish_revision_instruction}。"
            listing = ListingDraft(
                title=candidate.title,
                description=description,
                sku=str(state.selected_sku),
                price_fen=int(state.pricing["price_fen"]),
                currency=str(state.pricing["currency"]),
                revision=state.publish_revision,
                revision_instruction=state.publish_revision_instruction,
                mock=True,
            ).model_dump(mode="json")
            origin = "mock"
        else:
            listing = ListingDraft.model_validate(
                listing_writer(facts, state.publish_revision_instruction)
            ).model_dump(mode="json")
            origin = "mock" if listing["mock"] else "real"
        artifact(
            context, ArtifactType.LISTING, "commerce.listing", listing,
            schema_name="commerce.listing_draft", origin=origin,
            version=state.publish_revision + 1,
        )
        return {"listing": listing, "publish_decision": None}

    def localize(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        result = skills.execute(
            "commerce.localize_listing",
            {"listing": state.listing, "locale": state.locale},
            {"translator": translator}, store=context.store,
            run_id=context.run_id, node_id=context.node_id,
        )
        localized = LocalizedListing.model_validate({
            **dict(result["localized_listing"]),
            "locale": state.locale,
        }).model_dump(mode="json")
        origin = "mock" if localized["mock"] else "real"
        artifact(
            context, ArtifactType.LISTING, "commerce.localization", localized,
            schema_name="commerce.localized_listing", origin=origin,
            version=state.publish_revision + 1,
        )
        return {"localized_listing": localized}

    def asset_generation(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        candidate = _candidate(state)
        prepared = skills.execute(
            "commerce.product_image",
            {
                "product_name": candidate.title,
                "sku": state.selected_sku,
                "marketplace": marketplace_name,
                "locale": state.locale,
                "revision": state.publish_revision + state.rework_count,
                "revision_instruction": state.publish_revision_instruction,
            },
            {}, store=context.store, run_id=context.run_id, node_id=context.node_id,
        )
        brief = ProductImageBrief.model_validate(prepared["brief"])
        prompt = str(prepared["prompt"])
        if product_image is None:
            result = ProductImageResult(
                status="mock", origin="mock", provider="mock-image-capability",
                model="mock-product-image-v1", prompt=prompt, brief=brief,
                message="Mock 商品主图 / 未生成真实图片",
            )
        else:
            generation_request_id = (
                f"commerce-image:{context.run_id}:"
                f"{state.publish_revision}:{state.rework_count}"
            )
            result = product_image.generate(
                brief, prompt,
                request_id=generation_request_id,
            )
        payload = result.model_dump(mode="json")
        artifact_id = artifact(
            context,
            ArtifactType.IMAGE,
            "commerce.image.real" if result.origin == "real" else "commerce.image.mock",
            payload,
            location=result.location,
            schema_name="commerce.product_image",
            origin=result.origin,
            status=result.status,
            version=state.publish_revision + state.rework_count + 1,
        )
        if product_image is not None and result.origin == "real":
            attach_image_artifact(generation_request_id, artifact_id)
        if result.actual_fen is not None:
            current = context.store.get_run(context.run_id)
            context.store.set_run_cost(context.run_id, current.cost_fen + result.actual_fen)
        if result.status in {"blocked", "failed", "unknown"}:
            raise ToolError(result.message or "商品图生成未完成")
        return {"asset_artifact_id": artifact_id, "product_image": payload}

    def qc(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        passed = (qc_checker or (lambda _state: True))(state)
        report = QcReport(
            passed=passed,
            checks=["artifact_exists", "marketplace_main_image_constraints"],
            mock=True,
            asset_artifact_id=state.asset_artifact_id,
            rework_count=state.rework_count,
        )
        payload = report.model_dump(mode="json")
        artifact(
            context, ArtifactType.REPORT, "commerce.qc.mock", payload,
            schema_name="commerce.qc_report",
        )
        return {"qc_result": payload, "qc_passed": passed}

    def rework(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        count = state.rework_count + 1
        artifact(
            context, ArtifactType.REPORT, "commerce.rework",
            {"from": state.rework_count, "to": count, "mock": True},
            schema_name="commerce.rework_record",
        )
        return {
            "rework_count": count,
            "asset_artifact_id": None,
            "product_image": None,
            "qc_result": None,
            "qc_passed": None,
        }

    def publish_approval(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        auto_mock = state.execution_mode == "fast" and state.data_mode == "demo"
        decision = context.approval_decision or (
            ApprovalDecision.APPROVE if auto_mock else None
        )
        if decision is None:
            raise RuntimeError("publish approval decision is missing")
        update: dict[str, Any] = {"publish_decision": decision.value}
        if decision is ApprovalDecision.REQUEST_REVISION:
            update["publish_revision"] = state.publish_revision + 1
            update["publish_revision_instruction"] = _revision_instruction(
                context.approval_response
            )
        return update

    def create_marketplace_draft(
        state: CommerceState, context: RuntimeContext
    ) -> dict[str, Any]:
        draft = marketplace.create_draft(state.localized_listing or {})
        payload = draft.model_dump(mode="json")
        artifact(
            context, ArtifactType.LISTING, "commerce.marketplace.mock", payload,
            schema_name="commerce.marketplace_draft",
        )
        return {"marketplace_draft": payload}

    def publish_draft(state: CommerceState, context: RuntimeContext) -> dict[str, Any]:
        draft = marketplace.submit_draft(str(state.marketplace_draft["id"]))
        payload = draft.model_dump(mode="json")
        result = PublishResult(
            draft_id=draft.id,
            status=draft.status,
            marketplace=marketplace_name,
            mock=True,
        ).model_dump(mode="json")
        artifact(
            context, ArtifactType.REPORT, "commerce.publish.mock", result,
            schema_name="commerce.publish_result",
        )
        return {"marketplace_draft": payload}

    nodes = {
        "requirement": WorkflowNode("requirement", requirement),
        "source_search": WorkflowNode("source_search", source_search),
        "candidate_analysis": WorkflowNode("candidate_analysis", candidate_analysis),
        "candidate_approval": WorkflowNode(
            "candidate_approval", candidate_approval, requires_approval=True,
            approval_when=lambda state: not (
                state.execution_mode == "fast" and state.data_mode == "demo"
            ),
            approval_request=lambda state: {
                "kind": "candidate_approval", "candidates": state.candidates,
                "analysis": state.analysis, "revision": state.candidate_revision,
                "origin": "mock",
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
            approval_when=lambda state: not (
                state.execution_mode == "fast" and state.data_mode == "demo"
            ),
            approval_request=lambda state: {
                "kind": "publish_approval", "listing": state.localized_listing,
                "image": state.product_image, "qc": state.qc_result,
                "revision": state.publish_revision,
                "origin": "mock",
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
        services={
            "source": source,
            "marketplace": marketplace,
            "product_image": product_image,
            "mock": product_image is None,
        },
    )


def _candidate(state: CommerceState) -> Candidate:
    for raw in state.candidates:
        candidate = Candidate.model_validate(raw)
        if candidate.id == state.selected_candidate_id:
            return candidate
    raise RuntimeError("selected Commerce candidate is missing")


def _revision_instruction(response: Mapping[str, Any]) -> str | None:
    value = response.get("revision_instruction", response.get("notes"))
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
