"""Commerce v0.1 缺失边界的集成验收。"""

from __future__ import annotations

from pathlib import Path

from kantoku.adapters.commerce import (
    MockMarketplaceAdapter,
    MockSourceAdapter,
    MockTranslationAdapter,
)
from kantoku.config.settings import ROOT
from kantoku.core.approval import ApprovalService
from kantoku.core.runtime.graph import GraphRuntime
from kantoku.core.runtime.models import ApprovalDecision, ExecutionStatus
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import SkillLoader, SkillRegistry
from kantoku.domains.commerce import CommerceState, build_commerce_workflow


def _runtime(store: RuntimeStore) -> tuple[GraphRuntime, str]:
    registry = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(registry)
    runtime = GraphRuntime(store)
    workflow = build_commerce_workflow(
        MockSourceAdapter(),
        MockMarketplaceAdapter(),
        registry,
        MockTranslationAdapter(),
    )
    runtime.register(workflow)
    return runtime, workflow.id


def test_selected_candidate_and_all_middle_nodes_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "selected.db"
    store = RuntimeStore(database)
    runtime, workflow_id = _runtime(store)
    waiting = runtime.start(workflow_id, CommerceState(requirement="reading lamp"))
    approval = store.approval_for_node(waiting.id, "candidate_approval")
    assert approval is not None
    selected_id = str(approval.request["candidates"][1]["id"])

    restarted_store = RuntimeStore(database)
    restarted_runtime, _ = _runtime(restarted_store)
    resumed = ApprovalService(restarted_store, restarted_runtime).decide_and_resume(
        approval.id,
        ApprovalDecision.APPROVE,
        {"candidate_id": selected_id},
    )

    assert resumed.status is ExecutionStatus.WAITING
    assert resumed.current_node == "publish_approval"
    assert resumed.state["selected_candidate_id"] == selected_id
    completed = {
        item.node_id
        for item in restarted_store.list_nodes(waiting.id)
        if item.status is ExecutionStatus.COMPLETED
    }
    assert {
        "candidate_approval",
        "sku_selection",
        "pricing",
        "listing_draft",
        "localize",
        "asset_generation",
        "qc",
    } <= completed


def test_publish_reject_finishes_without_marketplace_draft(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "publish-reject.db")
    runtime, workflow_id = _runtime(store)
    approvals = ApprovalService(store, runtime)
    waiting = runtime.start(workflow_id, CommerceState(requirement="reading lamp"))
    candidate = store.approval_for_node(waiting.id, "candidate_approval")
    assert candidate is not None
    waiting = approvals.decide_and_resume(candidate.id, ApprovalDecision.APPROVE)
    publish = store.approval_for_node(waiting.id, "publish_approval")
    assert publish is not None

    rejected = approvals.decide_and_resume(publish.id, ApprovalDecision.REJECT)

    assert rejected.status is ExecutionStatus.COMPLETED
    assert rejected.state["marketplace_draft"] is None
    assert not {
        "commerce.marketplace.mock",
        "commerce.publish.mock",
    } & {artifact.source for artifact in store.list_artifacts(rejected.id)}


def test_commerce_nodes_leave_provider_retry_to_provider_boundary(tmp_path: Path) -> None:
    runtime, workflow_id = _runtime(RuntimeStore(tmp_path / "retry.db"))
    workflow = runtime.workflow(workflow_id)

    assert all(node.retry_limit == 0 for node in workflow.nodes.values())
