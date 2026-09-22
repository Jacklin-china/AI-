"""SQLite 持久化与向前兼容迁移。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from kantoku.config import ToolError, get_settings
from kantoku.config.observability import current_trace_id
from kantoku.config.settings import ROOT
from kantoku.core.conversations import (
    ConversationMessageRecord,
    ConversationRecord,
    InteractionMode,
    MessageRole,
    MessageType,
)

from .models import (
    BATCH_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    ApprovalDecision,
    ApprovalRecord,
    ArtifactRecord,
    ArtifactType,
    BatchRecord,
    BatchStatus,
    CheckpointRecord,
    ExecutionStatus,
    NodeExecutionRecord,
    RunRecord,
    RuntimeEventRecord,
    RuntimeEventType,
    SkillExecutionRecord,
    utc_now,
)

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS core_schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, domain TEXT NOT NULL, workflow TEXT NOT NULL,
            status TEXT NOT NULL, state_json TEXT NOT NULL, current_node TEXT NOT NULL,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
            error TEXT, cost_fen INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS node_executions (
            run_id TEXT NOT NULL, node_id TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT, completed_at TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
            error TEXT, outputs_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (run_id, node_id),
            FOREIGN KEY (run_id) REFERENCES runs(id)
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, run_id TEXT NOT NULL,
            node_id TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
            location TEXT, version INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (run_id) REFERENCES runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, created_at);
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
            decision TEXT NOT NULL, request_json TEXT NOT NULL DEFAULT '{}',
            response_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            decided_at TEXT, FOREIGN KEY (run_id) REFERENCES runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_pending
            ON approvals(decision, created_at);
        CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            node_id TEXT NOT NULL, next_node TEXT NOT NULL, status TEXT NOT NULL,
            state_json TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id, id);
        CREATE TABLE IF NOT EXISTS skill_executions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
            skill_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
            completed_at TEXT, error TEXT, outputs_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (run_id) REFERENCES runs(id)
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
            concurrency_limit INTEGER NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batch_runs (
            batch_id TEXT NOT NULL, run_id TEXT NOT NULL, position INTEGER NOT NULL,
            PRIMARY KEY (batch_id, run_id),
            UNIQUE (batch_id, position),
            FOREIGN KEY (batch_id) REFERENCES batches(id),
            FOREIGN KEY (run_id) REFERENCES runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_batch_runs_run ON batch_runs(run_id);
        """,
    ),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL, event_type TEXT NOT NULL, node_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            UNIQUE (run_id, sequence),
            FOREIGN KEY (run_id) REFERENCES runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_run_events_stream ON run_events(run_id, sequence);
        """,
    ),
    (
        4,
        """
        ALTER TABLE batches ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, interaction_mode TEXT NOT NULL,
            domain TEXT, active_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY (active_run_id) REFERENCES runs(id)
        );
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
            type TEXT NOT NULL, content TEXT NOT NULL, run_id TEXT, event_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id),
            FOREIGN KEY (run_id) REFERENCES runs(id),
            UNIQUE (conversation_id, event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_updated
            ON conversations(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_conversation_messages
            ON conversation_messages(conversation_id, created_at);
        """,
    ),
    (
        6,
        """
        ALTER TABLE conversations ADD COLUMN deleted_at TEXT;
        CREATE INDEX IF NOT EXISTS idx_conversations_visible
            ON conversations(deleted_at, updated_at DESC);
        """,
    ),
)


def configured_database_path() -> Path:
    """按项目配置解析数据库绝对路径。"""
    path = get_settings().storage.sqlite_path
    return path if path.is_absolute() else ROOT / path


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ToolError("Core 数据库中的 JSON 状态不是对象")
    return loaded


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class RuntimeStore:
    """Core 数据仓库；所有写入均为短事务。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or configured_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def create_conversation(
        self,
        title: str = "新对话",
        *,
        interaction_mode: InteractionMode = InteractionMode.AUTONOMOUS,
        domain: str | None = None,
    ) -> ConversationRecord:
        """Create a durable conversation independently from runtime runs."""
        now = utc_now()
        record = ConversationRecord(
            id=f"conversation-{uuid4().hex}", title=title.strip() or "新对话",
            interaction_mode=interaction_mode, domain=domain,
            created_at=now, updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations "
                "(id,title,interaction_mode,domain,active_run_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (record.id, record.title, record.interaction_mode.value, record.domain,
                 None, now.isoformat(), now.isoformat()),
            )
        return record

    def list_conversations(
        self, limit: int = 50, *, domain: str | None = None, query: str = ""
    ) -> list[ConversationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations WHERE deleted_at IS NULL "
                "AND (? IS NULL OR domain=?) AND title LIKE ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (domain, domain, f"%{query}%", limit),
            ).fetchall()
        return [self._conversation(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> ConversationRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=? AND deleted_at IS NULL", (conversation_id,)
            ).fetchone()
        if row is None:
            raise ToolError("找不到指定对话", detail=conversation_id)
        return self._conversation(row)

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        domain: str | None = None,
        active_run_id: str | None = None,
    ) -> ConversationRecord:
        current = self.get_conversation(conversation_id)
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversations SET title=?,domain=?,active_run_id=?,updated_at=? "
                "WHERE id=?",
                (title if title is not None else current.title,
                 domain if domain is not None else current.domain,
                 active_run_id if active_run_id is not None else current.active_run_id,
                 now.isoformat(), conversation_id),
            )
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> None:
        """Hide only the conversation; Runs, artifacts and ledgers remain untouched."""
        self.get_conversation(conversation_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversations SET deleted_at=? WHERE id=?",
                (utc_now().isoformat(), conversation_id),
            )

    def add_conversation_message(
        self,
        conversation_id: str,
        *,
        role: MessageRole,
        type: MessageType,
        content: str,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> ConversationMessageRecord:
        self.get_conversation(conversation_id)
        record = ConversationMessageRecord(
            id=f"message-{uuid4().hex}", conversation_id=conversation_id,
            role=role, type=type, content=content, run_id=run_id,
            event_id=event_id, created_at=utc_now(),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                (record.id, record.conversation_id, record.role.value, record.type.value,
                 record.content, record.run_id, record.event_id,
                 record.created_at.isoformat()),
            )
            connection.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?",
                (record.created_at.isoformat(), conversation_id),
            )
        return record

    def list_conversation_messages(
        self, conversation_id: str
    ) -> list[ConversationMessageRecord]:
        self.get_conversation(conversation_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversation_messages WHERE conversation_id=? "
                "ORDER BY created_at,id", (conversation_id,),
            ).fetchall()
        return [ConversationMessageRecord(
            id=row["id"], conversation_id=row["conversation_id"], role=row["role"],
            type=row["type"], content=row["content"], run_id=row["run_id"],
            event_id=row["event_id"], created_at=_time(row["created_at"]),
        ) for row in rows]

    @staticmethod
    def _conversation(row: sqlite3.Row) -> ConversationRecord:
        return ConversationRecord(
            id=row["id"], title=row["title"], interaction_mode=row["interaction_mode"],
            domain=row["domain"], active_run_id=row["active_run_id"],
            created_at=_time(row["created_at"]), updated_at=_time(row["updated_at"]),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except (sqlite3.Error, OSError) as error:
            raise ToolError("Core 数据库操作失败", detail=type(error).__name__) from error
        finally:
            if "connection" in locals():
                connection.close()

    def migrate(self) -> None:
        """以只增不删方式升级旧数据库。"""
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS core_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM core_schema_migrations")
            }
            for version, script in MIGRATIONS:
                if version in applied:
                    continue
                connection.executescript(script)
                connection.execute(
                    "INSERT INTO core_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now().isoformat()),
                )

    def create_run(
        self, domain: str, workflow: str, state: dict[str, Any], current_node: str
    ) -> RunRecord:
        """创建待执行 Run。"""
        now = utc_now()
        record = RunRecord(
            id=f"run-{uuid4().hex}", domain=domain, workflow=workflow,
            status=ExecutionStatus.PENDING, state=state, current_node=current_node,
            started_at=now, updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs(id,domain,workflow,status,state_json,current_node,"
                "started_at,updated_at,cost_fen) VALUES (?,?,?,?,?,?,?,?,?)",
                (record.id, domain, workflow, record.status, _dump(state), current_node,
                 now.isoformat(), now.isoformat(), 0),
            )
        return record

    def update_run(
        self,
        run_id: str,
        *,
        status: ExecutionStatus,
        state: dict[str, Any],
        current_node: str,
        error: str | None = None,
        cost_fen: int | None = None,
        force: bool = False,
    ) -> RunRecord:
        """原子更新 Run 状态；终态默认不可被普通写入回退。"""
        current = self.get_run(run_id)
        if (
            not force
            and current.status in TERMINAL_STATUSES
            and status not in TERMINAL_STATUSES
        ):
            return current
        now = utc_now()
        completed = now.isoformat() if status in {
            ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED
        } else None
        with self._connect() as connection:
            if cost_fen is None:
                connection.execute(
                    "UPDATE runs SET status=?,state_json=?,current_node=?,updated_at=?,"
                    "completed_at=?,error=? WHERE id=?",
                    (status, _dump(state), current_node, now.isoformat(), completed, error, run_id),
                )
            else:
                connection.execute(
                    "UPDATE runs SET status=?,state_json=?,current_node=?,updated_at=?,"
                    "completed_at=?,error=?,cost_fen=? WHERE id=?",
                    (status, _dump(state), current_node, now.isoformat(), completed, error,
                     cost_fen, run_id),
                )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        """读取 Run；不存在时返回统一 ToolError。"""
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ToolError("找不到指定 Run", detail=run_id)
        return self._run(row)

    def set_run_cost(self, run_id: str, cost_fen: int) -> RunRecord:
        """以 Core 为费用事实源更新 Run，并写入可恢复事件。"""
        if type(cost_fen) is not int or cost_fen < 0:
            raise ToolError("Run 费用必须是非负整数分")
        current = self.get_run(run_id)
        if current.cost_fen == cost_fen:
            return current
        with self._connect() as connection:
            connection.execute(
                "UPDATE runs SET cost_fen=?,updated_at=? WHERE id=?",
                (cost_fen, utc_now().isoformat(), run_id),
            )
        self.append_event(
            run_id, RuntimeEventType.COST_UPDATED,
            payload={"cost_fen": cost_fen, "delta_fen": cost_fen - current.cost_fen},
        )
        return self.get_run(run_id)

    def list_runs(
        self,
        limit: int = 100,
        *,
        domain: str | None = None,
        status: ExecutionStatus | None = None,
    ) -> list[RunRecord]:
        """按更新时间倒序列出 Run，可按领域与状态过滤。"""
        clauses: list[str] = []
        params: list[object] = []
        if domain:
            clauses.append("domain=?")
            params.append(domain)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs{where} ORDER BY updated_at DESC LIMIT ?", params
            ).fetchall()
        return [self._run(row) for row in rows]

    def _run(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"], domain=row["domain"], workflow=row["workflow"],
            status=row["status"], state=_load(row["state_json"]),
            current_node=row["current_node"], started_at=_time(row["started_at"]),
            updated_at=_time(row["updated_at"]), completed_at=_time(row["completed_at"]),
            error=row["error"], cost_fen=row["cost_fen"],
        )

    def save_node(self, record: NodeExecutionRecord) -> None:
        """新增或更新一个 Node 的可观测状态。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO node_executions VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id,node_id) DO UPDATE SET status=excluded.status,"
                "started_at=COALESCE(node_executions.started_at,excluded.started_at),"
                "completed_at=excluded.completed_at,retry_count=excluded.retry_count,"
                "error=excluded.error,outputs_json=excluded.outputs_json",
                (record.run_id, record.node_id, record.status,
                 record.started_at.isoformat() if record.started_at else None,
                 record.completed_at.isoformat() if record.completed_at else None,
                 record.retry_count, record.error, _dump(record.outputs)),
            )

    def list_nodes(self, run_id: str) -> list[NodeExecutionRecord]:
        """按开始时间返回节点执行记录。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM node_executions WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [NodeExecutionRecord(
            run_id=row["run_id"], node_id=row["node_id"], status=row["status"],
            started_at=_time(row["started_at"]), completed_at=_time(row["completed_at"]),
            retry_count=row["retry_count"], error=row["error"],
            outputs=_load(row["outputs_json"]),
        ) for row in rows]

    def save_checkpoint(
        self, run_id: str, node_id: str, next_node: str,
        status: ExecutionStatus, state: dict[str, Any],
    ) -> CheckpointRecord:
        """保存一次不可变节点边界快照。"""
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO checkpoints(run_id,node_id,next_node,status,state_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, node_id, next_node, status, _dump(state), now.isoformat()),
            )
            checkpoint_id = int(cursor.lastrowid)
        return CheckpointRecord(
            id=checkpoint_id, run_id=run_id, node_id=node_id, next_node=next_node,
            status=status, state=state, created_at=now,
        )

    def latest_checkpoint(self, run_id: str) -> CheckpointRecord | None:
        """读取最后一个可恢复检查点。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return CheckpointRecord(
            id=row["id"], run_id=row["run_id"], node_id=row["node_id"],
            next_node=row["next_node"], status=row["status"],
            state=_load(row["state_json"]), created_at=_time(row["created_at"]),
        )

    def create_approval(
        self, run_id: str, node_id: str, request: dict[str, Any]
    ) -> ApprovalRecord:
        """为等待节点创建或复用 pending 审批。"""
        existing = self.approval_for_node(run_id, node_id)
        if existing is not None and (
            existing.decision is ApprovalDecision.PENDING or existing.request == request
        ):
            return existing
        record = ApprovalRecord(
            id=f"approval-{uuid4().hex}", run_id=run_id, node_id=node_id,
            decision=ApprovalDecision.PENDING, request=request, created_at=utc_now(),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO approvals(id,run_id,node_id,decision,request_json,response_json,"
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (record.id, run_id, node_id, record.decision, _dump(request), "{}",
                 record.created_at.isoformat()),
            )
        self.append_event(
            run_id, RuntimeEventType.APPROVAL_REQUIRED, node_id=node_id,
            payload={"approval_id": record.id, "request": request},
        )
        return record

    def approval_for_node(self, run_id: str, node_id: str) -> ApprovalRecord | None:
        """读取某节点最近一次审批。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE run_id=? AND node_id=? "
                "ORDER BY created_at DESC LIMIT 1", (run_id, node_id),
            ).fetchone()
        return self._approval(row) if row else None

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        """按 ID 读取审批；供幂等决策在恢复工作流前检查当前状态。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
        if row is None:
            raise ToolError("审批不存在", detail=approval_id)
        return self._approval(row)

    def decide_approval(
        self, approval_id: str, decision: ApprovalDecision,
        response: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """一次性提交审批决定。"""
        if decision is ApprovalDecision.PENDING:
            raise ToolError("不能把审批重新设为 pending")
        now = utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if existing is None:
                raise ToolError("审批不存在", detail=approval_id)
            current = self._approval(existing)
            if current.decision is decision:
                return current
            if current.decision is not ApprovalDecision.PENDING:
                raise ToolError("审批已经按其他决定处理", detail=approval_id)
            cursor = connection.execute(
                "UPDATE approvals SET decision=?,response_json=?,decided_at=? "
                "WHERE id=? AND decision=?",
                (decision, _dump(response or {}), now.isoformat(), approval_id,
                 ApprovalDecision.PENDING),
            )
            if cursor.rowcount != 1:
                raise ToolError("审批已经被其他请求处理", detail=approval_id)
            row = connection.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
        resolved = self._approval(row)
        self.append_event(
            resolved.run_id, RuntimeEventType.APPROVAL_RESOLVED, node_id=resolved.node_id,
            payload={"approval_id": resolved.id, "decision": resolved.decision.value},
        )
        return resolved

    def list_approvals(self, pending_only: bool = False) -> list[ApprovalRecord]:
        """查询审批队列。"""
        query = "SELECT * FROM approvals"
        params: tuple[str, ...] = ()
        if pending_only:
            query += " WHERE decision=?"
            params = (ApprovalDecision.PENDING,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._approval(row) for row in rows]

    def _approval(self, row: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            id=row["id"], run_id=row["run_id"], node_id=row["node_id"],
            decision=row["decision"], request=_load(row["request_json"]),
            response=_load(row["response_json"]), created_at=_time(row["created_at"]),
            decided_at=_time(row["decided_at"]),
        )

    def save_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        """保存 Artifact；ID 重复会明确失败。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (record.id, record.type, record.run_id, record.node_id, record.source,
                 record.status, record.created_at.isoformat(), _dump(record.metadata),
                 record.location, record.version),
            )
        self.append_event(
            record.run_id, RuntimeEventType.ARTIFACT_CREATED, node_id=record.node_id,
            payload={
                "artifact_id": record.id, "type": record.type.value,
                "source": record.source, "location": record.location,
                "version": record.version,
            },
        )
        return record

    def create_artifact(
        self, *, type: ArtifactType, run_id: str, node_id: str, source: str,
        status: str = "ready", metadata: dict[str, Any] | None = None,
        location: str | None = None, version: int = 1,
    ) -> ArtifactRecord:
        """构建并保存通用产物。"""
        return self.save_artifact(ArtifactRecord(
            id=f"artifact-{uuid4().hex}", type=type, run_id=run_id, node_id=node_id,
            source=source, status=status, created_at=utc_now(), metadata=metadata or {},
            location=location, version=version,
        ))

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        """读取一个 Artifact。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise ToolError("找不到指定 Artifact", detail=artifact_id)
        return self._artifact(row)

    def list_artifacts(
        self,
        run_id: str | None = None,
        *,
        type: ArtifactType | None = None,
        domain: str | None = None,
    ) -> list[ArtifactRecord]:
        """查询 Artifact，可按 run、type 与 domain 过滤。"""
        clauses: list[str] = []
        params: list[object] = []
        if run_id:
            clauses.append("artifacts.run_id=?")
            params.append(run_id)
        if type:
            clauses.append("artifacts.type=?")
            params.append(type)
        if domain:
            clauses.append("runs.domain=?")
            params.append(domain)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT artifacts.* FROM artifacts JOIN runs ON runs.id=artifacts.run_id"
                f"{where} ORDER BY artifacts.created_at DESC", params,
            ).fetchall()
        return [self._artifact(row) for row in rows]

    @staticmethod
    def _artifact(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            id=row["id"], type=ArtifactType(row["type"]), run_id=row["run_id"],
            node_id=row["node_id"], source=row["source"], status=row["status"],
            created_at=_time(row["created_at"]), metadata=_load(row["metadata_json"]),
            location=row["location"], version=row["version"],
        )

    def save_skill_execution(self, record: SkillExecutionRecord) -> None:
        """记录 Skill 调用结果。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO skill_executions VALUES (?,?,?,?,?,?,?,?,?)",
                (record.id, record.run_id, record.node_id, record.skill_id, record.status,
                 record.started_at.isoformat(),
                 record.completed_at.isoformat() if record.completed_at else None,
                record.error, _dump(record.outputs)),
            )

    def create_batch(self, name: str, concurrency_limit: int) -> BatchRecord:
        """创建空 Batch。"""
        if concurrency_limit < 1:
            raise ToolError("Batch 并发限制必须大于零")
        now = utc_now()
        record = BatchRecord(
            id=f"batch-{uuid4().hex}", name=name, status=BatchStatus.PENDING,
            concurrency_limit=concurrency_limit, created_at=now, updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO batches(id,name,status,concurrency_limit,created_at,"
                "updated_at,version) "
                "VALUES (?,?,?,?,?,?,?)",
                (record.id, name, record.status, concurrency_limit,
                 now.isoformat(), now.isoformat(), record.version),
            )
        return record

    def add_batch_run(self, batch_id: str, run_id: str, position: int) -> None:
        """将已有 Run 关联到 Batch。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO batch_runs(batch_id,run_id,position) VALUES (?,?,?)",
                (batch_id, run_id, position),
            )

    def update_batch_status(
        self, batch_id: str, status: BatchStatus, *, force: bool = False
    ) -> BatchRecord:
        """更新 Batch 状态；终态默认不会被重新计算覆盖。"""
        current = self.get_batch(batch_id)
        if not force and current.status is status:
            return current
        terminal = current.status in BATCH_TERMINAL_STATUSES
        if not force and terminal and status not in BATCH_TERMINAL_STATUSES:
            return current
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE batches SET status=?,updated_at=?,version=version+1 WHERE id=?",
                (status, utc_now().isoformat(), batch_id),
            )
            if cursor.rowcount != 1:
                raise ToolError("找不到指定 Batch", detail=batch_id)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> BatchRecord:
        """读取 Batch 及其 Run ID。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
            run_rows = connection.execute(
                "SELECT run_id FROM batch_runs WHERE batch_id=? ORDER BY position",
                (batch_id,),
            ).fetchall()
        if row is None:
            raise ToolError("找不到指定 Batch", detail=batch_id)
        return BatchRecord(
            id=row["id"], name=row["name"], status=row["status"],
            concurrency_limit=row["concurrency_limit"],
            created_at=_time(row["created_at"]), updated_at=_time(row["updated_at"]),
            version=row["version"],
            run_ids=[item["run_id"] for item in run_rows],
        )

    def list_batches(self) -> list[BatchRecord]:
        """按更新时间倒序查询 Batch。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM batches ORDER BY updated_at DESC"
            ).fetchall()
        return [self.get_batch(row["id"]) for row in rows]

    def batch_ids_for_run(self, run_id: str) -> list[str]:
        """返回包含指定 Run 的 Batch，用于写入进度事件。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT batch_id FROM batch_runs WHERE run_id=? ORDER BY batch_id",
                (run_id,),
            ).fetchall()
        return [str(row["batch_id"]) for row in rows]

    def append_event(
        self,
        run_id: str,
        event_type: RuntimeEventType,
        *,
        node_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RuntimeEventRecord:
        """追加一条事件；sequence 在同一 Run 内单调递增。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS seq FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            sequence = int(row["seq"]) + 1
            record = RuntimeEventRecord(
                id=0, run_id=run_id, sequence=sequence, event_type=event_type,
                node_id=node_id, payload=payload or {}, created_at=utc_now(),
            )
            cursor = connection.execute(
                "INSERT INTO run_events(run_id,sequence,event_type,node_id,payload_json,"
                "created_at) VALUES (?,?,?,?,?,?)",
                (run_id, sequence, event_type.value, node_id,
                 _dump(record.payload), record.created_at.isoformat()),
            )
            record = record.model_copy(update={"id": int(cursor.lastrowid or 0)})
        logger.bind(
            component="runtime-event", trace_id=current_trace_id() or "-",
            run_id=run_id, node_id=node_id or "-",
        ).info("event={} sequence={}", event_type.value, sequence)
        return record

    def list_events(
        self, run_id: str, *, after: int = 0, limit: int = 500
    ) -> list[RuntimeEventRecord]:
        """按 sequence 升序读取事件，用于恢复时间线与断线续传。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id=? AND sequence>? "
                "ORDER BY sequence ASC LIMIT ?",
                (run_id, after, limit),
            ).fetchall()
        return [
            RuntimeEventRecord(
                id=row["id"], run_id=row["run_id"], sequence=row["sequence"],
                event_type=RuntimeEventType(row["event_type"]), node_id=row["node_id"],
                payload=_load(row["payload_json"]), created_at=_time(row["created_at"]),
            )
            for row in rows
        ]
