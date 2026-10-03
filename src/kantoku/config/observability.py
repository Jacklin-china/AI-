"""Safe public errors with a shared identifier for UI and detailed logs."""

from __future__ import annotations

import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import Any
from uuid import uuid4

from loguru import logger
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError

from .errors import BudgetError, ConfigError, KantokuError
from .logging_setup import redact_secrets


class ErrorKind(StrEnum):
    NEEDS_USER_ACTION = "needs_user_action"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    FATAL = "fatal"


_TRACE_ID: ContextVar[str | None] = ContextVar("kantoku_trace_id", default=None)
_RUN_ID: ContextVar[str | None] = ContextVar("kantoku_run_id", default=None)


def current_run_id() -> str | None:
    """Return the active workflow Run ID for durable provider records."""
    return _RUN_ID.get()


@contextmanager
def run_trace(run_id: str, node_id: str) -> Iterator[None]:
    """Correlate a node's budget and provider work with its Run."""
    token = _RUN_ID.set(run_id)
    try:
        with logger.contextualize(run_id=run_id, node_id=node_id):
            yield
    finally:
        _RUN_ID.reset(token)


def current_trace_id() -> str | None:
    """Return the request correlation ID inherited by this execution context."""
    return _TRACE_ID.get()


@contextmanager
def request_trace(trace_id: str) -> Iterator[None]:
    """Keep the same ID through API and worker calls (TaskRunner copies context)."""
    token = _TRACE_ID.set(trace_id)
    try:
        with logger.contextualize(trace_id=trace_id):
            yield
    finally:
        _TRACE_ID.reset(token)


def classify_error(error: Exception) -> ErrorKind:
    """Classify the underlying failure without exposing provider messages."""
    if isinstance(error, BudgetError):
        return ErrorKind.NEEDS_USER_ACTION
    if isinstance(error, ConfigError):
        return ErrorKind.BLOCKED
    if isinstance(error, APIStatusError):
        if error.status_code in {401, 403}:
            return ErrorKind.BLOCKED
        if error.status_code == 404:
            return ErrorKind.NOT_FOUND
        if error.status_code == 409:
            return ErrorKind.CONFLICT
        if error.status_code in {408, 429, 502, 503, 504}:
            return ErrorKind.RETRYABLE
    if isinstance(error, (ConnectionError, TimeoutError, APIConnectionError, APITimeoutError)):
        return ErrorKind.RETRYABLE
    if isinstance(error, (ValueError, KeyError, ValidationError)):
        return ErrorKind.INVALID_INPUT
    if isinstance(error, FileNotFoundError):
        return ErrorKind.NOT_FOUND
    if isinstance(error.__cause__, Exception) and error.__cause__ is not error:
        return classify_error(error.__cause__)
    return ErrorKind.FATAL


def public_error(error: Exception, **context: Any) -> dict[str, Any]:
    """Log traceback once and return a prompt/key-safe client payload."""
    saved = getattr(error, "_kantoku_public_failure", None)
    if isinstance(saved, dict) and saved.get("error_id"):
        return saved
    # Only read public diagnostic fields, never raw request headers or bodies.
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for attribute, key in (
            ("status_code", "http_status"), ("request_id", "provider_request_id"),
            ("provider_error_code", "provider_error_code"),
            ("provider_error_message", "provider_error_message"),
        ):
            value = getattr(current, attribute, None)
            if isinstance(value, (str, int)):
                context.setdefault(key, redact_secrets(str(value))[:1000])
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            detail = body.get("error", body)
            if isinstance(detail, dict):
                for key in ("code", "message"):
                    value = detail.get(key)
                    if isinstance(value, (str, int)):
                        context.setdefault(
                            f"provider_error_{key}", redact_secrets(str(value))[:1000],
                        )
        current = current.__cause__
    error_id = f"ERR-{uuid4().hex[:8].upper()}"
    kind = classify_error(error)
    safe_message = (
        redact_secrets(error.message) if isinstance(error, KantokuError) else "任务执行失败"
    )
    stack = redact_secrets("".join(traceback.format_exception(error)))
    trace_id = str(context.pop("trace_id", None) or current_trace_id() or "-")
    logger.bind(error_id=error_id, trace_id=trace_id, **context).error(
        "operation failed error_id={} kind={} exception={} detail={} "
        "scope={} traceback={} cause={}",
        error_id, kind.value, type(error).__name__, redact_secrets(str(error))[:500],
        {key: value for key, value in context.items()
         if key in {"project_id", "run_id", "task_id", "asset_id", "brief_version", "model",
                    "provider", "base_url", "request_id", "provider_request_id", "http_status",
                    "provider_error_code", "provider_error_message", "latency_ms", "phase"}},
        stack,
        type(error.__cause__).__name__ if error.__cause__ else "-",
    )
    failure = {
        "error_id": error_id,
        "trace_id": trace_id,
        "error_kind": kind.value,
        "safe_message": safe_message,
        "retryable": kind is ErrorKind.RETRYABLE,
    }
    error._kantoku_public_failure = failure
    return failure
