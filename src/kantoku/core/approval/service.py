"""通用人工审批应用服务。"""

from __future__ import annotations

from typing import Any

from kantoku.core.runtime.graph import GraphRuntime
from kantoku.core.runtime.models import ApprovalDecision, ApprovalRecord, RunRecord
from kantoku.core.runtime.store import RuntimeStore


class ApprovalService:
    """将 API 决策映射为持久化审批。"""

    def __init__(self, store: RuntimeStore, runtime: GraphRuntime | None = None) -> None:
        self.store = store
        self.runtime = runtime

    def decide(
        self, approval_id: str, decision: ApprovalDecision,
        response: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """提交 approve / reject / request_revision。"""
        return self.store.decide_approval(approval_id, decision, response)

    def pending(self) -> list[ApprovalRecord]:
        """读取待处理审批。"""
        return self.store.list_approvals(pending_only=True)

    def decide_and_resume(
        self, approval_id: str, decision: ApprovalDecision,
        response: dict[str, Any] | None = None,
    ) -> RunRecord:
        """保存决定并立即恢复所属 Run，避免前端分两次调用。"""
        if self.runtime is None:
            raise RuntimeError("ApprovalService requires a GraphRuntime")
        approval = self.decide(approval_id, decision, response)
        return self.runtime.resume(approval.run_id)
