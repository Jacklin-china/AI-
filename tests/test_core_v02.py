"""Core v0.2 的 Batch、取消、Artifact 查询与审批编排。"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from kantoku.config import ToolError
from kantoku.core.artifacts import ArtifactService
from kantoku.core.conversations import InteractionMode
from kantoku.core.database import database_manager
from kantoku.core.runtime import BatchService
from kantoku.core.runtime.batch import summarize_batch
from kantoku.core.runtime.graph import (
    END,
    START,
    GraphRuntime,
    WorkflowDefinition,
    WorkflowNode,
)
from kantoku.core.runtime.models import (
    ArtifactLifecycle,
    ArtifactType,
    BatchStatus,
    ExecutionStatus,
)
from kantoku.core.runtime.store import MIGRATIONS, RuntimeStore
from kantoku.core.state import RunState


class BatchState(RunState):
    value: int


def test_artifact_trash_restore_preserves_files_history_and_rejects_stale_versions(
    tmp_path: Path,
) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    run = store.create_run("test", "test.artifacts", {}, "finish")
    file = tmp_path / "media.png"
    file.write_bytes(b"unchanged media")
    artifact = store.create_artifact(
        type=ArtifactType.IMAGE, run_id=run.id, node_id="finish", source="test.image",
        location=str(file), metadata={"origin": "real", "payload": {"title": "original"}},
        version=7,
    )
    service = ArtifactService(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.trash(artifact.id, 1), range(2)))
    assert [item.lifecycle_version for item in results] == [2, 2]
    assert store.list_artifacts(lifecycle_state=ArtifactLifecycle.ACTIVE) == []
    assert store.list_artifacts(lifecycle_state=ArtifactLifecycle.TRASHED)[0].id == artifact.id
    # Existing consumers continue seeing records, even while hidden in the asset library.
    assert service.for_run(run.id)[0].id == artifact.id
    reopened = RuntimeStore(store.path).get_artifact(artifact.id)
    assert reopened.lifecycle_state is ArtifactLifecycle.TRASHED
    restored = service.restore(artifact.id, 2)
    assert restored.lifecycle_state is ArtifactLifecycle.ACTIVE
    assert restored.lifecycle_version == 3
    assert service.restore(artifact.id, 2) == restored
    with pytest.raises(ToolError, match="状态已变化"):
        service.trash(artifact.id, 1)
    assert store.get_artifact(artifact.id) == restored
    assert restored.version == artifact.version
    assert restored.status == artifact.status
    assert restored.metadata == artifact.metadata
    assert restored.location == artifact.location
    assert file.read_bytes() == b"unchanged media"
    assert store.get_run(run.id) == run
    for invalid in (True, None, "3", 0, -1):
        with pytest.raises(ToolError, match="有效的资产操作版本"):
            service.trash(artifact.id, invalid)
    with pytest.raises(ToolError, match="找不到指定资产"):
        service.trash("missing", 1)


def test_artifact_purge_deletes_media_only_from_recycle_bin(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    run = store.create_run("test", "test.artifacts", {}, "finish")
    file = tmp_path / "media.png"
    file.write_bytes(b"media to delete")
    artifact = store.create_artifact(
        type=ArtifactType.IMAGE, run_id=run.id, node_id="finish", source="test.image",
        location=str(file), metadata={"origin": "real", "payload": {"title": "original"}},
        version=7,
    )
    service = ArtifactService(store)
    # 正常资产不能直接彻底删除；必须先进入回收站。
    with pytest.raises(ToolError, match="只有回收站中的资产"):
        service.purge(artifact.id, 1)
    assert file.read_bytes() == b"media to delete"
    service.trash(artifact.id, 1)
    with pytest.raises(ToolError, match="状态已变化"):
        service.purge(artifact.id, 1)
    purged = service.purge(artifact.id, 2)
    assert purged.lifecycle_state is ArtifactLifecycle.PURGED
    assert purged.lifecycle_version == 3
    assert not file.exists()
    # 已清除的资产不再出现在正常库或回收站，但既有关联和历史仍然可读。
    assert store.list_artifacts(lifecycle_state=ArtifactLifecycle.ACTIVE) == []
    assert store.list_artifacts(lifecycle_state=ArtifactLifecycle.TRASHED) == []
    assert service.for_run(run.id)[0].id == artifact.id
    assert store.get_artifact(artifact.id).version == 7
    assert store.get_run(run.id) == run
    # 重复请求幂等，文件缺失不报错。
    assert service.purge(artifact.id, 2) == purged
    for invalid in (True, None, "3", 0, -1):
        with pytest.raises(ToolError, match="有效的资产操作版本"):
            service.purge(artifact.id, invalid)
    with pytest.raises(ToolError, match="找不到"):
        service.purge("missing", 1)


def test_artifact_purge_tolerates_missing_media_and_keeps_history(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    run = store.create_run("test", "test.artifacts", {}, "finish")
    artifact = store.create_artifact(
        type=ArtifactType.PROMPT, run_id=run.id, node_id="finish", source="test.prompt",
        location=str(tmp_path / "already-gone.png"),
    )
    service = ArtifactService(store)
    service.trash(artifact.id, 1)
    assert service.purge(artifact.id, 2).lifecycle_state is ArtifactLifecycle.PURGED
    assert service.for_run(run.id)[0].id == artifact.id


@pytest.mark.parametrize("other_state", [ArtifactLifecycle.ACTIVE, ArtifactLifecycle.TRASHED])
def test_purge_preserves_file_until_last_artifact_reference_is_removed(tmp_path, other_state):
    store = RuntimeStore(tmp_path / "runtime.db")
    run = store.create_run("test", "test.artifacts", {}, "finish")
    media = tmp_path / "shared.png"
    media.write_bytes(b"shared original")
    first = store.create_artifact(type=ArtifactType.IMAGE, run_id=run.id,
                                  node_id="test", source="test",
                                  location=str(media))
    second = store.create_artifact(type=ArtifactType.IMAGE, run_id=run.id,
        node_id="test", source="test",
        location=str(media.parent / "nested" / ".." / media.name), metadata={"internal": True})
    service = ArtifactService(store)
    if other_state is ArtifactLifecycle.TRASHED:
        service.trash(second.id, 1)
    service.trash(first.id, 1)
    service.purge(first.id, 2)
    assert media.read_bytes() == b"shared original"
    assert store.get_artifact(second.id).lifecycle_state is other_state
    if other_state is ArtifactLifecycle.ACTIVE:
        service.trash(second.id, 1)
    service.purge(second.id, 2)
    assert not media.exists()


def test_restore_winning_purge_cas_preserves_file(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path / "runtime.db")
    run = store.create_run("test", "test.artifacts", {}, "finish")
    media = tmp_path / "original.png"
    media.write_bytes(b"original")
    artifact = store.create_artifact(type=ArtifactType.IMAGE, run_id=run.id,
                                     node_id="test", source="test",
                                     location=str(media))
    service = ArtifactService(store)
    service.trash(artifact.id, 1)
    original = store.purge_artifact

    def restore_before_transition(artifact_id, expected_version, **fields):
        service.restore(artifact_id, expected_version)
        return original(artifact_id, expected_version, **fields)

    monkeypatch.setattr(store, "purge_artifact", restore_before_transition)
    with pytest.raises(ToolError, match="状态已变化"):
        service.purge(artifact.id, 2)
    assert store.get_artifact(artifact.id).lifecycle_state is ArtifactLifecycle.ACTIVE
    assert media.read_bytes() == b"original"


def test_purge_file_failure_keeps_recycle_bin_restore_and_retry_available(tmp_path, monkeypatch):
    from kantoku.core.artifacts import service as artifact_module

    store = RuntimeStore(tmp_path / "runtime.db")
    run = store.create_run("test", "test.artifacts", {}, "finish")
    media = tmp_path / "retry.png"
    media.write_bytes(b"original")
    artifact = store.create_artifact(type=ArtifactType.IMAGE, run_id=run.id,
                                     node_id="test", source="test",
                                     location=str(media))
    service = ArtifactService(store)
    service.trash(artifact.id, 1)
    remove = artifact_module._remove_media_file

    def unavailable(_location, **_fields):
        raise OSError("offline file lock")

    monkeypatch.setattr(artifact_module, "_remove_media_file", unavailable)
    with pytest.raises(ToolError, match="数据库操作失败") as failure:
        service.purge(artifact.id, 2)
    assert isinstance(failure.value.__cause__, OSError)
    assert media.exists()
    assert store.get_artifact(artifact.id).lifecycle_state is ArtifactLifecycle.TRASHED
    assert service.restore(artifact.id, 2).lifecycle_state is ArtifactLifecycle.ACTIVE
    service.trash(artifact.id, 3)
    monkeypatch.setattr(artifact_module, "_remove_media_file", remove)
    assert service.purge(artifact.id, 4).lifecycle_state is ArtifactLifecycle.PURGED
    assert not media.exists()


def test_purge_rejects_redirected_media_path_inside_runtime(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.db")
    run = store.create_run("test", "test.artifacts", {}, "finish")
    originals = tmp_path / "originals"
    originals.mkdir()
    media = originals / "protected.png"
    media.write_bytes(b"protected original")
    alias = tmp_path / "redirected"
    try:
        alias.symlink_to(originals, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Host does not permit directory links")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(originals)],
                       check=True, capture_output=True)
    artifact = store.create_artifact(type=ArtifactType.IMAGE, run_id=run.id,
        node_id="test", source="test", location=str(alias / media.name))
    service = ArtifactService(store)
    service.trash(artifact.id, 1)
    with pytest.raises(ToolError, match="重定向"):
        service.purge(artifact.id, 2)
    assert media.read_bytes() == b"protected original"
    assert store.get_artifact(artifact.id).lifecycle_state is ArtifactLifecycle.TRASHED


def test_artifact_lifecycle_migration_backups_legacy_db_and_can_restore(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with closing(database_manager.connect(path)) as connection, connection:
        for version, script in MIGRATIONS:
            if version >= 12:
                break
            connection.executescript(script)
            connection.execute(
                "INSERT INTO core_schema_migrations VALUES (?,?)", (version, "2026-10-09"),
            )
        connection.execute(
            "INSERT INTO runs(id,domain,workflow,status,state_json,current_node,"
            "started_at,updated_at) VALUES ('legacy-run','test','test.legacy','completed',"
            "'{}','END','2026-10-09T00:00:00+00:00','2026-10-09T00:00:00+00:00')",
        )
        connection.execute(
            "INSERT INTO artifacts(id,type,run_id,node_id,source,status,created_at,"
            "metadata_json,version) "
            "VALUES ('legacy','prompt','legacy-run','prompt','test.prompt','ready',"
            "'2026-10-09T00:00:00+00:00','{\"payload\":{\"text\":\"keep history\"}}',4)",
        )
        connection.execute("CREATE TABLE preserved_ledger (id TEXT, state TEXT)")
        connection.execute("INSERT INTO preserved_ledger VALUES ('bill','unknown')")
    migrated = RuntimeStore(path)
    old = migrated.get_artifact("legacy")
    assert old.lifecycle_state is ArtifactLifecycle.ACTIVE
    assert old.lifecycle_version == 1
    assert old.version == 4 and old.metadata["payload"]["text"] == "keep history"
    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert len(backups) == 1
    with closing(database_manager.connect(backups[0])) as backup, backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT version FROM artifacts").fetchone()[0] == 4
        assert backup.execute("SELECT state FROM preserved_ledger").fetchone()[0] == "unknown"
        assert "lifecycle_state" not in {
            row[1] for row in backup.execute("PRAGMA table_info(artifacts)")
        }
        restored_path = tmp_path / "restored.db"
        with closing(database_manager.connect(restored_path)) as destination, destination:
            backup.backup(destination)
    assert RuntimeStore(restored_path).get_artifact("legacy") == old
    ArtifactService(migrated).trash("legacy", 1)
    with closing(database_manager.connect(path)) as connection, connection:
        assert connection.execute("SELECT state FROM preserved_ledger").fetchone()[0] == "unknown"
    RuntimeStore(path)
    assert len(list((tmp_path / "backups").glob("legacy*.sqlite"))) == 1


def test_existing_artifact_survives_conversation_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with closing(database_manager.connect(path)) as connection, connection:
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
