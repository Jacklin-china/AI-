"""Shared runtime paths and atomic file staging, independent of business services."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from .errors import ConfigError
from .settings import ROOT


@lru_cache(maxsize=32)
def _resolved_forbidden(identities: tuple) -> tuple[Path, ...]:
    return tuple(Path(raw).expanduser().resolve() for raw, *_ in identities)


def _forbidden_paths() -> tuple[Path, ...]:
    candidates = [os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")]
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(os.environ["LOCALAPPDATA"]) / "Temp"))
    if os.name == "nt":
        candidates.append(str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "Temp"))
    identities = []
    for raw in sorted({raw for raw in candidates if raw}):
        try:
            info = Path(raw).stat()
            identities.append((raw, info.st_dev, info.st_ino))
        except FileNotFoundError:
            identities.append((raw, None, None))
    # Cache resolution, not validation. Target identity changes invalidate it,
    # including a Temp junction redirected while the process is alive.
    return _resolved_forbidden(tuple(identities))


def validate_runtime_path(path: Path) -> Path:
    """Reject OS temporary directories, including paths redirected by junctions."""
    resolved = path.expanduser().resolve()
    for forbidden in _forbidden_paths():
        if resolved.is_relative_to(forbidden):
            raise ConfigError("运行文件禁止写入系统 Temp 目录", detail=f"path={resolved}")
    return resolved


def process_alive(pid: int) -> bool:
    """判断进程是否仍在运行；无法确认时一律返回 True（保守保留）。

    Windows 上绝不能使用 ``os.kill(pid, 0)``：该调用会走 TerminateProcess，
    等于直接杀死目标进程。这里改用 OpenProcess + GetExitCodeProcess 只读查询。
    """
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes

            kernel = ctypes.windll.kernel32
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return kernel.GetLastError() == 5  # Access denied: preserve conservatively.
            code = ctypes.c_ulong()
            try:
                return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle),
                                                      ctypes.byref(code))) and code.value == 259
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _report_cleanup_failure(error: BaseException, *, component: str) -> None:
    """上报尽力而为的清理失败；上报本身失败也绝不向上抛。

    ``public_error`` 的入参签名是 ``Exception``，而删除拦截器抛出的 ``SystemExit``
    属于 ``BaseException``；这里统一兜住，保证清理失败只留下诊断，不改变调用方行为。
    """
    try:
        # 延迟导入：observability 经 logging_setup 反向依赖本模块。
        from .observability import public_error

        public_error(error, component=component)  # type: ignore[arg-type]
    except BaseException:  # noqa: BLE001 - 诊断失败不得影响主流程
        pass


@dataclass(frozen=True, slots=True)
class RuntimeManager:
    """One runtime root; existing explicitly configured non-Temp paths remain valid."""

    root: Path

    @property
    def data(self) -> Path:
        return validate_runtime_path(self.root / "data")

    @property
    def logs(self) -> Path:
        return validate_runtime_path(self.root / "logs")

    @property
    def cache(self) -> Path:
        return validate_runtime_path(self.root / "cache")

    @property
    def artifacts(self) -> Path:
        return validate_runtime_path(self.root / "artifacts")

    @property
    def tmp(self) -> Path:
        return validate_runtime_path(self.root / "tmp")

    @property
    def catalog(self):
        if _catalog_factory is None:
            raise ConfigError("Runtime 文件登记未初始化，请从标准项目入口运行")
        return _catalog_factory()

    def ensure_directory(self, directory: Path) -> Path:
        directory = validate_runtime_path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def register(self, path: Path, *, file_type: str, owner: str = "runtime",
                 expires_in: float | None = None) -> str:
        return self.catalog.register(validate_runtime_path(path), file_type, owner, expires_in)

    def remove_file(self, path: Path, *, file_type: str, if_registered: bool = False,
                    connection=None) -> None:
        self.catalog.remove_file(validate_runtime_path(path), file_type, if_registered,
                                 connection=connection)

    def iter_files(self, directory: Path) -> Iterator[Path]:
        """Inventories do not follow junctions into unmanaged locations."""
        directory = validate_runtime_path(directory)
        for current, folders, files in os.walk(directory, followlinks=False):
            folders[:] = [name for name in folders if (Path(current) / name).resolve()
                          == Path(current) / name and not (Path(current) / name).is_symlink()]
            for name in files:
                path = Path(current) / name
                if not path.is_symlink() and path.resolve() == path:
                    yield path

    def cleanup(self) -> dict[str, int]:
        return self.catalog.cleanup()

    def start_maintenance(self) -> RuntimeMaintenance:
        maintenance = RuntimeMaintenance(self)
        maintenance.start()
        return maintenance

    def create_test_session(self, prefix: str = "kantoku-test-") -> Path:
        require_permanent_test_cleanup()
        if not prefix.startswith("kantoku-") or not all(
            char.isalnum() or char == "-" for char in prefix
        ):
            raise ConfigError("测试目录名称无效")
        directory = self.tmp / "sessions" / f"{prefix}{uuid4().hex}"
        self.ensure_directory(directory)
        now = time.time()
        # A single durable ownership marker permits recovery after a killed test
        # process without writing its catalog into the production database.
        marker = {"file_id": uuid4().hex, "type": "test_session", "owner": "kantoku-tests",
                  "created_at": now, "expire_at": now + 86400, "status": "active",
                  "pid": os.getpid()}
        with (directory / ".kantoku-session.json").open("x", encoding="utf-8") as stream:
            json.dump(marker, stream)
        return directory

    def sweep_abandoned_test_sessions(self) -> int:
        """立刻删除上一次被中断的测试会话，不等待 24h 过期。

        测试进程退出即代表会话不再被使用，因此仅凭 owner + 进程存活即可回收，
        无需等待 ``expire_at``。仍存活的 PID（含并发运行的另一次 pytest）一律跳过，
        标记文件缺失、越界或非测试会话的目录也一律跳过，避免误删非测试数据。

        清理是尽力而为：单个目录失败只记录，绝不影响本次测试启动。这里额外捕获
        ``SystemExit``，因为宿主环境可能注入删除拦截器（超阈值时直接 SystemExit），
        它不是 ``Exception`` 子类，若漏掉会把整个 pytest 会话变成 INTERNALERROR。
        """
        sessions = self.tmp / "sessions"
        removed = 0
        for marker in sorted(sessions.glob("kantoku-*/.kantoku-session.json")):
            directory = marker.parent
            try:
                if marker.is_symlink() or marker.stat().st_size > 2048:
                    continue
                if validate_runtime_path(marker) != marker or directory.is_symlink():
                    continue
                record = json.loads(marker.read_text(encoding="utf-8"))
                if (record.get("owner"), record.get("type")) != ("kantoku-tests", "test_session"):
                    continue
                pid = record.get("pid")
                if type(pid) is not int or pid <= 0 or process_alive(pid):
                    continue
                require_permanent_test_cleanup()
                remove_owned_directory(directory, sessions)
                removed += 1
            except (Exception, SystemExit) as error:
                _report_cleanup_failure(error, component="runtime.test_session_sweep")
                if directory.is_dir():
                    print(f"Runtime cleanup: retained {directory.name} for manual review")
        return removed

    def resolve(self, configured: Path) -> Path:
        return validate_runtime_path(
            configured if configured.is_absolute() else self.root / configured
        )

    def prepare(self) -> None:
        for directory in (self.data, self.logs, self.cache, self.artifacts, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)
        key = (self.root, os.getpid())
        with _staging_lock:
            staging = _sdk_staging.get(key)
            if staging is None or not staging.exists():
                staging = self.ensure_directory(
                    self.tmp / "staging" / f"kantoku-sdk-{os.getpid()}-{uuid4().hex}"
                )
                now = time.time()
                with (staging / ".kantoku-session.json").open("x", encoding="utf-8") as stream:
                    json.dump({"type": "temp_directory", "owner": "kantoku-sdk",
                               "pid": os.getpid(), "status": "active", "created_at": now,
                               "expire_at": now + 86400}, stream)
                _sdk_staging[key] = staging
        # SDKs using Python's default staging also stay out of Windows Temp.
        tempfile.tempdir = str(staging)

    def claim_staging(self) -> None:
        """Keep the process lease until all SDK users have stopped."""
        self.prepare()
        key = (self.root, os.getpid())
        with _staging_lock:
            if key not in _sdk_leases:
                file_id = self.register(_sdk_staging[key], file_type="temp_directory",
                                        owner="kantoku-sdk", expires_in=86400)
                lease = self.catalog.lease(file_id)
                lease.__enter__()
                _sdk_leases[key] = (file_id, lease)

    def close_staging(self) -> None:
        key = (self.root, os.getpid())
        with _staging_lock:
            directory = _sdk_staging.get(key)
            owned = _sdk_leases.pop(key, None)
            if directory is None:
                return
            try:
                if owned:
                    owned[1].__exit__(None, None, None)
                remove_owned_directory(directory, self.tmp / "staging")
                if owned:
                    self.catalog.mark_deleted(owned[0])
                _sdk_staging.pop(key, None)
            except Exception as error:
                self.catalog.report_error(error)


RuntimePaths = RuntimeManager
_catalog_factory: Callable | None = None
_staging_lock = threading.RLock()
_sdk_staging: dict[tuple[Path, int], Path] = {}
_sdk_leases: dict[tuple[Path, int], tuple] = {}


def set_catalog_factory(factory: Callable) -> None:
    global _catalog_factory
    _catalog_factory = factory


def _configured_root() -> str | None:
    """显式配置的运行根目录；环境变量优先，其次是 settings.storage.runtime_root。

    配置不可读时回退项目根，保持与配置破损前一致的行为；真实原因由 doctor /
    Config 校验报告，不在这里吞掉。
    """
    override = os.environ.get("KANTOKU_RUNTIME_ROOT")
    if override:
        return override
    try:
        from .settings import get_settings

        configured = get_settings().storage.runtime_root
    except Exception:
        return None
    return str(configured) if configured else None


def get_runtime_paths() -> RuntimeManager:
    root = Path(_configured_root() or str(ROOT))
    if not root.is_absolute():
        root = ROOT / root
    return RuntimeManager(validate_runtime_path(root))


def get_test_runtime_paths() -> RuntimeManager:
    """Private test namespace outside source and production storage; no migration."""
    identity = hashlib.sha256(str(ROOT.resolve()).encode("utf-8")).hexdigest()[:16]
    # A short profile path also leaves room for SQLite sidecars on Windows.
    # Packaged apps can redirect LOCALAPPDATA to a path already over 100 chars.
    base = validate_runtime_path(Path.home() / ".kantoku-tests" / identity)
    current = get_runtime_paths()
    # Nested acceptance fixtures stay inside their already private outer session.
    if current.root.is_relative_to(base):
        return current
    if base.is_relative_to(ROOT.resolve()) or base == current.root:
        raise ConfigError("测试目录必须与源码和生产运行目录隔离")
    return RuntimeManager(base)


def require_permanent_test_cleanup() -> None:
    """Do not bypass host guards or create fixtures under redirected deletion."""
    expected = ((os.unlink, {"nt", "posix"}, None), (os.rmdir, {"nt", "posix"}, None),
                (shutil.rmtree, {"shutil"}, "shutil.py"),
                (Path.unlink, {"pathlib"}, "pathlib.py"))
    for operation, modules, filename in expected:
        code = getattr(operation, "__code__", None)
        genuine = code is None if filename is None else (
            code is not None and Path(code.co_filename).name == filename)
        if getattr(operation, "__module__", None) not in modules or not genuine:
            raise ConfigError("当前环境改写了文件删除行为，无法保证测试彻底清理；"
                              "请在未改写删除行为的本机终端运行测试")


def resolve_runtime_path(configured: Path) -> Path:
    return get_runtime_paths().resolve(configured)


@contextmanager
def temporary_file(*, directory: Path, suffix: str = ".tmp") -> Iterator[Path]:
    """Exclusive, centrally owned staging; always close before Windows replacement.

    Atomic replacement must stage on the destination volume. Such short-lived
    files are owned here, never named or created by a business module.
    """
    directory = validate_runtime_path(directory)
    manager = get_runtime_paths()
    manager.ensure_directory(directory)
    catalog = manager.catalog
    path = directory / f".kantoku-{uuid4().hex}{suffix}"
    file_id = manager.register(path, file_type="temp", expires_in=24 * 3600)
    with catalog.lease(file_id):
        try:
            with path.open("xb"):
                pass
            yield path
        finally:
            path.unlink(missing_ok=True)
            catalog.mark_deleted(file_id)


def atomic_write_bytes(path: Path, content: bytes, *, file_type: str = "artifact",
                       owner: str = "runtime", replace: bool = True) -> None:
    path = validate_runtime_path(path)
    manager = get_runtime_paths()
    directory = path.parent
    while not directory.exists():
        directory = directory.parent
    if shutil.disk_usage(directory).free < len(content) + 64 * 1024**2:
        raise OSError("可用磁盘空间不足，已停止新增文件；既有产物保留")
    file_id = manager.register(path, file_type=file_type, owner=owner)
    with manager.catalog.lease(file_id), temporary_file(directory=path.parent) as staged:
        staged.write_bytes(content)
        if replace:
            staged.replace(path)
        else:
            # Atomic no-clobber publication, including concurrent processes.
            os.link(staged, path)


def atomic_write_text(path: Path, content: str, *, file_type: str = "artifact",
                      owner: str = "runtime") -> None:
    atomic_write_bytes(path, content.encode("utf-8"), file_type=file_type, owner=owner)


def exclusive_write_text(path: Path, content: str, *, file_type: str = "recovery") -> None:
    manager = get_runtime_paths()
    path = validate_runtime_path(path)
    manager.ensure_directory(path.parent)
    file_id = manager.register(path, file_type=file_type)
    with manager.catalog.lease(file_id), path.open("x", encoding="utf-8") as stream:
        stream.write(content)


def probe_writable_directory(directory: Path) -> None:
    with temporary_file(directory=directory, suffix=".probe"):
        pass


@contextmanager
def temporary_directory(*, prefix: str) -> Iterator[Path]:
    manager = get_runtime_paths()
    staging = manager.ensure_directory(manager.tmp / "sessions")
    directory = staging / f"{prefix}{uuid4().hex}"
    file_id = manager.register(directory, file_type="temp_directory", expires_in=24 * 3600)
    with manager.catalog.lease(file_id):
        try:
            directory.mkdir()
            yield directory
        finally:
            manager.catalog.close_databases(directory)
            remove_owned_directory(directory, staging)
            manager.catalog.mark_deleted(file_id)


def _is_link_like(path: Path) -> bool:
    """符号链接或目录联接（Windows junction / 其它 reparse point）。"""
    if path.is_symlink():
        return True
    try:
        return bool(path.is_junction())
    except (OSError, AttributeError):
        # Python 3.11 has no Path.is_junction; Windows reparse points still must
        # never be followed into real dependencies or user data.
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        except OSError:
            return False


def _remove_tree(path: Path) -> None:
    """递归删除目录树，遇到链接只删除链接本身，绝不进入其目标。

    仓库里确实存在指向真实 ``frontend/node_modules`` 的目录联接（``data/qa/*`` 快照
    与 ``tmp/staged-check``）。``shutil.rmtree`` 只对顶层做符号链接判断，递归时不
    拒绝联接，一旦跟随就会删掉真实依赖，因此这里自己实现遍历而不是调用它。
    """
    if _is_link_like(path):
        os.rmdir(path) if path.is_dir() else path.unlink()
        return
    for entry in os.scandir(path):
        child = Path(entry.path)
        if _is_link_like(child):
            os.rmdir(child) if entry.is_dir(follow_symlinks=False) else os.unlink(child)
        elif entry.is_dir(follow_symlinks=False):
            _remove_tree(child)
        else:
            os.unlink(child)
    os.rmdir(path)


def remove_owned_directory(directory: Path, parent: Path) -> None:
    """Only private ephemeral session trees; never follow junctions/symlinks."""
    target = validate_runtime_path(directory)
    parent = validate_runtime_path(parent)
    if target == parent or not target.is_relative_to(parent):
        raise ConfigError("临时目录清理越界")
    if _is_link_like(directory):
        raise ConfigError("禁止清理重定向目录")
    if target.exists():
        _remove_tree(target)


@contextmanager
def isolated_runtime(*, prefix: str) -> Iterator[Path]:
    """Offline acceptance owns its entire runtime, never the production catalog."""
    from .settings import get_settings

    require_permanent_test_cleanup()
    parent = get_test_runtime_paths()
    directory = parent.create_test_session(prefix)
    previous = os.environ.get("KANTOKU_RUNTIME_ROOT")
    previous_staging = tempfile.tempdir
    settings = get_settings()
    database = settings.storage.sqlite_path
    os.environ["KANTOKU_RUNTIME_ROOT"] = str(directory)
    settings.storage.sqlite_path = Path("data/kantoku.db")
    manager = get_runtime_paths()
    try:
        manager.prepare()
        yield directory
    finally:
        try:
            manager.close_staging()
            if _catalog_factory is not None:
                manager.catalog.close_databases(directory)
            remove_owned_directory(directory, parent.tmp / "sessions")
        finally:
            settings.storage.sqlite_path = database
            if previous is None:
                os.environ.pop("KANTOKU_RUNTIME_ROOT", None)
            else:
                os.environ["KANTOKU_RUNTIME_ROOT"] = previous
            tempfile.tempdir = previous_staging


class RuntimeMaintenance:
    """File housekeeping only; not an Agent scheduler or workflow runtime."""

    def __init__(self, manager: RuntimeManager) -> None:
        self.manager = manager
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="runtime-storage", daemon=True)
        boundary = manager.catalog.initialized_at()
        self.legacy = {path for path in manager.iter_files(manager.cache)
                       if path.stat().st_mtime < boundary}

    def tick(self) -> None:
        # Reuse the existing maintenance loop for killed test processes, too.
        get_test_runtime_paths().sweep_abandoned_test_sessions()
        for name in ("ruff", "vite", "python", "pytest", "uv", "npm"):
            for path in self.manager.iter_files(self.manager.cache / name):
                if path.is_file() and not path.is_symlink() and path.resolve() not in self.legacy:
                    self.manager.register(path, file_type="cache", owner=name)
        self.manager.catalog.register_databases()
        self.manager.cleanup()

    def _run(self) -> None:
        while not self.stopped.wait(60):
            try:
                self.tick()
            except Exception as error:
                # The injected catalog reports through existing structured errors.
                self.manager.catalog.report_error(error)

    def start(self) -> None:
        self.manager.claim_staging()
        self.tick()
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join()
        self.tick()
