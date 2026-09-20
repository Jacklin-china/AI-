"""显式 Mock 的 Marketplace Adapter。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from kantoku.config import ToolError
from kantoku.domains.commerce.models import MarketplaceDraft


class MockMarketplaceAdapter:
    """用内存草稿验证 MarketplacePort，不访问 Ozon。"""

    def __init__(self) -> None:
        self._drafts: dict[str, MarketplaceDraft] = {}

    def create_draft(self, listing: Mapping[str, Any]) -> MarketplaceDraft:
        """创建显式 Mock 草稿。"""
        digest = hashlib.sha256(repr(sorted(listing.items())).encode("utf-8")).hexdigest()[:12]
        draft = MarketplaceDraft(
            id=f"mock-draft-{digest}", listing=dict(listing), status="draft", mock=True
        )
        self._drafts[draft.id] = draft
        return draft

    def submit_draft(self, draft_id: str) -> MarketplaceDraft:
        """提交 Mock 草稿；仅改变本地状态。"""
        try:
            draft = self._drafts[draft_id]
        except KeyError:
            raise ToolError("找不到 Mock Marketplace Draft", detail=draft_id) from None
        submitted = draft.model_copy(update={"status": "submitted"})
        self._drafts[draft_id] = submitted
        return submitted
