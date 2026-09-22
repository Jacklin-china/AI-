"""初始化双通道日志，确保工作目录变化不影响文件位置。"""

from __future__ import annotations

import os
import re
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values
from loguru import logger

from kantoku.config import ConfigError, get_settings
from kantoku.config.settings import ENV_PATH, ROOT

LOG_DIR = ROOT / "data" / "logs"

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
    record["message"] = redact_secrets(str(record["message"]))
    extra = record["extra"]
    if isinstance(extra, dict):
        for key, value in tuple(extra.items()):
            extra[key] = redact_secrets(str(value))
    # Loguru's native exception object can include unsanitized provider text.
    # public_error() records safe traceback frames and the error ID explicitly.
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
    try:
        logger.level(console_level)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
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
                "<cyan>{module}:{line}</cyan> | {message}"),
        colorize=None,
        diagnose=False,
        filter=_console_filter,
    )
    logger.add(
        LOG_DIR / "kantoku.log",
        level="DEBUG",
        format="{message}",
        serialize=True,
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        diagnose=False,
        filter=_patch_record,
    )
    logger.configure(extra={
        "component": "app", "trace_id": "-", "conversation_id": "-", "run_id": "-", "node_id": "-",
        "message_id": "-", "skill": "-", "provider": "-", "provider_request_id": "-",
        "request_id": "-", "error_id": "-",
    })


def archive_development_log() -> Path | None:
    """Archive only the active application log; never touch audit/ledger data."""
    active = LOG_DIR / "kantoku.log"
    if not active.is_file():
        return None
    if active.resolve().parent != LOG_DIR.resolve():
        raise ConfigError("开发日志路径无效")
    archive = LOG_DIR / f"kantoku.{datetime.now(UTC):%Y-%m-%d_%H-%M-%S_%f}.log"
    logger.remove()
    active.rename(archive)
    setup_logging()
    return archive
