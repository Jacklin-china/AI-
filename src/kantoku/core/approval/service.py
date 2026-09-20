"""通用人工审批应用服务。"""

from __future__ import annotations

from typing import Any

from kantoku.core.runtime.models import ApprovalDecision, ApprovalRecord
from kantoku.core.runtime.store import RuntimeStore


class ApprovalService:
    """将 API 决策映射为持久化审批。"""

    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def decide(
        self, approval_id: str, decision: ApprovalDecision,
        response: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """提交 approve / reject / request_revision。"""
        return self.store.decide_approval(approval_id, decision, response)

    def pending(self) -> list[ApprovalRecord]:
        """读取待处理审批。"""
        return self.store.list_approvals(pending_only=True)
