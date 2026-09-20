"""Commerce Pack v0.1 的端口、双审批、返工与 Batch 验收。"""

from __future__ import annotations

from pathlib import Path

from kantoku.adapters.commerce import (
    MockMarketplaceAdapter,
    MockSourceAdapter,
    MockTranslationAdapter,
)
from kantoku.config.settings import ROOT
from kantoku.core.approval import ApprovalService
from kantoku.core.runtime import BatchService
from kantoku.core.runtime.graph import GraphRuntime
from kantoku.core.runtime.models import ApprovalDecision, BatchStatus, ExecutionStatus
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import SkillLoader, SkillRegistry
from kantoku.domains.commerce import CommerceState, build_commerce_workflow


def _runtime(
    store: RuntimeStore, *, qc_checker: object | None = None
) -> tuple[GraphRuntime, str]:
    skills = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(skills)
    runtime = GraphRuntime(store)
    workflow = build_commerce_workflow(
        MockSourceAdapter(), MockMarketplaceAdapter(), skills, MockTranslationAdapter(),
        qc_checker=qc_checker if callable(qc_checker) else None,
    )
    runtime.register(workflow)
    return runtime, workflow.id


def test_mock_adapter_contracts_are_explicit() -> None:
    candidates = MockSourceAdapter().fetch("reading lamp")
    assert len(candidates) == 3
    assert all(item.mock and item.source == "mock-source" for item in candidates)
    marketplace = MockMarketplaceAdapter()
    draft = marketplace.create_draft({"title": "Mock lamp", "mock": True})
    assert draft.mock is True and draft.status == "draft"
    assert marketplace.submit_draft(draft.id).status == "submitted"


def test_commerce_two_approvals_survive_runtime_restart(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "commerce.db")
    runtime, workflow_id = _runtime(store)
    waiting = runtime.start(workflow_id, CommerceState(requirement="reading lamp"))
    assert waiting.status is ExecutionStatus.WAITING
    assert waiting.current_node == "candidate_approval"

    restarted, _ = _runtime(RuntimeStore(store.path))
    candidate = store.list_approvals(pending_only=True)[0]
    after_candidate = ApprovalService(store, restarted).decide_and_resume(
        candidate.id, ApprovalDecision.APPROVE
    )
    assert after_candidate.status is ExecutionStatus.WAITING
    assert after_candidate.current_node == "publish_approval"

    restarted_again, _ = _runtime(RuntimeStore(store.path))
    publish = store.list_approvals(pending_only=True)[0]
    completed = ApprovalService(store, restarted_again).decide_and_resume(
        publish.id, ApprovalDecision.APPROVE
    )
    assert completed.status is ExecutionStatus.COMPLETED
    assert completed.state["marketplace_draft"]["status"] == "submitted"
    sources = {item.source for item in store.list_artifacts(completed.id)}
    assert {
        "commerce.requirement", "commerce.source.mock", "commerce.analysis",
        "commerce.sku", "commerce.pricing", "commerce.listing",
        "commerce.localization", "commerce.image.mock", "commerce.qc.mock",
        "commerce.marketplace.mock", "commerce.publish.mock",
    } <= sources


def test_candidate_reject_and_revision_routes(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "routes.db")
    runtime, workflow_id = _runtime(store)
    rejected = runtime.start(workflow_id, CommerceState(requirement="reject me"))
    approval = store.list_approvals(pending_only=True)[0]
    rejected = ApprovalService(store, runtime).decide_and_resume(
        approval.id, ApprovalDecision.REJECT
    )
    assert rejected.status is ExecutionStatus.COMPLETED
    assert rejected.state["selected_sku"] is None

    revised = runtime.start(workflow_id, CommerceState(requirement="revise me"))
    approval = store.list_approvals(pending_only=True)[0]
    revised = ApprovalService(store, runtime).decide_and_resume(
        approval.id, ApprovalDecision.REQUEST_REVISION
    )
    assert revised.status is ExecutionStatus.WAITING
    assert revised.current_node == "candidate_approval"
    assert revised.state["candidate_revision"] == 1


def test_qc_rework_and_max_reworks(tmp_path: Path) -> None:
    def checker(state: CommerceState) -> bool:
        return state.rework_count >= 1

    store = RuntimeStore(tmp_path / "rework.db")
    runtime, workflow_id = _runtime(store, qc_checker=checker)
    runtime.start(
        workflow_id, CommerceState(requirement="rework once", max_reworks=1)
    )
    candidate = store.list_approvals(pending_only=True)[0]
    result = ApprovalService(store, runtime).decide_and_resume(
        candidate.id, ApprovalDecision.APPROVE
    )
    assert result.current_node == "publish_approval"
    assert result.state["rework_count"] == 1
    assert len([item for item in store.list_artifacts(result.id)
                if item.source == "commerce.image.mock"]) == 2

    limited_store = RuntimeStore(tmp_path / "limited.db")
    limited_runtime, limited_id = _runtime(limited_store, qc_checker=lambda _state: False)
    limited = limited_runtime.start(
        limited_id, CommerceState(requirement="no rework", max_reworks=0)
    )
    candidate = limited_store.list_approvals(pending_only=True)[0]
    limited = ApprovalService(limited_store, limited_runtime).decide_and_resume(
        candidate.id, ApprovalDecision.APPROVE
    )
    assert limited.status is ExecutionStatus.COMPLETED
    assert limited.state["rework_count"] == 0


def test_ru_locale_and_publish_revision(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "ru.db")
    runtime, workflow_id = _runtime(store)
    waiting = runtime.start(
        workflow_id, CommerceState(requirement="lamp", locale="ru-RU")
    )
    candidate = store.list_approvals(pending_only=True)[0]
    waiting = ApprovalService(store, runtime).decide_and_resume(
        candidate.id, ApprovalDecision.APPROVE
    )
    assert waiting.state["localized_listing"]["locale"] == "ru-RU"
    publish = store.list_approvals(pending_only=True)[0]
    waiting = ApprovalService(store, runtime).decide_and_resume(
        publish.id, ApprovalDecision.REQUEST_REVISION
    )
    assert waiting.current_node == "publish_approval"
    assert waiting.state["publish_revision"] == 1


def test_five_run_commerce_batch_demo(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "batch-demo.db")
    runtime, workflow_id = _runtime(store)
    batches = BatchService(store, runtime)
    batch = batches.create(
        name="Commerce 5 Run Demo", workflow_id=workflow_id,
        states=[CommerceState(requirement=f"product {index}") for index in range(5)],
        concurrency_limit=2,
    )
    assert batch.status is BatchStatus.WAITING
    assert len(batch.run_ids) == 5

    approvals = ApprovalService(store, runtime)
    for run_id in batch.run_ids:
        candidate = store.approval_for_node(run_id, "candidate_approval")
        assert candidate is not None
        approvals.decide_and_resume(candidate.id, ApprovalDecision.APPROVE)
        publish = store.approval_for_node(run_id, "publish_approval")
        assert publish is not None
        completed = approvals.decide_and_resume(publish.id, ApprovalDecision.APPROVE)
        assert completed.status is ExecutionStatus.COMPLETED
        assert store.list_artifacts(run_id)
    assert batches.refresh(batch.id).status is BatchStatus.COMPLETED
