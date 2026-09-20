"""Kantoku Core 的通用运行记录；本模块不包含任何领域知识。"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """返回带时区的 UTC 时间。"""
    return datetime.now(UTC)


class ExecutionStatus(StrEnum):
    """Run 与 Node 的统一生命周期。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    """通用人工审批结果。"""

    PENDING = "pending"
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_REVISION = "request_revision"


class ArtifactType(StrEnum):
    """Core 能持久化的产物类型。"""

    IMAGE = "image"
    VIDEO = "video"
    PROMPT = "prompt"
    DOCUMENT = "document"
    JSON = "json"
    LISTING = "listing"
    REPORT = "report"


class CoreModel(BaseModel):
    """数据库边界使用的严格模型基类。"""

    model_config = ConfigDict(extra="forbid")


class RunRecord(CoreModel):
    """一次 Workflow 运行的持久化快照。"""

    id: str
    domain: str
    workflow: str
    status: ExecutionStatus
    state: dict[str, Any]
    current_node: str
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    cost_fen: int = Field(default=0, ge=0)


class NodeExecutionRecord(CoreModel):
    """一个 Node 的最新执行状态与输出。"""

    run_id: str
    node_id: str
    status: ExecutionStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    retry_count: int = Field(default=0, ge=0)
    error: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)


class ArtifactRecord(CoreModel):
    """跨领域统一产物。"""

    id: str
    type: ArtifactType
    run_id: str
    node_id: str
    source: str
    status: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    location: str | None = None
    version: int = Field(default=1, gt=0)


class ApprovalRecord(CoreModel):
    """任意需要人工确认的 Node 审批记录。"""

    id: str
    run_id: str
    node_id: str
    decision: ApprovalDecision
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    decided_at: datetime | None = None


class CheckpointRecord(CoreModel):
    """每个节点边界保存的可恢复状态。"""

    id: int
    run_id: str
    node_id: str
    next_node: str
    status: ExecutionStatus
    state: dict[str, Any]
    created_at: datetime


class SkillExecutionRecord(CoreModel):
    """Skill 的审计记录。"""

    id: str
    run_id: str
    node_id: str
    skill_id: str
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
