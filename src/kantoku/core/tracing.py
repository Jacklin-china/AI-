"""把模型和工具调用保存为可查询的结构化运行记录。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kantoku.config import TracingError, get_settings
from kantoku.config.settings import ROOT

_CREATE_TRACE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT,
    in_tokens INTEGER NOT NULL CHECK (in_tokens >= 0),
    out_tokens INTEGER NOT NULL CHECK (out_tokens >= 0),
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    cost_fen INTEGER CHECK (cost_fen IS NULL OR cost_fen >= 0),
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    shot_no INTEGER CHECK (shot_no IS NULL OR shot_no > 0),
    error TEXT,
    usage_reported INTEGER CHECK (usage_reported IN (0, 1))
)
"""

_INSERT_TRACE_SQL = """
INSERT INTO trace (
    ts, kind, model, in_tokens, out_tokens,
    latency_ms, cost_fen, ok, shot_no, error, usage_reported
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_RECENT_TRACES_SQL = """
SELECT
    ts, kind, model, in_tokens, out_tokens,
    latency_ms, cost_fen, ok, shot_no, error, usage_reported
FROM trace
ORDER BY ts DESC, id DESC
LIMIT ?
"""


@dataclass(frozen=True, slots=True)
class Trace:
    ts: str
    kind: str
    model: str | None
    in_tokens: int
    out_tokens: int
    latency_ms: int
    cost_fen: int | None
    ok: bool
    shot_no: int | None
    error: str | None
    usage_reported: bool | None = None

    def __post_init__(self) -> None:
        """阻止明显不可能的埋点数据进入数据库。"""
        for name in ("ts", "kind"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} 必须是字符串")
        for name in ("model", "error"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} 必须是字符串或 None")
        for name in ("in_tokens", "out_tokens", "latency_ms", "cost_fen", "shot_no"):
            value = getattr(self, name)
            if value is None and name in {"cost_fen", "shot_no"}:
                continue
            # bool 是 int 的子类；精确比较，避免 True 被当成 1 分。
            if type(value) is not int:
                raise ValueError(f"{name} 必须是整数")
        if type(self.ok) is not bool:
            raise ValueError("ok 必须是布尔值")
        if self.usage_reported is not None and type(self.usage_reported) is not bool:
            raise ValueError("usage_reported 必须是布尔值或 None")
        if self.usage_reported is False and (self.in_tokens != 0 or self.out_tokens != 0):
            raise ValueError("未报告 usage 时 token 占位必须为 0")
        if not self.ts.strip():
            raise ValueError("ts 不能为空")
        if not self.kind.strip():
            raise ValueError("kind 不能为空")
        if self.in_tokens < 0 or self.out_tokens < 0 or self.latency_ms < 0:
            raise ValueError("token 和耗时不能为负数")
        if self.cost_fen is not None and self.cost_fen < 0:
            raise ValueError("cost_fen 不能为负数")
        if self.shot_no is not None and self.shot_no <= 0:
            raise ValueError("shot_no 必须大于 0")
        if self.ok and self.error is not None:
            raise ValueError("成功 trace 的 error 必须是 None")
        if not self.ok and (self.error is None or not self.error.strip()):
            raise ValueError("失败 trace 必须提供 error")


def _database_path() -> Path:
    """把配置中的相对路径稳定地解析到项目根目录。"""
    configured_path = get_settings().storage.sqlite_path
    if configured_path.is_absolute():
        return configured_path
    return ROOT / configured_path


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    """提供自动提交、回滚和关闭的 SQLite 连接。"""
    path = _database_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    except (OSError, sqlite3.Error) as error:
        raise TracingError(
            "无法打开 trace 数据库",
            detail=f"path={path}；error={type(error).__name__}",
        ) from error

    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise TracingError(
            "trace 数据库操作失败",
            detail=f"path={path}；error={type(error).__name__}",
        ) from error
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_trace_table(connection: sqlite3.Connection) -> None:
    """兼容升级仅加列，历史用量标记保持 NULL，不猜测也不删旧记录。"""
    connection.execute(_CREATE_TRACE_TABLE_SQL)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(trace)")}
    if "usage_reported" not in columns:
        try:
            connection.execute(
                "ALTER TABLE trace ADD COLUMN usage_reported INTEGER "
                "CHECK (usage_reported IN (0, 1))"
            )
        except sqlite3.OperationalError:
            # 另一个进程可能已在检查后加列；仅在列确实存在时继续。
            current = {row["name"] for row in connection.execute("PRAGMA table_info(trace)")}
            if "usage_reported" not in current:
                raise


def init_trace_table() -> None:
    """幂等创建 trace 表。"""
    with _connection() as connection:
        _ensure_trace_table(connection)


def write_trace(trace: Trace) -> None:
    """参数化写入一条 trace；表不存在时自动创建。"""
    values = (
        trace.ts,
        trace.kind,
        trace.model,
        trace.in_tokens,
        trace.out_tokens,
        trace.latency_ms,
        trace.cost_fen,
        int(trace.ok),
        trace.shot_no,
        trace.error,
        None if trace.usage_reported is None else int(trace.usage_reported),
    )
    with _connection() as connection:
        _ensure_trace_table(connection)
        connection.execute(_INSERT_TRACE_SQL, values)


def recent_traces(limit: int = 50) -> list[Trace]:
    """按时间和插入顺序倒序读取最近的 trace。"""
    if type(limit) is not int:
        raise ValueError("limit 必须是整数")
    if limit <= 0:
        raise ValueError("limit 必须大于 0")

    with _connection() as connection:
        _ensure_trace_table(connection)
        rows = connection.execute(_SELECT_RECENT_TRACES_SQL, (limit,)).fetchall()

    return [
        Trace(
            ts=row["ts"],
            kind=row["kind"],
            model=row["model"],
            in_tokens=row["in_tokens"],
            out_tokens=row["out_tokens"],
            latency_ms=row["latency_ms"],
            cost_fen=row["cost_fen"],
            ok=bool(row["ok"]),
            shot_no=row["shot_no"],
            error=row["error"],
            usage_reported=(None if row["usage_reported"] is None else bool(row["usage_reported"])),
        )
        for row in rows
    ]
