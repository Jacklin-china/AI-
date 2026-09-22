"""Commerce Product Image Skill 与通用 Image Capability 的稳定边界。"""

from __future__ import annotations

from typing import Protocol

from .models import ProductImageBrief, ProductImageResult


class ProductImageCapability(Protocol):
    """领域只依赖该能力，不直接依赖任何供应商 SDK。"""

    def generate(
        self,
        brief: ProductImageBrief,
        prompt: str,
        *,
        request_id: str,
    ) -> ProductImageResult: ...
