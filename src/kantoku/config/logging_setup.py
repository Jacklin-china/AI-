"""初始化双通道日志，确保工作目录变化不影响文件位置。"""

from __future__ import annotations

import sys

from loguru import logger

from kantoku.config import ConfigError, get_settings
from kantoku.config.settings import ROOT

LOG_DIR = ROOT / "data" / "logs"


def setup_logging(level: str | None = None) -> None:
    console_level = level if level is not None else get_settings().app.log_level
    try:
        logger.level(console_level)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except (ValueError, OSError) as error:
        raise ConfigError("日志初始化失败", detail=type(error).__name__) from None
    logger.remove()
    logger.add(
        sys.stderr,
        level=console_level,
        format=("<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}"),
        colorize=sys.stderr.isatty(),
        diagnose=False,
    )
    logger.add(
        LOG_DIR / "kantoku_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format=("{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}"),
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        diagnose=False,
    )
