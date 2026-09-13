from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from kantoku.config import ConfigError, logging_setup


def test_logging_reinitialization_has_no_duplicate_and_file_keeps_debug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")
    try:
        logging_setup.setup_logging("INFO")
        logging_setup.setup_logging("INFO")
        logger.info("console-marker")
        logger.debug("file-only-marker")
    finally:
        logger.remove()
    captured = capsys.readouterr()
    assert captured.err.count("console-marker") == 1
    assert "file-only-marker" not in captured.err
    assert "\x1b[" not in captured.err
    content = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "console-marker" in content
    assert "file-only-marker" in content


def test_invalid_log_level_preserves_existing_handler(tmp_path: Path) -> None:
    messages: list[str] = []
    handler_id = logger.add(lambda message: messages.append(str(message)))
    try:
        with pytest.raises(ConfigError):
            logging_setup.setup_logging("NOT-A-LEVEL")
        logger.info("handler-preserved")
        assert any("handler-preserved" in message for message in messages)
    finally:
        logger.remove(handler_id)
