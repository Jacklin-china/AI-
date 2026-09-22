from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from loguru import logger
from openai import AuthenticationError, RateLimitError

from kantoku.config import ConfigError, logging_setup
from kantoku.config.observability import classify_error, public_error, request_trace


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


def test_structured_log_redacts_env_value_and_correlates_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("KANTOKU_TEST_API_KEY", "unique-private-value-123")
    try:
        logging_setup.setup_logging("INFO")
        with request_trace("trace-test-123"):
            logger.bind(component="provider", provider="test").info(
                "private={}", "unique-private-value-123",
            )
            try:
                raise RuntimeError("private exception unique-private-value-123")
            except RuntimeError as error:
                failure = public_error(error, component="provider")
    finally:
        logger.remove()
    content = (tmp_path / "logs" / "kantoku.log").read_text(encoding="utf-8")
    assert "unique-private-value-123" not in content
    records = [json.loads(line)["record"] for line in content.splitlines()]
    assert all(record["time"]["repr"] for record in records)
    assert all(record["level"]["name"] for record in records)
    assert any(record["extra"]["component"] == "provider" for record in records)
    assert failure["trace_id"] == "trace-test-123"
    assert any(failure["error_id"] == record["extra"]["error_id"] for record in records)
    assert any("test_logging.py" in record["message"] for record in records)
    assert any("Traceback (most recent call last)" in record["message"] for record in records)


def test_error_classification_and_development_log_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        logging_setup, "get_settings",
        lambda: SimpleNamespace(app=SimpleNamespace(log_level="INFO")),
    )
    try:
        logging_setup.setup_logging()
        logger.info("before archive")
        archived = logging_setup.archive_development_log()
        assert archived is not None and archived.is_file()
        assert "before archive" in archived.read_text(encoding="utf-8")
        assert (tmp_path / "logs" / "kantoku.log").is_file()
    finally:
        logger.remove()
    assert classify_error(TimeoutError()).value == "retryable"
    assert classify_error(ValueError()).value == "invalid_input"
    assert classify_error(FileNotFoundError()).value == "not_found"
    assert classify_error(RuntimeError()).value == "fatal"
    response = MagicMock(status_code=401, request=MagicMock(), headers={})
    assert classify_error(
        AuthenticationError("auth", response=response, body=None)
    ).value == "blocked"
    response = MagicMock(status_code=429, request=MagicMock(), headers={})
    assert classify_error(
        RateLimitError("limit", response=response, body=None)
    ).value == "retryable"
