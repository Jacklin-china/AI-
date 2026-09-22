"""Kantoku v0.3 Commerce 产品化语义回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kantoku.adapters.commerce import (
    CoreProductImageCapability,
    MockMarketplaceAdapter,
    MockSourceAdapter,
    MockTranslationAdapter,
)
from kantoku.config.settings import ROOT
from kantoku.core.approval import ApprovalService
from kantoku.core.runtime.graph import END, START, GraphRuntime, WorkflowDefinition, WorkflowNode
from kantoku.core.runtime.models import ApprovalDecision, RuntimeEventType
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import SkillLoader, SkillRegistry
from kantoku.core.state import RunState
from kantoku.domains.commerce import CommerceState, build_commerce_workflow
from kantoku.domains.commerce.models import ProductImageBrief


def _runtime(store: RuntimeStore) -> GraphRuntime:
    skills = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(skills)
    runtime = GraphRuntime(store)
    runtime.register(build_commerce_workflow(
        MockSourceAdapter(), MockMarketplaceAdapter(), skills, MockTranslationAdapter()
    ))
    return runtime


def test_artifacts_have_schema_version_origin_and_human_payload(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "schema.db")
    runtime = _runtime(store)
    waiting = runtime.start("commerce.production.v1", CommerceState(requirement="lamp"))
    approval = store.approval_for_node(waiting.id, "candidate_approval")
    assert approval is not None
    waiting = ApprovalService(store, runtime).decide_and_resume(
        approval.id, ApprovalDecision.APPROVE
    )

    artifacts = store.list_artifacts(waiting.id)
    assert artifacts
    assert all(item.metadata["schema_name"].startswith("commerce.") for item in artifacts)
    assert all(item.metadata["schema_version"] == "1.0" for item in artifacts)
    assert all(item.metadata["origin"] in {"real", "mock", "blocked"} for item in artifacts)
    image = next(
        item for item in artifacts
        if item.metadata["schema_name"] == "commerce.product_image"
    )
    assert image.location is None
    assert image.metadata["payload"]["message"] == "Mock 商品主图 / 未生成真实图片"


def test_candidate_revision_instruction_is_saved_and_used(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "candidate-revision.db")
    runtime = _runtime(store)
    waiting = runtime.start("commerce.production.v1", CommerceState(requirement="travel lamp"))
    approval = store.approval_for_node(waiting.id, "candidate_approval")
    assert approval is not None
    revised = ApprovalService(store, runtime).decide_and_resume(
        approval.id,
        ApprovalDecision.REQUEST_REVISION,
        {"revision_instruction": "采购成本必须低于 15 元"},
    )

    assert revised.state["candidate_revision_instruction"] == "采购成本必须低于 15 元"
    candidate_artifacts = [
        item for item in store.list_artifacts(revised.id)
        if item.metadata["schema_name"] == "commerce.candidate_list"
    ]
    assert (
        candidate_artifacts[0].metadata["payload"]["revision_instruction"]
        == "采购成本必须低于 15 元"
    )
    assert "采购成本必须低于 15 元" in candidate_artifacts[0].metadata["payload"]["query"]


def test_publish_revision_instruction_changes_next_listing(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "publish-revision.db")
    runtime = _runtime(store)
    waiting = runtime.start("commerce.production.v1", CommerceState(requirement="reading lamp"))
    candidate = store.approval_for_node(waiting.id, "candidate_approval")
    assert candidate is not None
    waiting = ApprovalService(store, runtime).decide_and_resume(
        candidate.id, ApprovalDecision.APPROVE
    )
    publish = store.approval_for_node(waiting.id, "publish_approval")
    assert publish is not None
    revised = ApprovalService(store, runtime).decide_and_resume(
        publish.id,
        ApprovalDecision.REQUEST_REVISION,
        {"revision_instruction": "标题更简洁，不要夸张词"},
    )

    assert revised.state["publish_revision_instruction"] == "标题更简洁，不要夸张词"
    assert revised.state["listing"]["revision_instruction"] == "标题更简洁，不要夸张词"
    assert revised.state["listing"]["revision"] == 1
    assert (
        revised.state["product_image"]["brief"]["revision_instruction"]
        == "标题更简洁，不要夸张词"
    )
    assert revised.state["product_image"]["brief"]["revision"] == 1
    image_artifacts = [
        item for item in store.list_artifacts(revised.id)
        if item.metadata["schema_name"] == "commerce.product_image"
    ]
    assert image_artifacts[0].version == 2


def test_duplicate_approval_decision_does_not_resume_twice(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "approval-idempotency.db")
    runtime = _runtime(store)
    waiting = runtime.start("commerce.production.v1", CommerceState(requirement="desk lamp"))
    candidate = store.approval_for_node(waiting.id, "candidate_approval")
    assert candidate is not None
    service = ApprovalService(store, runtime)

    first = service.decide_and_resume(candidate.id, ApprovalDecision.APPROVE)
    artifact_count = len(store.list_artifacts(first.id))
    event_count = len(store.list_events(first.id))
    second = service.decide_and_resume(candidate.id, ApprovalDecision.APPROVE)

    assert second.status == first.status
    assert len(store.list_artifacts(first.id)) == artifact_count
    assert len(store.list_events(first.id)) == event_count


class NeverCalledProvider:
    model_id = "expensive-model"

    def generation_identity(self) -> dict[str, str]:
        return {"provider": "preflight-only"}

    def submit(self, **_kwargs: Any) -> str:
        raise AssertionError("budget preflight must block before submit")


def test_real_product_image_budget_preflight_blocks_before_provider() -> None:
    capability = CoreProductImageCapability(NeverCalledProvider(), max_fen=30)  # type: ignore[arg-type]
    brief = ProductImageBrief(
        product_name="Lamp", sku="SKU-1", marketplace="Ozon", locale="ru-RU",
        image_purpose="main", background="gray", composition="centered",
        constraints=["no text"],
    )
    result = capability.generate(brief, "product photo", request_id="commerce-preflight")
    assert result.status == "blocked"
    assert result.origin == "blocked"
    assert result.message is not None and result.message.startswith("BLOCKED_BY_BUDGET")


def test_run_cost_event_is_backend_truth(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "cost.db")
    run = store.create_run("test", "test.cost", {}, "start")
    updated = store.set_run_cost(run.id, 23)
    assert updated.cost_fen == 23
    event = store.list_events(run.id)[-1]
    assert event.event_type is RuntimeEventType.COST_UPDATED
    assert event.payload == {"cost_fen": 23, "delta_fen": 23}


class FailureState(RunState):
    pass


def test_runtime_error_event_does_not_leak_exception_text(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "safe-error.db")
    runtime = GraphRuntime(store)

    def fail(_state: FailureState, _context: object) -> None:
        raise RuntimeError("secret prompt and provider body")

    runtime.register(WorkflowDefinition(
        id="test.safe-error", domain="test", state_type=FailureState,
        nodes={"fail": WorkflowNode("fail", fail)},
        edges={START: "fail", "fail": END},
    ))
    run = runtime.start("test.safe-error", FailureState())
    events = store.list_events(run.id)
    failed = next(item for item in events if item.event_type is RuntimeEventType.RUN_FAILED)
    serialized = str(failed.payload)
    assert "secret prompt" not in serialized
    assert failed.payload["safe_message"] == "任务执行失败"
    assert str(failed.payload["debug_reference"]).startswith("err-")


def test_product_image_skill_is_discovered_without_core_change() -> None:
    skills = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(skills)
    metadata = {item.id: item for item in skills.list()}
    assert "commerce.product_image" in metadata
    assert metadata["commerce.product_image"].domain == "commerce"
