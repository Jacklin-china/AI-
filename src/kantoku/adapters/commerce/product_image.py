"""Commerce 商品图到 Core Image Capability 的适配器。"""

from __future__ import annotations

from kantoku.config import BudgetError, ConfigError
from kantoku.core.budget import estimate_image_fen
from kantoku.domains.commerce.models import ProductImageBrief, ProductImageResult
from kantoku.tools.image_gen import ImageProvider, gen_image


class MockProductImageCapability:
    """开发模式能力；不生成文件，也不把占位符冒充成真实图片。"""

    def generate(
        self,
        brief: ProductImageBrief,
        prompt: str,
        *,
        request_id: str,
    ) -> ProductImageResult:
        del request_id
        return ProductImageResult(
            status="mock",
            origin="mock",
            provider="mock-image-capability",
            model="mock-product-image-v1",
            prompt=prompt,
            brief=brief,
            message="Mock 商品主图 / 未生成真实图片",
        )


class CoreProductImageCapability:
    """通过统一预算与 ImageProvider 边界执行真实商品图生成。"""

    def __init__(self, provider: ImageProvider, *, max_fen: int) -> None:
        self.provider = provider
        self.max_fen = max_fen

    def generate(
        self,
        brief: ProductImageBrief,
        prompt: str,
        *,
        request_id: str,
    ) -> ProductImageResult:
        estimated_fen = estimate_image_fen()
        identity = self.provider.generation_identity()
        provider_name = str(identity.get("provider", "configured-image-provider"))
        if estimated_fen > self.max_fen:
            return ProductImageResult(
                status="blocked",
                origin="blocked",
                provider=provider_name,
                model=self.provider.model_id,
                prompt=prompt,
                brief=brief,
                message=(
                    "BLOCKED_BY_BUDGET: "
                    f"预估 ¥{estimated_fen / 100:.2f}，本次验收上限 ¥{self.max_fen / 100:.2f}"
                ),
            )
        try:
            result = gen_image(
                prompt,
                brief.revision + 1,
                project="kantoku-commerce",
                episode=request_id,
                client_request_id=request_id,
                provider=self.provider,
                est_fen=estimated_fen,
            )
        except (ConfigError, BudgetError) as error:
            return ProductImageResult(
                status="blocked",
                origin="blocked",
                provider=provider_name,
                model=self.provider.model_id,
                prompt=prompt,
                brief=brief,
                message=f"BLOCKED_BY_CREDENTIALS_OR_BUDGET: {error}",
            )
        origin = "real" if result.status == "succeeded" else "blocked"
        return ProductImageResult(
            status=result.status,
            origin=origin,
            location=str(result.path) if result.path is not None else None,
            provider=provider_name,
            model=self.provider.model_id,
            prompt=prompt,
            brief=brief,
            actual_fen=result.actual_fen,
            message=result.error,
        )
