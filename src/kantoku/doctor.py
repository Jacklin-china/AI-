"""Read-only environment diagnostics; live provider checks are always explicit."""

from __future__ import annotations

import os
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from kantoku.config import get_settings
from kantoku.config.observability import public_error
from kantoku.config.runtime_paths import get_runtime_paths, resolve_runtime_path
from kantoku.config.settings import ROOT
from kantoku.core.database import database_manager
from kantoku.core.llm import stream_chat, vision_chat
from kantoku.core.runtime.store import configured_database_path
from kantoku.shells.image_cli import _provider


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    # Never follow runtime-directory junctions into another user's files.
    for directory, folders, files in os.walk(path, followlinks=False):
        folders[:] = [name for name in folders if (Path(directory) / name).resolve()
                      == Path(directory) / name and not (Path(directory) / name).is_symlink()]
        for name in files:
            item = Path(directory) / name
            if not item.is_symlink():
                total += item.stat().st_size
    return total


def _failure(name: str, error: Exception) -> Check:
    failure = public_error(error, component="doctor")
    return Check(name, "BLOCKED", f"{error} · error_id={failure['error_id']}")


def runtime_checks() -> list[Check]:
    """No mkdir, schema migration, checkpoint, or orphan deletion during diagnosis."""
    checks: list[Check] = []
    try:
        paths = get_runtime_paths()
        directories = (paths.data, paths.logs, paths.cache, paths.artifacts, paths.tmp)
        database = configured_database_path()
    except Exception as error:
        return [_failure("Runtime Paths", error)]
    for directory in directories:
        try:
            nearest = directory
            while not nearest.exists() and nearest != nearest.parent:
                nearest = nearest.parent
            writable = os.access(nearest, os.W_OK) and nearest.is_dir()
            if directory.exists() and not directory.is_dir():
                writable = False
            checks.append(Check(
                f"Directory {directory.name}", "READY" if writable else "BLOCKED",
                f"{directory} · {'exists' if directory.exists() else 'not created yet'}",
            ))
        except Exception as error:
            checks.append(_failure(f"Directory {directory.name}", error))
    databases = {database}
    sidecars: set[Path] = set()
    scan_roots = set(directories) | {database.parent}
    try:
        for directory in scan_roots:
            if not directory.is_dir():
                continue
            for current, folders, files in os.walk(directory, followlinks=False):
                folders[:] = [name for name in folders if (Path(current) / name).resolve()
                              == Path(current) / name and not (Path(current) / name).is_symlink()]
                # Retained pytest fixtures include deliberately corrupt databases;
                # they are not application stores. Their size is still reported.
                if Path(current) == paths.cache:
                    folders[:] = [name for name in folders if name != "tests"]
                for name in files:
                    path = Path(current) / name
                    if name.endswith(("-wal", "-shm")):
                        sidecars.add(path)
                    elif path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                        databases.add(path)
    except Exception as error:
        checks.append(_failure("Database inventory", error))
    for path in sorted(databases):
        try:
            resolve_runtime_path(path)
            if not path.is_file():
                checks.append(Check("Database", "MISSING", f"{path} · 未创建数据库"))
                continue
            wal = Path(str(path) + "-wal")
            if wal.exists() and wal.stat().st_size:
                checks.append(Check(
                    "Database", "IN_USE", f"{path} · WAL={wal.stat().st_size} bytes；"
                    "请正常关闭后重查，不自动合并或删除",
                ))
                continue
            with closing(database_manager.connect(path, readonly=True)) as connection:
                result = connection.execute("PRAGMA quick_check").fetchall()
                with path.open("rb") as stream:
                    header = stream.read(20)
                mode = "wal" if header[18:20] == b"\x02\x02" else "delete"
            okay = result == [("ok",)]
            checks.append(Check(
                "Database", "READY" if okay else "BLOCKED",
                f"{path} · quick_check={result} · mode={mode}",
            ))
        except Exception as error:
            checks.append(_failure("Database", error))
    orphans = [path for path in sorted(sidecars) if not Path(str(path)[:-4]).is_file()]
    checks.append(Check(
        "Orphan WAL/SHM", "BLOCKED" if orphans else "READY",
        "; ".join(str(path) for path in orphans) if orphans else "0 orphan files",
    ))
    for name, directory, limit in (
        ("Log Size", paths.logs, 256 * 1024**2),
        ("Cache Size", paths.cache, 1024**3),
        ("Temporary Size", paths.tmp, 1024**3),
    ):
        try:
            size = _directory_size(directory)
            checks.append(Check(name, "WARNING" if size > limit else "READY",
                                f"{directory} · {size} bytes ({size / 1024**2:.2f} MiB)"))
        except Exception as error:
            checks.append(_failure(name, error))
    legacy_logs = paths.data / "logs"
    if legacy_logs.exists():
        checks.append(Check("Legacy Logs", "WARNING",
                            f"{legacy_logs} · {_directory_size(legacy_logs)} bytes；保留旧日志"))
    try:
        tracked = set()
        tracked_directories = set()
        expired = 0
        if database.is_file() and not (
            Path(str(database) + "-wal").exists()
            and Path(str(database) + "-wal").stat().st_size
        ):
            with closing(database_manager.connect(database, readonly=True)) as connection:
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='runtime_files'"
                ).fetchone():
                    import time

                    records = connection.execute(
                        "SELECT path,status,expire_at,type,owner FROM runtime_files"
                    ).fetchall()
                    tracked = {Path(row[0]) for row in records}
                    tracked_directories = {Path(row[0]) for row in records
                                           if row[1] == "active" and row[3] == "temp_directory"
                                           and row[4] == "kantoku-sdk"}
                    expired = sum(row[1] in {"active", "deleting"} and row[2] is not None
                                  and row[2] < time.time() for row in records)
                    pending = sum(row[1] == "deleting" for row in records)
                    checks.append(Check("Interrupted Cleanup", "WARNING" if pending else "READY",
                                        f"{pending} pending; 下一轮仅重试已登记临时文件"))
        unknown = sum(path.is_file() and path.resolve() not in tracked
                      and not any(path.resolve().is_relative_to(directory)
                                  for directory in tracked_directories)
                      for base in (paths.tmp, paths.cache, paths.logs)
                      for path in base.rglob("*") if not path.is_symlink())
        checks.append(Check("File Lifecycle", "WARNING" if expired or unknown else "READY",
                            f"expired={expired}, unclassified={unknown}; 未登记旧文件保留"))
        checks.append(Check("SDK Staging", "READY",
                            f"{len(tracked_directories)} owned directories; "
                            "进程租约保护，正常退出删除；死亡进程超过24小时回收"))
    except Exception as error:
        checks.append(_failure("File Lifecycle", error))
    return checks


def run_doctor(*, live: bool = False) -> list[Check]:
    """Check configuration and local resources without paid calls by default."""
    checks: list[Check] = []
    try:
        settings = get_settings()
        checks.append(Check("Config", "READY", "settings.yaml 已通过校验"))
    except Exception as error:
        return [Check("Config", "INVALID_CONFIG", type(error).__name__)]
    checks.extend(runtime_checks())
    for name, path in (
        ("Frontend Build", ROOT / "src" / "kantoku" / "shells" / "web" / "index.html"),
    ):
        checks.append(Check(name, "READY" if path.exists() else "BLOCKED", str(path)))
    provider_checks = (
        ("DeepSeek", settings.llm.api_key_env, settings.llm.model_chat, settings.llm.chat_api_key),
        (
            "Vision", settings.llm.vision_api_key_env,
            settings.llm.model_vision, settings.llm.vision_api_key,
        ),
        (
            "Jimeng" if settings.image.provider == "volcengine-jimeng" else "Qwen Image",
            (settings.image.access_key_env if settings.image.provider == "volcengine-jimeng"
             else settings.image.api_key_env),
            settings.image.model,
            (settings.image.access_key if settings.image.provider == "volcengine-jimeng"
             else settings.image.api_key),
        ),
    )
    for name, env_name, model, credential in provider_checks:
        try:
            configured = bool(credential())
        except Exception:
            configured = False
        detail = f"{model} · {'凭据已配置' if configured else f'缺少 {env_name}'}"
        status = "READY" if configured else "BLOCKED"
        if name == "Qwen Image" and configured:
            try:
                _provider().validate_request(
                    prompt="配置检查", shot_no=1, reference_urls=(), seed=None,
                )
            except Exception as error:
                status = "BLOCKED"
                detail += f" · 业务空间地址未就绪 ({type(error).__name__})"
        if live and status == "READY":
            try:
                if name == "DeepSeek":
                    list(stream_chat(
                        [{"role": "user", "content": "Reply exactly: OK"}], timeout_s=10
                    ))
                    detail += " · live text OK"
                elif name == "Vision":
                    vision_chat(
                        [{"role": "user", "content": "Reply exactly: OK"}], timeout_s=10
                    )
                    detail += " · live vision endpoint OK"
                else:
                    access = _provider().check_access()
                    if access.authenticated is False:
                        raise RuntimeError("provider authentication rejected")
                    if not access.service_ready:
                        status = "BLOCKED"
                        detail += " · no reliable free generation probe"
                    else:
                        detail += " · live query completed, no generation"
            except Exception as error:
                status = "BLOCKED"
                detail += f" · live {type(error).__name__}"
        checks.append(Check(name, status, detail))
    return checks


def print_doctor(*, live: bool = False) -> int:
    checks = run_doctor(live=live)
    print("Kantoku Doctor" + (" · LIVE requested" if live else " · safe local checks"))
    labels = {"READY": "Healthy", "WARNING": "Warning", "IN_USE": "Warning",
              "MISSING": "Warning"}
    for item in checks:
        print(f"[{labels.get(item.status, 'Error'):<7}] {item.name:<20} {item.detail}")
    failed = any(item.status not in labels for item in checks)
    warning = any(item.status != "READY" for item in checks)
    print("Status: " + ("Error" if failed else "Warning" if warning else "Healthy"))
    return 1 if failed else 0
