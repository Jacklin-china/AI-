"""Generic file catalog in the existing SQLite store; no workflow/domain rules."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from threading import RLock
from uuid import uuid4

from kantoku.config import get_settings
from kantoku.config.observability import public_error
from kantoku.config.runtime_paths import (
    get_runtime_paths,
    process_alive,
    remove_owned_directory,
    resolve_runtime_path,
    validate_runtime_path,
)
from kantoku.core.database import DatabaseManager

EPHEMERAL = {"temp", "temp_directory", "test_session", "cache", "log", "qa"}
CAPACITY = {"cache": 1024**3, "log": 256 * 1024**2, "qa": 256 * 1024**2}
_catalogs: dict[Path, FileCatalog] = {}
_lock = RLock()


class FileCatalog:
    def __init__(self, database: Path, databases: DatabaseManager) -> None:
        self.database = database
        self.databases = databases
        self._migrate()

    @classmethod
    def configured(cls, databases: DatabaseManager) -> FileCatalog:
        path = resolve_runtime_path(get_settings().storage.sqlite_path)
        with _lock:
            if path not in _catalogs:
                _catalogs[path] = cls(path, databases)
            return _catalogs[path]

    @contextmanager
    def connection(self):
        with closing(self.databases.connect(self.database)) as connection, connection:
            yield connection

    def _migrate(self) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='runtime_files'"
            ).fetchone():
                return
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if existing:
                directory = get_runtime_paths().ensure_directory(self.database.parent / "backups")
                backup = directory / f"{self.database.stem}.pre-runtime-policy-{uuid4().hex}.sqlite"
                # Read through another connection while this write lock excludes migrations.
                with closing(self.databases.connect(self.database)) as source, closing(
                    self.databases.connect(backup)
                ) as destination:
                    source.backup(destination)
                self.databases.checkpoint_all(within=backup)
            connection.execute(
                "CREATE TABLE runtime_files (file_id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, "
                "type TEXT NOT NULL, owner TEXT NOT NULL, created_at REAL NOT NULL, "
                "expire_at REAL, status TEXT NOT NULL, touched_at REAL NOT NULL, "
                "version INTEGER NOT NULL DEFAULT 1)"
            )
            connection.execute(
                "CREATE TABLE runtime_file_leases (lease_id TEXT PRIMARY KEY, "
                "file_id TEXT NOT NULL, pid INTEGER NOT NULL, expire_at REAL NOT NULL)"
            )
            connection.execute("CREATE INDEX runtime_expiry ON runtime_files(status,expire_at)")
            self._register(connection, self.database, "database", "database", None)
            if existing:
                self._register(connection, backup, "backup", "migration", None)

    @staticmethod
    def _register(connection, path: Path, kind: str, owner: str, expiry: float | None) -> str:
        row = connection.execute(
            "SELECT file_id,type,status FROM runtime_files WHERE path=?", (str(path),)
        ).fetchone()
        if row and row[2] == "deleting":
            raise RuntimeError("文件正在清理，请稍后重试")
        if row and row[1] != kind:
            raise RuntimeError("已登记文件的生命周期类型不可被覆盖")
        file_id = row[0] if row else uuid4().hex
        now = time.time()
        connection.execute(
            "INSERT INTO runtime_files VALUES (?,?,?,?,?,?,'active',?,1) "
            "ON CONFLICT(path) DO UPDATE SET touched_at=excluded.touched_at, "
            "expire_at=excluded.expire_at,status='active',version=runtime_files.version+1",
            (file_id, str(path), kind, owner, now, expiry, now),
        )
        return file_id

    def register(self, path: Path, kind: str, owner: str, expires_in: float | None) -> str:
        if kind in EPHEMERAL:
            root = get_runtime_paths()
            allowed = {
                "temp": (root.tmp, root.data, root.artifacts, root.cache, root.logs),
                "temp_directory": (root.tmp,), "test_session": (root.tmp,),
                "cache": (root.cache,), "log": (root.logs,), "qa": (root.tmp, root.cache),
            }[kind]
            if not any(path.is_relative_to(base) for base in allowed):
                # Atomic staging beside an explicitly chosen non-Temp export is also safe:
                # it is removed synchronously, never a candidate for automatic cleanup.
                if kind != "temp" or not path.name.startswith(".kantoku-"):
                    raise ValueError("可自动清理文件必须位于对应的受管理目录")
                kind = "export_staging"
                expires_in = None
        if expires_in is None:
            expires_in = {"temp": 86400, "temp_directory": 86400,
                          "test_session": 86400, "log": 7 * 86400, "qa": 7 * 86400}.get(kind)
        expiry = time.time() + expires_in if expires_in is not None else None
        with self.connection() as connection:
            file_id = self._register(connection, path, kind, owner, expiry)
            if kind == "cache" and path.is_file():
                connection.execute("UPDATE runtime_files SET touched_at=? WHERE file_id=?",
                                   (max(path.stat().st_mtime, path.stat().st_atime), file_id))
            return file_id

    def remove_file(self, path: Path, kind: str, if_registered: bool = False,
                    *, connection=None) -> None:
        if connection is None:
            with self.connection() as opened:
                self.remove_file(path, kind, if_registered, connection=opened)
            return
        row = connection.execute("SELECT file_id,type FROM runtime_files WHERE path=?",
                                 (str(path),)).fetchone()
        if row is None and if_registered:
            return
        if row is None or row[1] != kind:
            raise ValueError("仅允许删除当前操作已登记的文件")
        path.unlink(missing_ok=True)
        connection.execute("UPDATE runtime_files SET status='deleted',version=version+1 "
                           "WHERE file_id=?", (row[0],))

    @contextmanager
    def lease(self, file_id: str) -> Iterator[None]:
        lease_id = uuid4().hex
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM runtime_files WHERE file_id=?", (file_id,)
            ).fetchone()
            if not row or row[0] != "active":
                raise RuntimeError("文件生命周期已改变，不能开始写入")
            connection.execute("INSERT INTO runtime_file_leases VALUES (?,?,?,?)",
                               (lease_id, file_id, os.getpid(), time.time() + 86400))
        try:
            yield
        finally:
            with self.connection() as connection:
                connection.execute("DELETE FROM runtime_file_leases WHERE lease_id=?", (lease_id,))

    def mark_deleted(self, file_id: str) -> None:
        with self.connection() as connection:
            connection.execute("UPDATE runtime_files SET status='deleted',version=version+1 "
                               "WHERE file_id=?", (file_id,))

    def close_databases(self, directory: Path) -> None:
        results = self.databases.checkpoint_all(within=directory)
        if any(result.status != "READY" for result in results):
            raise RuntimeError("临时目录的数据库仍被占用，保留目录供重试")

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        return process_alive(pid)

    def cleanup(self) -> dict[str, int]:
        self._adopt_abandoned_sessions()
        now = time.time()
        released = deleted = failures = 0
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT file_id,path,type,expire_at,touched_at,version,status FROM runtime_files "
                "WHERE status IN ('active','deleting')"
            ).fetchall()
        sizes = {}
        for row in rows:
            path = Path(row[1])
            if row[2] in EPHEMERAL and path.is_file() and not path.is_symlink():
                sizes[row[0]] = path.stat().st_size
            elif row[2] in {"temp_directory", "test_session"} and path.is_dir():
                sizes[row[0]] = sum(item.stat().st_size
                                    for item in get_runtime_paths().iter_files(path))
        totals = {kind: sum(sizes.get(row[0], 0) for row in rows if row[2] == kind)
                  for kind in CAPACITY}
        for file_id, raw, kind, expiry, _touched, version, status in sorted(
            rows, key=lambda row: row[4]
        ):
            if kind not in EPHEMERAL:
                continue
            due = expiry is not None and expiry <= now
            over = kind in CAPACITY and totals[kind] > CAPACITY[kind]
            if not due and not over and status != "deleting":
                continue
            path = Path(raw)
            try:
                resolved = validate_runtime_path(path)
                root = get_runtime_paths()
                bases = (root.tmp, root.cache, root.logs, root.data, root.artifacts)
                if path.is_symlink() or resolved != path or not any(
                    path.is_relative_to(base) for base in bases
                ):
                    raise ValueError("清理路径被重定向或越界，已保留")
                with self.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    leases = connection.execute(
                        "SELECT lease_id,pid FROM runtime_file_leases WHERE file_id=?", (file_id,)
                    ).fetchall()
                    if any(self._pid_alive(pid) for _, pid in leases):
                        continue
                    connection.execute("DELETE FROM runtime_file_leases WHERE file_id=?",
                                       (file_id,))
                    changed = connection.execute(
                        "UPDATE runtime_files SET status='deleting',version=version+1 "
                        "WHERE file_id=? AND version=? AND status IN ('active','deleting')",
                        (file_id, version),
                    ).rowcount
                if not changed:
                    continue
                size = sizes.get(file_id, 0)
                if kind in {"temp_directory", "test_session"}:
                    self.close_databases(path)
                    remove_owned_directory(path, root.tmp)
                else:
                    path.unlink(missing_ok=True)
                self.mark_deleted(file_id)
                released += size
                deleted += 1
                if kind in totals:
                    totals[kind] -= size
            except Exception as error:
                public_error(error, component="runtime.cleanup")
                failures += 1
        # Keep tombstones briefly for diagnostics, never accumulate per-write rows forever.
        with self.connection() as connection:
            connection.execute("DELETE FROM runtime_files WHERE status='deleted' AND touched_at<? "
                               "AND file_id NOT IN (SELECT file_id FROM runtime_file_leases)",
                               (now - 86400,))
        return {"deleted": deleted, "released_bytes": released, "failures": failures}

    @staticmethod
    def report_error(error: Exception) -> None:
        public_error(error, component="runtime.maintenance")

    def register_databases(self) -> None:
        for path in self.databases.paths():
            self.register(path, "database", "database", None)

    def initialized_at(self) -> float:
        with self.connection() as connection:
            return connection.execute("SELECT MIN(created_at) FROM runtime_files").fetchone()[0]

    def _adopt_abandoned_sessions(self) -> None:
        root = get_runtime_paths()
        markers = [*(root.tmp / "sessions").glob("kantoku-*/.kantoku-session.json"),
                   *(root.tmp / "staging").glob("kantoku-sdk-*/.kantoku-session.json")]
        for marker in markers:
            try:
                if marker.is_symlink() or marker.stat().st_size > 2048:
                    continue
                if validate_runtime_path(marker) != marker:
                    continue
                record = json.loads(marker.read_text(encoding="utf-8"))
                expected = ("kantoku-sdk", "temp_directory") if (
                    marker.parent.parent == root.tmp / "staging"
                ) else ("kantoku-tests", "test_session")
                if ((record.get("owner"), record.get("type")) != expected
                    or record.get("status") != "active"
                    or type(record.get("pid")) is not int or record["pid"] <= 0
                    or not isinstance(record.get("created_at"), (int, float))
                    or not isinstance(record.get("expire_at"), (int, float))):
                    continue
                if (record["created_at"] > time.time() - 86400
                        or record["expire_at"] > time.time()
                        or self._pid_alive(record["pid"])):
                    continue
                with self.connection() as connection:
                    self._register(connection, marker.parent, expected[1], expected[0],
                                   record["expire_at"])
            except (OSError, ValueError, TypeError) as error:
                public_error(error, component="runtime.session_recovery")
