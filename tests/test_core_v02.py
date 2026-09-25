"""Core v0.2 的 Batch、取消、Artifact 查询与审批编排。"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from kantoku.core.conversations import InteractionMode
from kantoku.core.runtime import BatchService
from kantoku.core.runtime.batch import summarize_batch
from kantoku.core.runtime.graph import (
    END,
    START,
    GraphRuntime,
    WorkflowDefinition,
    WorkflowNode,
)
from kantoku.core.runtime.models import ArtifactType, BatchStatus, ExecutionStatus
from kantoku.core.runtime.store import MIGRATIONS, RuntimeStore
from kantoku.core.state import RunState


class BatchState(RunState):
    value: int


def test_existing_artifact_survives_conversation_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        for version, script in MIGRATIONS:
            if version > 6:
                break
            connection.executescript(script)
            connection.execute(
                "INSERT INTO core_schema_migrations(version,applied_at) VALUES (?,?)",
                (version, "2026-09-23T00:00:00+00:00"),
            )
        timestamp = "2026-09-23T00:00:00+00:00"
        connection.execute(
            "INSERT INTO runs(id,domain,workflow,status,state_json,current_node,"
            "started_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("home-run", "comic", "comic.v1", "completed", "{}", "END", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO conversations(id,title,interaction_mode,active_run_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("home-chat", "旧首页聊天", "autonomous", "home-run", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO artifacts(id,type,run_id,node_id,source,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("legacy-image", "image", "home-run", "generate", "comic", "ready", timestamp),
        )

    store = RuntimeStore(path)
    artifact = store.get_artifact("legacy-image")
    assert artifact.run_id == "home-run"
    assert artifact.conversation_id is None
    assert store.list_runs(interaction_mode=InteractionMode.GUIDED) == []
    assert [run.id for run in store.list_runs(interaction_mode=InteractionMode.AUTONOMOUS)] == [
        "home-run"
    ]


def _workflow(handler: object | None = None) -> WorkflowDefinition[BatchState]:
    execute = handler if callable(handler) else lambda state, _context: {"value": state.value + 1}
    return WorkflowDefinition(
        id="test.batch.v1", domain="test", state_type=BatchState,
        nodes={"work": WorkflowNode("work", execute)},
        edges={START: "work", "work": END},
    )


def test_batch_create_and_concurrency_limit(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "batch.db")
    active = 0
    maximum = 0
    started = 0
    guard = threading.Lock()
    first_pair = threading.Barrier(2)

    def work(state: BatchState, _context: object) -> dict[str, int]:
        nonlocal active, maximum, started
        with guard:
            active += 1
            started += 1
            ordinal = started
            maximum = max(maximum, active)
        if ordinal <= 2:
            first_pair.wait(timeout=5)
        time.sleep(0.03)
        with guard:
            active -= 1
        return {"value": state.value + 1}

    runtime = GraphRuntime(store)
    runtime.register(_workflow(work))
    batch = BatchService(store, runtime).create(
        name="five runs", workflow_id="test.batch.v1",
        states=[BatchState(value=index) for index in range(5)], concurrency_limit=2,
    )

    assert batch.status is BatchStatus.COMPLETED
    assert len(batch.run_ids) == 5
    assert 1 < maximum <= 2


def test_batch_partial_failed_and_cancel(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "status.db")
    runtime = GraphRuntime(store)
    runtime.register(_workflow())
    service = BatchService(store, runtime)
    batch = store.create_batch("mixed", 2)
    statuses = [
        ExecutionStatus.COMPLETED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    ]
    for position, status in enumerate(statuses):
        run = store.create_run("test", "test.batch.v1", {"value": position}, "work")
        store.update_run(
            run.id, status=status, state=run.state, current_node=run.current_node
        )
        store.add_batch_run(batch.id, run.id, position)
    assert service.refresh(batch.id).status is BatchStatus.PARTIAL_FAILED
    assert summarize_batch(statuses) is BatchStatus.PARTIAL_FAILED

    waiting_batch = store.create_batch("cancel", 1)
    run = store.create_run("test", "test.batch.v1", {"value": 1}, "work")
    store.add_batch_run(waiting_batch.id, run.id, 0)
    cancelled = service.cancel(waiting_batch.id)
    assert cancelled.status is BatchStatus.CANCELLED
    assert store.get_run(run.id).status is ExecutionStatus.CANCELLED


def test_artifact_global_filters_and_lookup(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "artifacts.db")
    comic = store.create_run("comic", "one", {}, END)
    commerce = store.create_run("commerce", "two", {}, END)
    image = store.create_artifact(
        type=ArtifactType.IMAGE, run_id=comic.id, node_id="image", source="test"
    )
    listing = store.create_artifact(
        type=ArtifactType.LISTING, run_id=commerce.id, node_id="listing", source="test"
    )

    assert store.get_artifact(image.id) == image
    assert store.list_artifacts(type=ArtifactType.LISTING) == [listing]
    assert store.list_artifacts(domain="comic") == [image]
    assert store.list_artifacts(commerce.id, domain="commerce") == [listing]
