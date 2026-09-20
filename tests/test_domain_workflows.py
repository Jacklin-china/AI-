"""Comic 与 Commerce 共用同一 Core 的端到端测试。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kantoku.adapters.commerce import (
    MockMarketplaceAdapter,
    MockSourceAdapter,
    MockTranslationAdapter,
)
from kantoku.config.settings import ROOT
from kantoku.core.runtime.graph import GraphRuntime
from kantoku.core.runtime.models import ApprovalDecision, ExecutionStatus
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import SkillLoader, SkillRegistry
from kantoku.domains.comic import ComicState, build_comic_workflow
from kantoku.domains.commerce import CommerceState, build_commerce_workflow


class FakeComicServices:
    """不产生外部费用，但覆盖真实 Comic 适配器的完整契约。"""

    def __init__(self) -> None:
        self.generated = 0

    def prepare(self, _state: ComicState) -> Mapping[str, Any]:
        return {"request_id": "studio-" + "a" * 32}

    def generate(self, state: ComicState) -> Mapping[str, Any]:
        self.generated += 1
        return {
            "image_path": f"/fake/image-{state.rework_count}.png",
            "provider_job_id": f"job-{state.rework_count}",
        }

    def qc(self, _state: ComicState) -> Mapping[str, Any]:
        return {
            "qc_result": {
                "broken_hands": False, "watermark": False, "composition_ok": True,
                "persona_consistency": 5, "confidence": 0.9, "reason": "fake pass",
            },
            "qc_passed": True,
        }

    def review(
        self, _state: ComicState, decision: str, _response: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {"approval_decision": decision}

    def rework(self, state: ComicState) -> Mapping[str, Any]:
        return {
            "request_id": "studio-" + "b" * 32,
            "rework_count": state.rework_count + 1,
            "approval_decision": None,
            "image_path": None,
            "qc_result": None,
            "qc_passed": None,
        }

    def archive(self, _state: ComicState) -> Mapping[str, Any]:
        return {"archive_path": "/fake/archive.png"}


def test_comic_workflow_waits_resumes_and_archives(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "comic.db")
    services = FakeComicServices()
    runtime = GraphRuntime(store)
    workflow = build_comic_workflow(services)
    runtime.register(workflow)

    waiting = runtime.start(workflow.id, ComicState(
        project="demo", prompt="scene", shot_no=1, estimate_fen=10, confirmed=True
    ))
    assert waiting.status is ExecutionStatus.WAITING
    assert waiting.current_node == "human_review"
    approval = store.list_approvals(pending_only=True)[0]
    store.decide_approval(approval.id, ApprovalDecision.APPROVE)
    completed = runtime.resume(waiting.id)

    assert completed.status is ExecutionStatus.COMPLETED
    assert completed.state["archive_path"] == "/fake/archive.png"
    assert services.generated == 1
    assert store.list_artifacts(completed.id)[0].source == "comic.archive"


def test_comic_revision_loops_once_and_requests_new_approval(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "revision.db")
    services = FakeComicServices()
    runtime = GraphRuntime(store)
    workflow = build_comic_workflow(services)
    runtime.register(workflow)
    waiting = runtime.start(workflow.id, ComicState(
        project="demo", prompt="scene", shot_no=1, estimate_fen=10, confirmed=True
    ))
    first = store.list_approvals(pending_only=True)[0]
    store.decide_approval(first.id, ApprovalDecision.REQUEST_REVISION)
    waiting_again = runtime.resume(waiting.id)
    assert waiting_again.status is ExecutionStatus.WAITING
    assert waiting_again.state["rework_count"] == 1
    assert services.generated == 2
    assert store.list_approvals(pending_only=True)[0].id != first.id


def test_commerce_mock_workflow_uses_same_runtime(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "commerce.db")
    runtime = GraphRuntime(store)
    skills = SkillRegistry()
    SkillLoader(ROOT / "skills", project_root=ROOT).load(skills)
    workflow = build_commerce_workflow(
        MockSourceAdapter(), MockMarketplaceAdapter(), skills, MockTranslationAdapter()
    )
    runtime.register(workflow)
    waiting = runtime.start(workflow.id, CommerceState(requirement="portable lamp"))
    assert waiting.status is ExecutionStatus.WAITING
    assert waiting.state["mock"] is True

    approval = store.list_approvals(pending_only=True)[0]
    store.decide_approval(approval.id, ApprovalDecision.APPROVE)
    completed = runtime.resume(waiting.id)
    assert completed.status is ExecutionStatus.WAITING
    assert completed.current_node == "publish_approval"
    publish = store.list_approvals(pending_only=True)[0]
    store.decide_approval(publish.id, ApprovalDecision.APPROVE)
    finished = runtime.resume(waiting.id)
    assert finished.status is ExecutionStatus.COMPLETED
    assert finished.state["marketplace_draft"]["mock"] is True
    assert all(item.metadata["mock"] is True for item in store.list_artifacts(finished.id))
