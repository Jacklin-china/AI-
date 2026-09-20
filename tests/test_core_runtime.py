"""Kantoku Core Runtime 的关键恢复与路由行为。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kantoku.core.runtime.graph import (
    END,
    START,
    ConditionalEdge,
    GraphRuntime,
    WorkflowDefinition,
    WorkflowNode,
)
from kantoku.core.runtime.models import ApprovalDecision, ArtifactType, ExecutionStatus
from kantoku.core.runtime.runner import TaskRunner
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.skills import Skill, SkillMetadata, SkillRegistry
from kantoku.core.state import RunState


class ExampleState(RunState):
    value: int = 0
    route: str = "finish"
    decision: str | None = None


@pytest.fixture
def store(tmp_path: Path) -> RuntimeStore:
    return RuntimeStore(tmp_path / "core.db")


def test_normal_workflow_and_conditional_route(store: RuntimeStore) -> None:
    workflow = WorkflowDefinition(
        id="test.normal",
        domain="test",
        state_type=ExampleState,
        nodes={
            "increment": WorkflowNode("increment", lambda state, _ctx: {
                "value": state.value + 1
            }),
            "finish": WorkflowNode("finish", lambda state, _ctx: {
                "value": state.value + 10
            }),
            "skip": WorkflowNode("skip", lambda _state, _ctx: {"value": -1}),
        },
        edges={
            START: "increment",
            "increment": ConditionalEdge(
                lambda state: state.route,
                {"finish": "finish", "skip": "skip"},
            ),
            "finish": END,
            "skip": END,
        },
    )
    runtime = GraphRuntime(store)
    runtime.register(workflow)
    run = runtime.start(workflow.id, ExampleState())

    assert run.status is ExecutionStatus.COMPLETED
    assert run.state["value"] == 11
    assert [item.node_id for item in store.list_nodes(run.id)] == ["increment", "finish"]


def test_failure_stops_and_retry_succeeds(store: RuntimeStore) -> None:
    calls = {"retry": 0, "after_failure": 0}

    def flaky(_state: ExampleState, _context: object) -> dict[str, int]:
        calls["retry"] += 1
        if calls["retry"] == 1:
            raise RuntimeError("temporary")
        return {"value": 4}

    retry_workflow = WorkflowDefinition(
        id="test.retry", domain="test", state_type=ExampleState,
        nodes={"flaky": WorkflowNode("flaky", flaky, retry_limit=1)},
        edges={START: "flaky", "flaky": END},
    )
    runtime = GraphRuntime(store)
    runtime.register(retry_workflow)
    retried = runtime.start(retry_workflow.id, ExampleState())
    assert retried.status is ExecutionStatus.COMPLETED
    assert store.list_nodes(retried.id)[0].retry_count == 1

    def fail(_state: ExampleState, _context: object) -> dict[str, Any]:
        raise ValueError("stop")

    def forbidden(_state: ExampleState, _context: object) -> dict[str, Any]:
        calls["after_failure"] += 1
        return {}

    failure_workflow = WorkflowDefinition(
        id="test.failure", domain="test", state_type=ExampleState,
        nodes={
            "fail": WorkflowNode("fail", fail),
            "forbidden": WorkflowNode("forbidden", forbidden),
        },
        edges={START: "fail", "fail": "forbidden", "forbidden": END},
    )
    runtime.register(failure_workflow)
    failed = runtime.start(failure_workflow.id, ExampleState())
    assert failed.status is ExecutionStatus.FAILED
    assert calls["after_failure"] == 0


def test_explicit_error_route(store: RuntimeStore) -> None:
    workflow = WorkflowDefinition(
        id="test.error-route", domain="test", state_type=ExampleState,
        nodes={
            "fail": WorkflowNode(
                "fail", lambda _state, _ctx: 1 / 0, error_target="recover"
            ),
            "recover": WorkflowNode("recover", lambda _state, _ctx: {"value": 9}),
        },
        edges={START: "fail", "fail": END, "recover": END},
    )
    runtime = GraphRuntime(store)
    runtime.register(workflow)
    run = runtime.start(workflow.id, ExampleState())
    assert run.status is ExecutionStatus.COMPLETED
    assert run.state["value"] == 9
    assert store.list_nodes(run.id)[0].status is ExecutionStatus.FAILED


def test_checkpoint_approval_resume_and_no_replay(store: RuntimeStore) -> None:
    calls = {"paid": 0}

    def paid(state: ExampleState, _context: object) -> dict[str, int]:
        calls["paid"] += 1
        return {"value": state.value + 1}

    def approval(_state: ExampleState, context: Any) -> dict[str, str]:
        return {"decision": context.approval_decision.value}

    workflow = WorkflowDefinition(
        id="test.approval", domain="test", state_type=ExampleState,
        nodes={
            "paid": WorkflowNode("paid", paid),
            "review": WorkflowNode(
                "review", approval, requires_approval=True,
                approval_request=lambda state: {"value": state.value},
            ),
            "accepted": WorkflowNode("accepted", lambda _state, _ctx: {"value": 8}),
        },
        edges={
            START: "paid",
            "paid": "review",
            "review": ConditionalEdge(
                lambda state: state.decision or "reject",
                {"approve": "accepted", "reject": END, "request_revision": END},
            ),
            "accepted": END,
        },
    )
    first_runtime = GraphRuntime(store)
    first_runtime.register(workflow)
    waiting = first_runtime.start(workflow.id, ExampleState())
    assert waiting.status is ExecutionStatus.WAITING
    assert waiting.current_node == "review"
    assert calls["paid"] == 1

    approval_record = store.list_approvals(pending_only=True)[0]
    store.decide_approval(approval_record.id, ApprovalDecision.APPROVE)

    restarted_runtime = GraphRuntime(RuntimeStore(store.path))
    restarted_runtime.register(workflow)
    completed = restarted_runtime.resume(waiting.id)
    assert completed.status is ExecutionStatus.COMPLETED
    assert completed.state["value"] == 8
    assert calls["paid"] == 1
    assert restarted_runtime.resume(waiting.id).status is ExecutionStatus.COMPLETED
    assert calls["paid"] == 1


def test_reject_route_and_artifact_persistence(store: RuntimeStore) -> None:
    workflow = WorkflowDefinition(
        id="test.reject", domain="test", state_type=ExampleState,
        nodes={
            "review": WorkflowNode(
                "review",
                lambda _state, context: {"decision": context.approval_decision.value},
                requires_approval=True,
            )
        },
        edges={START: "review", "review": END},
    )
    runtime = GraphRuntime(store)
    runtime.register(workflow)
    waiting = runtime.start(workflow.id, ExampleState())
    approval = store.list_approvals(pending_only=True)[0]
    store.decide_approval(approval.id, ApprovalDecision.REJECT, {"reason": "no"})
    completed = runtime.resume(waiting.id)
    assert completed.state["decision"] == "reject"

    artifact = store.create_artifact(
        type=ArtifactType.REPORT, run_id=completed.id, node_id="review",
        source="test", metadata={"ok": True}, location="memory://report",
    )
    assert store.list_artifacts(completed.id) == [artifact]


def test_skill_registry_executes_real_callable_and_runner_is_parallel_ready(
    store: RuntimeStore,
) -> None:
    registry = SkillRegistry()
    registry.load([Skill(
        SkillMetadata(
            id="test.double", name="Double", domain="test", description="Double input",
            version="1.0.0", required_tools=(), input_schema={"required": ["value"]},
            output_schema={"required": ["value"]},
        ),
        lambda inputs, _context: {"value": int(inputs["value"]) * 2},
    )])
    run = store.create_run("test", "skill", {}, END)
    output = registry.execute(
        "test.double", {"value": 3}, {}, store=store, run_id=run.id, node_id="skill"
    )
    assert output == {"value": 6}

    runner = TaskRunner(max_workers=2)
    try:
        assert runner.submit(lambda: "done").result(timeout=2) == "done"
    finally:
        runner.close()


def test_core_runtime_has_no_domain_specific_branches() -> None:
    root = Path(__file__).parents[1] / "src" / "kantoku" / "core"
    sources = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
    assert "if comic" not in sources.lower()
    assert "if commerce" not in sources.lower()
    assert "if ozon" not in sources.lower()
