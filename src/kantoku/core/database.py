"""Single SQLite connection factory and graceful WAL checkpoint boundary."""

from __future__ import annotations

import atexit
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import uuid4

from kantoku.config import ConfigError, get_settings
from kantoku.config.observability import public_error
from kantoku.config.runtime_paths import (
    get_runtime_paths,
    resolve_runtime_path,
    validate_runtime_path,
)


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    path: Path
    status: str
    detail: str


class _ManagedConnection(sqlite3.Connection):
    """Track explicit close without changing SQLite's transaction context semantics."""

    manager: DatabaseManager | None = None
    managed_path: Path | None = None

    def close(self) -> None:
        super().close()
        if self.manager is not None and self.managed_path is not None:
            self.manager._closed(self.managed_path)
            self.manager = None


class DatabaseManager:
    """No schemas or domain rules; callers retain their existing transactions."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._connections: dict[Path, int] = {}
        self._anchors: dict[Path, sqlite3.Connection] = {}
        self._pid = os.getpid()

    def connect(
        self, path: Path, *, timeout: float = 30, readonly: bool = False,
        foreign_keys: bool = False,
    ) -> sqlite3.Connection:
        path = resolve_runtime_path(path)
        if not readonly:
            paths = get_runtime_paths()
            if not (path.is_relative_to(paths.root / "data")
                    or path.is_relative_to(paths.root / "tmp")):
                configured = resolve_runtime_path(get_settings().storage.sqlite_path)
                if not (path == configured or path.is_relative_to(configured.parent / "backups")):
                    raise ConfigError("数据库必须位于 data/、隔离测试目录或明确配置的旧存储位置")
        for suffix in ("-wal", "-shm", "-journal"):
            validate_runtime_path(Path(str(path) + suffix))
        with self._lock:
            if self._pid != os.getpid():
                raise RuntimeError("DatabaseManager must be created after process fork")
            if readonly:
                # Never open an active WAL immutably: it would hide committed pages.
                wal = Path(str(path) + "-wal")
                if wal.exists() and wal.stat().st_size:
                    raise sqlite3.OperationalError("数据库存在 WAL，请先正常关闭写入进程")
                target = path.as_uri() + "?mode=ro&immutable=1"
            else:
                target = str(path)
                if path not in self._anchors:
                    get_runtime_paths().ensure_directory(path.parent)
                    if len(self._anchors) >= 256:
                        raise sqlite3.OperationalError("数据库连接容量已满，请关闭不用的数据库")
                    anchor = sqlite3.connect(target, timeout=timeout, check_same_thread=False)
                    try:
                        anchor.execute("PRAGMA temp_store = MEMORY")
                        anchor.execute("PRAGMA journal_mode = WAL")
                        # Open the WAL pager even for a newly empty database, so the
                        # anchor actually retains the shared-memory/WAL handles.
                        anchor.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
                    except Exception:
                        anchor.close()
                        raise
                    self._anchors[path] = anchor
            connection = sqlite3.connect(
                target, timeout=timeout, uri=readonly, factory=_ManagedConnection,
            )
            try:
                connection.execute("PRAGMA temp_store = MEMORY")
                if foreign_keys:
                    connection.execute("PRAGMA foreign_keys = ON")
                if not readonly:
                    connection.manager = self
                    connection.managed_path = path
                    self._connections[path] = self._connections.get(path, 0) + 1
                return connection
            except Exception:
                connection.close()
                raise

    def backup(self, path: Path, *, owner: str) -> Path:
        """Consistent SQLite backup, preserving active WAL; no live-file replacement."""
        path = resolve_runtime_path(path)
        manager = get_runtime_paths()
        directory = manager.ensure_directory(path.parent / "backups")
        destination = directory / f"{path.stem}-{uuid4().hex}.sqlite"
        with closing(self.connect(path)) as source, closing(self.connect(destination)) as target:
            source.backup(target)
        self.checkpoint_all(within=destination)
        manager.register(destination, file_type="backup", owner=owner)
        return destination

    def paths(self) -> tuple[Path, ...]:
        with self._lock:
            return tuple(self._anchors)

    def _closed(self, path: Path) -> None:
        with self._lock:
            self._connections[path] -= 1

    def checkpoint_all(self, *, within: Path | None = None) -> list[DatabaseStatus]:
        """Call only after HTTP requests and workers drain. Never unlink WAL/SHM."""
        results: list[DatabaseStatus] = []
        with self._lock:
            for path, anchor in list(self._anchors.items()):
                if within is not None and not path.is_relative_to(within.resolve()):
                    continue
                active = self._connections.get(path, 0)
                if not path.exists():
                    continue
                if active:
                    error = RuntimeError(f"{path}: {active} connections remain open")
                    failure = public_error(error, component="database.shutdown")
                    results.append(DatabaseStatus(path, "BUSY", str(failure)))
                    continue
                try:
                    anchor.execute("PRAGMA busy_timeout = 2000")
                    busy, frames, checkpointed = anchor.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if busy:
                        raise sqlite3.OperationalError(
                            f"{path}: WAL checkpoint busy ({checkpointed}/{frames})"
                        )
                    results.append(DatabaseStatus(path, "READY", "wal_checkpoint(TRUNCATE)"))
                except Exception as error:
                    failure = public_error(error, component="database.shutdown")
                    results.append(DatabaseStatus(path, "BLOCKED", str(failure)))
                finally:
                    anchor.close()
                    del self._anchors[path]
                    self._connections.pop(path, None)
        return results


database_manager = DatabaseManager()


def _file_catalog():
    # Lazy injection keeps Config independent of Core and avoids import-time writes.
    from kantoku.core.runtime.storage import FileCatalog

    return FileCatalog.configured(database_manager)


from kantoku.config.runtime_paths import set_catalog_factory  # noqa: E402

set_catalog_factory(_file_catalog)
# Handles legacy CLI/module entry points too; ThreadPoolExecutor joins precede atexit.
atexit.register(database_manager.checkpoint_all)
