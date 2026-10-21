"""Verify real files, multi-process WAL and bounded cleanup without production data."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from kantoku.config import ConfigError
from kantoku.config.runtime_paths import (
    RuntimeManager,
    atomic_write_bytes,
    get_runtime_paths,
    get_test_runtime_paths,
    isolated_runtime,
    remove_owned_directory,
    require_permanent_test_cleanup,
    validate_runtime_path,
)
from kantoku.core.database import DatabaseManager, database_manager
from kantoku.core.runtime import storage
from kantoku.core.runtime.storage import FileCatalog


def test_default_test_namespace_is_outside_source_and_production(monkeypatch):
    from kantoku.config.settings import ROOT

    monkeypatch.setenv("KANTOKU_RUNTIME_ROOT", str(ROOT))
    manager = get_test_runtime_paths()
    assert manager.root != get_runtime_paths().root
    assert not manager.root.is_relative_to(ROOT.resolve())
    assert validate_runtime_path(manager.root) == manager.root


def test_redirected_delete_is_rejected_before_creating_test_files(monkeypatch):
    called = []
    def redirected_delete(*_args, **_kwargs):
        called.append(True)
    # A wrapper can preserve the original module name; it is still not native.
    redirected_delete.__module__ = os.unlink.__module__
    monkeypatch.setattr(os, "unlink", redirected_delete)
    with pytest.raises(ConfigError, match="改写了文件删除"):
        require_permanent_test_cleanup()
    with pytest.raises(ConfigError, match="改写了文件删除"), isolated_runtime(
        prefix="kantoku-refuse-",
    ):
        pytest.fail("Rejected environment must not create or enter a test session")
    assert called == []


@pytest.mark.parametrize("catalog_initialized", [True, False])
def test_acceptance_files_are_removed_even_on_failure(monkeypatch, catalog_initialized):
    if not catalog_initialized:
        monkeypatch.setattr("kantoku.config.runtime_paths._catalog_factory", None)
    with pytest.raises(RuntimeError, match="offline failure"), isolated_runtime(
        prefix="kantoku-ephemeral-",
    ) as directory:
        (directory / "fixture.bin").write_bytes(b"temporary offline fixture")
        raise RuntimeError("offline failure")
    assert not directory.exists()


def test_isolated_runtime_restores_configuration_when_cleanup_fails(monkeypatch):
    from kantoku.config import get_settings

    parent = get_runtime_paths()
    previous = os.environ.get("KANTOKU_RUNTIME_ROOT")
    database = get_settings().storage.sqlite_path
    directory = None

    def fail_cleanup(*_args):
        raise OSError("offline simulated file lock")

    monkeypatch.setattr("kantoku.config.runtime_paths.remove_owned_directory", fail_cleanup)
    try:
        with pytest.raises(OSError, match="simulated file lock"), isolated_runtime(
            prefix="kantoku-cleanup-",
        ) as directory:
            assert get_runtime_paths().root == directory
        assert os.environ.get("KANTOKU_RUNTIME_ROOT") == previous
        assert get_settings().storage.sqlite_path == database
        assert directory.exists()
    finally:
        if directory is not None:
            remove_owned_directory(directory, parent.tmp / "sessions")


@pytest.fixture
def databases():
    manager = DatabaseManager()
    try:
        yield manager
    finally:
        manager.checkpoint_all()


@pytest.fixture
def managed(tmp_path, monkeypatch):
    monkeypatch.setenv("KANTOKU_RUNTIME_ROOT", str(tmp_path))
    manager = RuntimeManager(tmp_path)
    manager.prepare()
    catalog = FileCatalog(tmp_path / "data" / "catalog.sqlite", database_manager)
    monkeypatch.setattr("kantoku.config.runtime_paths._catalog_factory", lambda: catalog)
    return manager, catalog


def test_temp_is_rejected_including_database_sidecars(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path / "windows-temp"))
    for path in (tmp_path / "windows-temp" / "app.db",
                 tmp_path / "windows-temp" / "cache" / "a.log"):
        with pytest.raises(ConfigError):
            validate_runtime_path(path)
        assert not path.exists()


def test_random_database_outside_managed_storage_is_rejected(managed):
    manager, _ = managed
    path = manager.root / "random.db"
    with pytest.raises(ConfigError):
        database_manager.connect(path)
    assert not path.exists()


def test_crud_preserves_one_wal_pair_and_shutdown_removes_it(tmp_path, databases):
    manager = databases
    path = tmp_path / "app.sqlite"
    with closing(manager.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sample(value)")
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    identity = (wal.stat().st_ctime_ns, shm.stat().st_ctime_ns)
    for value in range(100):
        with closing(manager.connect(path)) as connection, connection:
            connection.execute("INSERT INTO sample VALUES (?)", (value,))
        assert (wal.stat().st_ctime_ns, shm.stat().st_ctime_ns) == identity
    assert manager.checkpoint_all()[0].status == "READY"
    assert not wal.exists() and not shm.exists()
    with closing(manager.connect(path, readonly=True)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sample").fetchone() == (100,)


def test_busy_shutdown_preserves_connection_and_data(tmp_path, databases):
    manager = databases
    connection = manager.connect(tmp_path / "busy.db")
    connection.execute("CREATE TABLE sample(value)")
    connection.commit()
    assert manager.checkpoint_all()[0].status == "BUSY"
    connection.execute("INSERT INTO sample VALUES (1)")
    connection.commit()
    connection.close()
    assert manager.checkpoint_all()[0].status == "READY"


def test_multiprocess_writers_keep_all_rows(tmp_path, databases):
    path = tmp_path / "shared.db"
    manager = databases
    with closing(manager.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sample(value)")
    script = """
import sys
from pathlib import Path
from contextlib import closing
from kantoku.core.database import database_manager
for value in range(40):
    with closing(database_manager.connect(Path(sys.argv[1]))) as connection, connection:
        connection.execute('INSERT INTO sample VALUES (?)', (value,))
database_manager.checkpoint_all()
"""
    children = [subprocess.Popen([sys.executable, "-B", "-c", script, str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        output, error = child.communicate(timeout=40)
        assert child.returncode == 0, (output, error)
    with closing(manager.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sample").fetchone() == (80,)
    assert manager.checkpoint_all()[0].status == "READY"
    assert not Path(str(path) + "-wal").exists()


def test_metadata_atomic_write_and_no_staging_remains(managed):
    manager, catalog = managed
    path = manager.artifacts / "picture.png"
    atomic_write_bytes(path, b"image")
    with catalog.connection() as connection:
        row = connection.execute("SELECT type,owner,created_at,expire_at,status "
                                 "FROM runtime_files WHERE path=?", (str(path),)).fetchone()
    assert row[0] == "artifact" and row[2] > 0 and row[3] is None and row[4] == "active"
    assert path.read_bytes() == b"image"
    assert not list(path.parent.glob(".kantoku-*"))


def test_ttl_and_capacity_preserve_active_and_user_files(managed, monkeypatch):
    manager, catalog = managed
    cache = manager.cache / "old.bin"
    cache.write_bytes(b"a" * 12)
    catalog.register(cache, "cache", "test", None)
    held = manager.tmp / "held.bin"
    held.write_bytes(b"held")
    held_id = catalog.register(held, "temp", "test", -1)
    expired = manager.tmp / "expired.bin"
    expired.write_bytes(b"expired")
    catalog.register(expired, "temp", "test", -1)
    persistent = []
    for kind in ("artifact", "backup", "recovery"):
        path = manager.data / f"{kind}.bin"
        path.write_bytes(b"must remain")
        catalog.register(path, kind, "test", -1)
        persistent.append(path)
    unknown = manager.tmp / "unknown.bin"
    unknown.write_bytes(b"unknown")
    monkeypatch.setitem(storage.CAPACITY, "cache", 10)
    with catalog.lease(held_id):
        result = catalog.cleanup()
    assert result == {"deleted": 2, "released_bytes": 19, "failures": 0}
    assert not cache.exists() and not expired.exists()
    assert held.exists() and unknown.exists() and all(path.exists() for path in persistent)
    assert catalog.cleanup()["deleted"] == 1


def test_failed_unlink_is_not_success_and_is_retriable(managed, monkeypatch):
    manager, catalog = managed
    path = manager.tmp / "occupied.bin"
    path.write_bytes(b"123")
    catalog.register(path, "temp", "test", -1)
    unlink = Path.unlink

    def denied(target, *args, **kwargs):
        if target == path:
            raise PermissionError("held open")
        return unlink(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", denied)
        assert catalog.cleanup() == {"deleted": 0, "released_bytes": 0, "failures": 1}
    assert path.exists()
    assert catalog.cleanup() == {"deleted": 1, "released_bytes": 3, "failures": 0}


def test_migration_backup_retains_existing_business_table(tmp_path, databases):
    manager = databases
    path = tmp_path / "legacy.db"
    with closing(manager.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE ledger(value)")
        connection.execute("INSERT INTO ledger VALUES ('unknown billing')")
    FileCatalog(path, manager)
    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert len(backups) == 1
    with closing(manager.connect(backups[0], readonly=True)) as connection:
        assert connection.execute("SELECT * FROM ledger").fetchall() == [("unknown billing",)]
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='runtime_files'"
        ).fetchone()
    with closing(manager.connect(path)) as connection:
        assert connection.execute("SELECT * FROM ledger").fetchall() == [("unknown billing",)]
    manager.checkpoint_all()


def test_doctor_is_read_only_and_reports_orphan(managed, monkeypatch):
    from kantoku import doctor

    manager, catalog = managed
    monkeypatch.setattr(doctor, "configured_database_path", lambda: catalog.database)
    database_manager.checkpoint_all(within=manager.root)
    orphan = manager.data / "lost.db-wal"
    orphan.write_bytes(b"unclassified")
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns)
              for path in manager.root.rglob("*") if path.is_file()}
    checks = doctor.runtime_checks()
    after = {path: (path.stat().st_size, path.stat().st_mtime_ns)
             for path in manager.root.rglob("*") if path.is_file()}
    assert before == after
    assert next(check for check in checks if check.name == "Orphan WAL/SHM").status == "BLOCKED"
    assert next(check for check in checks if check.name == "Temporary Size")


def test_cross_process_lease_blocks_cleanup(managed):
    manager, catalog = managed
    path = manager.tmp / "leased.bin"
    path.write_bytes(b"active")
    file_id = catalog.register(path, "temp", "test", -1)
    script = """
import sys,time
from pathlib import Path
from kantoku.core.database import database_manager
from kantoku.core.runtime.storage import FileCatalog
catalog=FileCatalog(Path(sys.argv[1]),database_manager)
with catalog.lease(sys.argv[2]):
    print('leased',flush=True)
    sys.stdin.readline()
database_manager.checkpoint_all()
"""
    child = subprocess.Popen([sys.executable, "-B", "-c", script,
                              str(catalog.database), file_id], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "leased"
        assert catalog.cleanup()["deleted"] == 0 and path.exists()
    finally:
        _, error = child.communicate("close\n", timeout=30)
    assert child.returncode == 0, error
    assert catalog.cleanup()["deleted"] == 1


def test_registered_log_cap_never_deletes_legacy_log(managed, monkeypatch):
    manager, catalog = managed
    legacy = manager.logs / "legacy.log"
    legacy.write_bytes(b"old")
    new = manager.logs / "rotated.log"
    new.write_bytes(b"x" * 11)
    catalog.register(new, "log", "application", None)
    monkeypatch.setitem(storage.CAPACITY, "log", 10)
    assert catalog.cleanup()["released_bytes"] == 11
    assert legacy.exists() and not new.exists()


def test_ephemeral_outside_managed_root_rejected(managed, tmp_path):
    _, catalog = managed
    with pytest.raises(ValueError):
        catalog.register(tmp_path.parent / "foreign.bin", "cache", "test", -1)


def test_managed_tmp_default_never_windows_temp(managed):
    import tempfile

    manager, _ = managed
    assert Path(tempfile.gettempdir()).is_relative_to(manager.tmp)
    assert get_runtime_paths().root == manager.root


def test_sdk_staging_is_process_owned_leased_and_removed(managed):
    import tempfile

    manager, catalog = managed
    manager.claim_staging()
    directory = Path(tempfile.gettempdir())
    (directory / "sdk-payload.bin").write_bytes(b"temporary SDK payload")
    with catalog.connection() as connection:
        row = connection.execute("SELECT file_id,type,owner FROM runtime_files WHERE path=?",
                                 (str(directory),)).fetchone()
        assert row[1:] == ("temp_directory", "kantoku-sdk")
        connection.execute("UPDATE runtime_files SET expire_at=0 WHERE file_id=?", (row[0],))
    assert catalog.cleanup()["deleted"] == 0
    assert directory.exists()
    manager.close_staging()
    assert not directory.exists()


def test_dead_sdk_staging_requires_valid_owner_and_expiry(managed):
    import json
    import tempfile

    manager, catalog = managed
    directory = Path(tempfile.gettempdir())
    marker = directory / ".kantoku-session.json"
    record = json.loads(marker.read_text())
    record["pid"] = 2147483647
    marker.write_text(json.dumps(record), encoding="utf-8")
    assert catalog.cleanup()["deleted"] == 0
    record["expire_at"] = time.time() - 1
    record["created_at"] = time.time() - 86401
    marker.write_text(json.dumps(record), encoding="utf-8")
    assert catalog.cleanup()["deleted"] == 1
    assert not directory.exists()
    manager.prepare()


def test_dead_process_lease_recovered_and_expiry_respected(managed):
    manager, catalog = managed
    path = manager.tmp / "abandoned.bin"
    path.write_bytes(b"dead")
    file_id = catalog.register(path, "temp", "test", -1)
    with catalog.connection() as connection:
        connection.execute("INSERT INTO runtime_file_leases VALUES (?,?,?,?)",
                           ("dead", file_id, 2147483647, time.time() + 86400))
    assert catalog.cleanup()["deleted"] == 1
    assert not path.exists()


def test_own_process_identity_is_alive():
    assert FileCatalog._pid_alive(os.getpid())


def test_abandoned_owned_session_is_removed_with_correct_size(managed):
    import json

    manager, catalog = managed
    session = manager.create_test_session()
    marker = session / ".kantoku-session.json"
    record = json.loads(marker.read_text())
    record.update(pid=2147483647, created_at=time.time() - 86401, expire_at=time.time() - 1)
    marker.write_text(json.dumps(record), encoding="utf-8")
    payload = session / "fixture.bin"
    payload.write_bytes(b"fixture")
    size = marker.stat().st_size + payload.stat().st_size
    result = catalog.cleanup()
    assert result == {"deleted": 1, "released_bytes": size, "failures": 0}
    assert not session.exists()


def test_sweep_removes_dead_test_session_without_waiting_for_expiry(managed):
    """被中断的测试会话在下一次测试启动时即被回收，不必等 24h 过期。"""
    import json

    manager, _catalog = managed
    session = manager.create_test_session()
    marker = session / ".kantoku-session.json"
    record = json.loads(marker.read_text(encoding="utf-8"))
    # 刚创建、尚未过期，但所属进程已退出：旧机制会保留，清扫必须回收。
    record["pid"] = 2147483647
    marker.write_text(json.dumps(record), encoding="utf-8")
    (session / "fixture.bin").write_bytes(b"fixture")

    assert manager.sweep_abandoned_test_sessions() == 1
    assert not session.exists()
    # 再次清扫是幂等的：目标已不存在，不再计数。
    assert manager.sweep_abandoned_test_sessions() == 0


def test_sweep_preserves_live_and_foreign_directories(managed):
    """仍存活的会话（含并发 pytest）与非测试目录一律保留。"""
    import json

    manager, _catalog = managed
    live = manager.create_test_session()
    assert manager.sweep_abandoned_test_sessions() == 0
    assert live.is_dir()

    foreign = manager.tmp / "sessions" / "kantoku-unknown"
    foreign.mkdir(parents=True)
    (foreign / "fixture.bin").write_bytes(b"must keep")

    spoofed = manager.tmp / "sessions" / "kantoku-test-spoofed"
    spoofed.mkdir(parents=True)
    (spoofed / ".kantoku-session.json").write_text(
        json.dumps({"pid": 2147483647, "owner": "someone-else", "type": "test_session"}),
        encoding="utf-8",
    )

    assert manager.sweep_abandoned_test_sessions() == 0
    assert foreign.is_dir() and spoofed.is_dir()


def test_remove_owned_directory_does_not_follow_nested_links(managed):
    """嵌套的符号链接/目录联接只删链接本身，绝不递归进目标目录。

    仓库里确实有指向真实 frontend/node_modules 的目录联接；若清理跟随它们，
    就会删掉真实依赖。当前环境不允许创建链接时跳过。
    """
    manager, _catalog = managed
    sessions = manager.ensure_directory(manager.tmp / "sessions")
    session = sessions / "kantoku-link-probe"
    session.mkdir()
    (session / "owned.bin").write_bytes(b"owned")
    outsider = sessions / "outsider-keep"
    outsider.mkdir()
    (outsider / "keep.bin").write_bytes(b"must survive")
    try:
        (session / "link").symlink_to(outsider, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        remove_owned_directory(session, sessions)
        remove_owned_directory(outsider, sessions)
        pytest.skip(f"当前环境不允许创建符号链接：{type(error).__name__}")

    remove_owned_directory(session, sessions)

    assert not session.exists()
    assert (outsider / "keep.bin").is_file(), "联接目标被误删"
    remove_owned_directory(outsider, sessions)


def test_unknown_session_directory_is_preserved(managed):
    manager, catalog = managed
    directory = manager.tmp / "sessions" / "kantoku-unknown"
    directory.mkdir(parents=True)
    (directory / "fixture.bin").write_bytes(b"must keep")
    assert catalog.cleanup()["deleted"] == 0
    assert directory.is_dir()


def test_cache_order_uses_actual_access_time(managed, monkeypatch):
    manager, catalog = managed
    old, new = manager.cache / "old.bin", manager.cache / "new.bin"
    for path in (old, new):
        path.write_bytes(b"123456")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    catalog.register(new, "cache", "test", None)
    catalog.register(old, "cache", "test", None)
    monkeypatch.setitem(storage.CAPACITY, "cache", 6)
    assert catalog.cleanup()["released_bytes"] == 6
    assert not old.exists() and new.exists()
