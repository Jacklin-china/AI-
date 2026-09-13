from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from kantoku.core import tracing as tracing_module
from kantoku.core.tracing import Trace, init_trace_table, recent_traces, write_trace


@pytest.fixture
def trace_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "nested" / "trace.db"
    monkeypatch.setattr(tracing_module, "_database_path", lambda: path)
    return path


def _trace(*, ts: str, ok: bool, error: str | None = None) -> Trace:
    return Trace(
        ts=ts,
        kind="llm.chat",
        model="test-model",
        in_tokens=10,
        out_tokens=20,
        latency_ms=500,
        cost_fen=None,
        ok=ok,
        shot_no=None,
        error=error,
    )


def test_init_trace_table_is_idempotent(trace_db: Path) -> None:
    init_trace_table()
    init_trace_table()

    connection = sqlite3.connect(trace_db)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'trace'"
        ).fetchone()
    finally:
        connection.close()

    assert row == ("trace",)


def test_write_and_read_success_and_failure_traces(trace_db: Path) -> None:
    success = _trace(ts="2026-09-11T10:00:00+00:00", ok=True)
    failure = _trace(
        ts="2026-09-11T10:01:00+00:00",
        ok=False,
        error="provider's timeout",
    )

    write_trace(success)
    write_trace(failure)
    traces = recent_traces()

    assert trace_db.exists()
    assert traces == [failure, success]
    assert traces[0].ok is False
    assert traces[0].error == "provider's timeout"


def test_recent_traces_applies_limit_and_returns_empty_database(trace_db: Path) -> None:
    assert recent_traces(limit=2) == []

    for minute in range(3):
        write_trace(_trace(ts=f"2026-09-11T10:0{minute}:00+00:00", ok=True))

    traces = recent_traces(limit=2)

    assert len(traces) == 2
    assert traces[0].ts == "2026-09-11T10:02:00+00:00"


def test_trace_rejects_invalid_data_and_limit(trace_db: Path) -> None:
    with pytest.raises(ValueError, match="失败 trace 必须提供 error"):
        _trace(ts="2026-09-11T10:00:00+00:00", ok=False)

    with pytest.raises(ValueError, match="limit 必须大于 0"):
        recent_traces(limit=0)

    assert not trace_db.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("in_tokens", -1),
        ("in_tokens", None),
        ("out_tokens", 1.5),
        ("latency_ms", True),
        ("cost_fen", True),
        ("cost_fen", float("nan")),
        ("shot_no", 0),
        ("shot_no", "1"),
        ("ok", 1),
        ("ts", None),
    ],
)
def test_trace_rejects_wrong_runtime_types(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(_trace(ts="2026-09-11T10:00:00+00:00", ok=True), **{field: value})


def test_failed_trace_rejects_blank_error() -> None:
    with pytest.raises(ValueError, match="失败 trace 必须提供 error"):
        _trace(ts="2026-09-11T10:00:00+00:00", ok=False, error="  ")


def test_tied_timestamps_use_insertion_order(trace_db: Path) -> None:
    first = _trace(ts="2026-09-11T10:00:00+00:00", ok=True)
    second = _trace(ts=first.ts, ok=False, error="timeout")
    write_trace(first)
    write_trace(second)
    assert recent_traces(2) == [second, first]


def test_legacy_database_upgrade_preserves_records_and_other_tables(trace_db: Path) -> None:
    trace_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(trace_db)
    try:
        connection.execute(
            "CREATE TABLE trace (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, "
            "model TEXT, in_tokens INTEGER, out_tokens INTEGER, latency_ms INTEGER, "
            "cost_fen INTEGER, ok INTEGER, shot_no INTEGER, error TEXT)"
        )
        connection.execute(
            "INSERT INTO trace VALUES (42, ?, 'llm.chat', 'old', 0, 0, 20, NULL, 1, NULL, NULL)",
            ("2026-09-10T10:00:00+00:00",),
        )
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep-me')")
        connection.commit()
    finally:
        connection.close()
    init_trace_table()
    init_trace_table()
    legacy = recent_traces()[0]
    assert legacy.model == "old"
    assert legacy.usage_reported is None
    write_trace(replace(_trace(ts="2026-09-11T10:00:00+00:00", ok=True), usage_reported=True))
    connection = sqlite3.connect(trace_db)
    try:
        assert connection.execute("SELECT id FROM trace ORDER BY id").fetchall() == [(42,), (43,)]
        assert connection.execute("SELECT value FROM unrelated").fetchone() == ("keep-me",)
    finally:
        connection.close()


@pytest.mark.parametrize("reported", [True, False, None])
def test_usage_marker_roundtrip(trace_db: Path, reported: bool | None) -> None:
    record = replace(
        _trace(ts="2026-09-11T10:00:00+00:00", ok=True),
        in_tokens=0,
        out_tokens=0,
        usage_reported=reported,
    )
    write_trace(record)
    assert recent_traces()[0] == record


def test_usage_marker_rejects_conflicting_values() -> None:
    record = _trace(ts="2026-09-11T10:00:00+00:00", ok=True)
    with pytest.raises(ValueError):
        replace(record, usage_reported="true")
    with pytest.raises(ValueError):
        replace(record, usage_reported=False)
