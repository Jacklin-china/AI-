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


def test_native_exception_keeps_sanitized_chained_traceback_in_both_sinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("KANTOKU_TEST_API_KEY", "trace-private-credential-456")
    try:
        logging_setup.setup_logging("INFO")
        try:
            try:
                raise ValueError("trace-private-credential-456 upstream failure")
            except ValueError as cause:
                raise RuntimeError("image parser failed") from cause
        except RuntimeError:
            logger.bind(trace_id="trace-native", run_id="run-native", task_id="image-native",
                        provider="qwen", model="configured-model").exception("image_failure")
    finally:
        logger.remove()
    console = capsys.readouterr().err
    file = (tmp_path / "logs/kantoku.log").read_text(encoding="utf-8")
    for content in (console, file):
        for fragment in ("test_logging.py", "ValueError", "RuntimeError", "Traceback",
                         "trace-native", "run-native", "image-native", "configured-model"):
            assert fragment in content
        assert "trace-private-credential-456" not in content


def test_error_conversion_keeps_original_identifier_and_http_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")
    try:
        logging_setup.setup_logging("INFO")
        response = MagicMock(status_code=401, request=MagicMock(), headers={"x-request-id": "up-1"})
        error = AuthenticationError("request rejected", response=response,
                                    body={"error": {"code": "InvalidKey", "message": "denied"}})
        first = public_error(error, trace_id="trace-http", provider="qwen",
                             model="qwen-image-2.0-pro", base_url="https://image.example/api/v1")
        assert public_error(error) == first
    finally:
        logger.remove()
    rows = [json.loads(line)["record"] for line in
            (tmp_path / "logs/kantoku.log").read_text(encoding="utf-8").splitlines()]
    failures = [row for row in rows if row["extra"]["error_id"] == first["error_id"]]
    assert len(failures) == 1
    assert failures[0]["extra"]["http_status"] == "401"
    assert failures[0]["extra"]["provider_error_code"] == "InvalidKey"
    assert failures[0]["extra"]["provider_error_message"] == "denied"
