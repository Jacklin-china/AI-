"""Marketplace 端口；Core 不理解平台草稿。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ..models import MarketplaceDraft


class MarketplacePort(Protocol):
    """未来 Ozon 等平台 Adapter 的稳定边界。"""

    def create_draft(self, listing: Mapping[str, Any]) -> MarketplaceDraft: ...

    def submit_draft(self, draft_id: str) -> MarketplaceDraft: ...
