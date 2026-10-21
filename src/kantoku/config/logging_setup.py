"""初始化双通道日志，确保工作目录变化不影响文件位置。"""

from __future__ import annotations

import os
import re
import sys
import traceback
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values
from loguru import logger

from kantoku.config import ConfigError, get_settings
from kantoku.config.runtime_paths import get_runtime_paths, validate_runtime_path
from kantoku.config.settings import ENV_PATH

# Optional override used by isolated tests; production resolves at initialization.
LOG_DIR: Path | None = None


def _log_dir() -> Path:
    return validate_runtime_path(LOG_DIR) if LOG_DIR is not None else get_runtime_paths().logs

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)((?:authorization|cookie|set-cookie|token|password|secret[_-]?key|secret|"
        r"api[_-]?key|access[_-]?key)\s*[:=]\s*)[^\s,;]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def _known_secrets() -> set[str]:
    """Read sensitive values without ever including them in a log record."""
    names = re.compile(r"(?i)(key|token|password|secret|cookie|authorization)")
    values = {value for key, value in os.environ.items() if names.search(key) and len(value) >= 8}
    with suppress(OSError):
        values.update(
            value for key, value in dotenv_values(ENV_PATH, encoding="utf-8").items()
            if names.search(key) and value and len(value) >= 8
        )
    return values


def redact_secrets(message: str) -> str:
    """Remove common credential shapes before they reach any sink."""
    value = message
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            value,
        )
    for secret in sorted(_known_secrets(), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def _patch_record(record: dict[str, object]) -> bool:
    exception = record.get("exception")
    if exception is not None:
        # Keep frames and chained causes, never Loguru's local-variable diagnostics.
        stack = "".join(traceback.format_exception(*exception))
        record["message"] = f"{record['message']}\n{stack}"
    record["message"] = redact_secrets(str(record["message"]))
    extra = record["extra"]
    if isinstance(extra, dict):
        for key, value in tuple(extra.items()):
            extra[key] = redact_secrets(str(value))
    # Loguru's native exception object can include unsanitized provider text.
    # The sanitized stack above replaces the unsafe native exception object.
    record["exception"] = None
    return True


def _console_filter(record: dict[str, object]) -> bool:
    """Console-only filter: redact first, then drop frontend polling noise."""
    if not _patch_record(record):
        return False
    extra = record["extra"]
    message = str(record["message"])
    # 前端每 1-2 秒轮询 /api/runs、/api/approvals 产生的 start/end 噪音
    # 仍写入 kantoku.log，只是不刷屏控制台。
    return not (
        extra.get("component") == "api"
        and ("request start" in message or "request end" in message)
    )


def setup_logging(level: str | None = None) -> None:
    console_level = level if level is not None else get_settings().app.log_level
    directory = _log_dir()
    try:
        logger.level(console_level)
        get_runtime_paths().prepare()
        validate_runtime_path(directory / "kantoku.log")
        get_runtime_paths().ensure_directory(directory)
    except (ValueError, OSError) as error:
        raise ConfigError("日志初始化失败", detail=type(error).__name__) from None
    # 控制台级别配色：INFO 蓝 / WARNING 黄 / ERROR 红 / CRITICAL 红加粗
    logger.level("DEBUG", color="<dim><blue>")
    logger.level("INFO", color="<blue>")
    logger.level("WARNING", color="<yellow>")
    logger.level("ERROR", color="<red>")
    logger.level("CRITICAL", color="<red><bold>")
    logger.remove()
    logger.add(
        sys.stderr,
        level=console_level,
        format=("<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
                "{extra[component]} | trace={extra[trace_id]} | "
                "project={extra[project_id]} run={extra[run_id]} task={extra[task_id]} | "
                "error={extra[error_id]} provider={extra[provider]} model={extra[model]} | "
                "<cyan>{file.path}:{function}:{line}</cyan> | {message}"),
        colorize=None,
        diagnose=False,
        filter=_console_filter,
    )
    legacy = {path.resolve() for path in directory.glob("kantoku*.log")}
    get_runtime_paths().register(directory / "kantoku.log", file_type="active_log",
                                 owner="application")

    def retain_rotated(files: list[str]) -> None:
        manager = get_runtime_paths()
        for raw in files:
            path = Path(raw).resolve()
            if path not in legacy and path.parent == directory:
                kind = "log" if directory == manager.logs else "qa"
                if directory == manager.logs or directory.is_relative_to(manager.tmp):
                    manager.register(path, file_type=kind, owner="application")

    logger.add(
        directory / "kantoku.log",
        level="DEBUG",
        format="{message}",
        serialize=True,
        rotation="10 MB",
        retention=retain_rotated,
        encoding="utf-8",
        diagnose=False,
        filter=_patch_record,
    )
    logger.configure(extra={
        "component": "app", "trace_id": "-", "conversation_id": "-", "run_id": "-", "node_id": "-",
        "message_id": "-", "skill": "-", "provider": "-", "provider_request_id": "-",
        "request_id": "-", "error_id": "-", "project_id": "-", "task_id": "-",
        "model": "-",
    })


def archive_development_log() -> Path | None:
    """Archive only the active application log; never touch audit/ledger data."""
    directory = _log_dir()
    active = directory / "kantoku.log"
    if not active.is_file():
        return None
    if active.resolve().parent != directory.resolve():
        raise ConfigError("开发日志路径无效")
    archive = directory / f"kantoku.{datetime.now(UTC):%Y-%m-%d_%H-%M-%S_%f}.log"
    logger.remove()
    active.rename(archive)
    manager = get_runtime_paths()
    kind = "log" if directory == manager.logs else (
        "qa" if directory.is_relative_to(manager.tmp) else "audit"
    )
    manager.register(archive, file_type=kind, owner="application")
    setup_logging()
    return archive
