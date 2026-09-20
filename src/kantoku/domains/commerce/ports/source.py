"""商品来源端口；Core 不依赖该接口。"""

from __future__ import annotations

from typing import Protocol

from ..models import Candidate


class SourcePort(Protocol):
    """未来 1688 等来源 Adapter 的稳定边界。"""

    def fetch(self, query: str) -> list[Candidate]: ...
