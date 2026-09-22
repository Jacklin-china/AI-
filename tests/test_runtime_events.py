"""Runtime Event 持久化、顺序、恢复与 Batch 终态稳定性。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kantoku.core.runtime.batch import BatchService, summarize_batch
from kantoku.core.runtime.graph import (
    END,
    START,
    ConditionalEdge,
    GraphRuntime,
    WorkflowDefinition,
    WorkflowNode,
)
from kantoku.core.runtime.models import (
    ApprovalDecision,
    BatchStatus,
    ExecutionStatus,
    RuntimeEventType,
)
from kantoku.core.runtime.store import RuntimeStore
from kantoku.core.state import RunState


class EventState(RunState):
    value: int = 0
    decision: str | None = None


@pytest.fixture
def store(tmp_path: Path) -> RuntimeStore:
    return RuntimeStore(tmp_path / "core.db")


def build(runtime: GraphRuntime) -> str:
    workflow = WorkflowDefinition(
        id="test.events",
        domain="test",
        state_type=EventState,
        nodes={
            "first": WorkflowNode("first", lambda _state, ctx: {
                "value": 1,
            }),
            "review": WorkflowNode(
                "review",
                lambda state, ctx: {"decision": str(ctx.approval_decision)},
                requires_approval=True,
                approval_request=lambda state: {"kind": "demo"},
            ),
            "done": WorkflowNode("done", lambda _state, _ctx: {"value": 2}),
        },
        edges={
            START: "first",
            "first": "review",
            "review": ConditionalEdge(
                lambda state: str(state.decision or "reject").lower(),
                {"approve": "done", "reject": END},
            ),
            "done": END,
        },
    )
    runtime.register(workflow)
    return workflow.id


def test_events_are_persisted_in_order(store: RuntimeStore) -> None:
    runtime = GraphRuntime(store)
    run = runtime.start(build(runtime), EventState())

    events = store.list_events(run.id)
    types = [item.event_type for item in events]
    assert types[0] is RuntimeEventType.RUN_STARTED
    assert RuntimeEventType.NODE_STARTED in types
    assert RuntimeEventType.APPROVAL_REQUIRED in types
    assert RuntimeEventType.RUN_WAITING in types
    sequences = [item.sequence for item in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert run.status is ExecutionStatus.WAITING


def test_events_after_cursor_and_dedup(store: RuntimeStore) -> None:
    runtime = GraphRuntime(store)
    run = runtime.start(build(runtime), EventState())
    total = len(store.list_events(run.id))
    tail = store.list_events(run.id, after=2)
    assert [item.sequence for item in tail] == list(range(3, total + 1))
    assert store.list_events(run.id, after=total) == []


def test_approval_resolution_emits_event_and_resumes(store: RuntimeStore) -> None:
    runtime = GraphRuntime(store)
    run = runtime.start(build(runtime), EventState())
    approval = next(item for item in store.list_approvals() if item.run_id == run.id)
    store.decide_approval(approval.id, ApprovalDecision.APPROVE, {"note": "ok"})

    resolved = [item for item in store.list_events(run.id)
                if item.event_type is RuntimeEventType.APPROVAL_RESOLVED]
    assert resolved and resolved[-1].payload["decision"] == "approve"

    runtime.resume(run.id)
    assert store.get_run(run.id).status is ExecutionStatus.COMPLETED
    assert store.list_events(run.id)[-1].event_type is RuntimeEventType.RUN_COMPLETED


def test_terminal_run_status_is_not_overwritten(store: RuntimeStore) -> None:
    runtime = GraphRuntime(store)
    run = runtime.start(build(runtime), EventState())
    cancelled = runtime.cancel(run.id)
    assert cancelled.status is ExecutionStatus.CANCELLED

    overwritten = store.update_run(
        run.id, status=ExecutionStatus.RUNNING, state=run.state,
        current_node=run.current_node,
    )
    assert overwritten.status is ExecutionStatus.CANCELLED
    assert store.list_events(run.id)[-1].event_type is RuntimeEventType.RUN_CANCELLED


def test_cancelled_run_stops_execution(store: RuntimeStore) -> None:
    calls: dict[str, int] = {"second": 0}

    def first(_state: Any, ctx: Any) -> dict[str, Any]:
        if ctx.node_id == "first":
            store.update_run(
                ctx.run_id, status=ExecutionStatus.CANCELLED,
                state={}, current_node="first",
            )
        return {}

    def second(_state: Any, _ctx: Any) -> dict[str, Any]:
        calls["second"] += 1
        return {}

    workflow = WorkflowDefinition(
        id="test.cancel",
        domain="test",
        state_type=EventState,
        nodes={
            "first": WorkflowNode("first", first),
            "second": WorkflowNode("second", second),
        },
        edges={START: "first", "first": "second", "second": END},
    )
    runtime = GraphRuntime(store)
    runtime.register(workflow)
    run = runtime.start(workflow.id, EventState())

    assert calls["second"] == 0
    assert store.get_run(run.id).status is ExecutionStatus.CANCELLED


def test_batch_aggregation_rules() -> None:
    assert summarize_batch([]) is BatchStatus.PENDING
    assert summarize_batch([ExecutionStatus.PENDING]) is BatchStatus.PENDING
    assert summarize_batch([ExecutionStatus.RUNNING]) is BatchStatus.RUNNING
    assert summarize_batch([ExecutionStatus.WAITING]) is BatchStatus.WAITING
    assert summarize_batch([ExecutionStatus.COMPLETED] * 3) is BatchStatus.COMPLETED
    assert summarize_batch([ExecutionStatus.CANCELLED] * 3) is BatchStatus.CANCELLED
    mixed = [
        ExecutionStatus.COMPLETED, ExecutionStatus.COMPLETED, ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED, ExecutionStatus.CANCELLED,
    ]
    assert summarize_batch(mixed) is BatchStatus.PARTIAL_FAILED


def test_batch_terminal_status_is_stable(store: RuntimeStore) -> None:
    runtime = GraphRuntime(store)
    build(runtime)
    service = BatchService(store, runtime)
    run_ids = [runtime.start("test.events", EventState()).id for _ in range(5)]
    batch = store.create_batch("demo", 2)
    for position, run_id in enumerate(run_ids):
        store.add_batch_run(batch.id, run_id, position)

    store.update_run(run_ids[3], status=ExecutionStatus.FAILED, state={}, current_node="first")
    runtime.cancel(run_ids[4])
    for run_id in run_ids[:3]:
        store.update_run(run_id, status=ExecutionStatus.COMPLETED, state={}, current_node=END)

    readings = {service.summarize(batch.id).status for _ in range(5)}
    assert readings == {BatchStatus.PARTIAL_FAILED}

    service.refresh(batch.id)
    assert store.get_batch(batch.id).status is BatchStatus.PARTIAL_FAILED
    for _ in range(5):
        assert service.refresh(batch.id).status is BatchStatus.PARTIAL_FAILED

    # 普通重新汇总不得把终态回退到 waiting / running
    assert store.update_batch_status(batch.id, BatchStatus.WAITING).status is (
        BatchStatus.PARTIAL_FAILED
    )
