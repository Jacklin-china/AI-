"""显式 Mock 的商品来源 Adapter。"""

from __future__ import annotations

import hashlib

from kantoku.domains.commerce.models import Candidate


class MockSourceAdapter:
    """用确定性数据验证 SourcePort，不访问 1688。"""

    def fetch(self, query: str) -> list[Candidate]:
        """返回三个明确带 mock=true 的候选。"""
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        return [Candidate(
            id=f"mock-candidate-{digest[index:index + 8]}",
            title=f"Mock {query} {index + 1}",
            cost_fen=1200 + index * 150,
            skus=[f"MOCK-{index + 1}-A", f"MOCK-{index + 1}-B"],
            source="mock-source",
            mock=True,
        ) for index in range(3)]
