"""Commerce 结构化 Listing 与本地化的 Core LLM 适配器。"""

from __future__ import annotations

import json
from typing import Any

from kantoku.config import LLMError
from kantoku.core.llm import chat
from kantoku.domains.commerce.models import ListingDraft, LocalizedListing


def _content_json(content: str | None) -> dict[str, Any]:
    if not content:
        raise LLMError("Commerce LLM 未返回结构化内容")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise LLMError("Commerce LLM 返回的 JSON 无效") from error
    if not isinstance(value, dict):
        raise LLMError("Commerce LLM 返回内容不是对象")
    return value


class CommerceLlmAdapter:
    """复用 Core 唯一 LLM 出口，Workflow 节点自身不做额外重试。"""

    def draft(self, facts: dict[str, Any], revision_instruction: str | None) -> dict[str, Any]:
        message = chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Create a concise marketplace listing as JSON. Preserve SKU, price, "
                        "currency and supplied product facts exactly. Never invent certifications."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"facts": facts, "revision_instruction": revision_instruction},
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        result = ListingDraft.model_validate(_content_json(message.content))
        if result.sku != facts["sku"] or result.price_fen != facts["price_fen"]:
            raise LLMError("Commerce LLM 改写了受保护的 SKU 或价格事实")
        return result.model_copy(update={"mock": False}).model_dump(mode="json")

    def __call__(self, listing: dict[str, Any], locale: str) -> dict[str, Any]:
        message = chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Localize only title and description for the target locale. Return JSON; "
                        "preserve SKU, numeric price, currency, revision and product facts exactly."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"locale": locale, "listing": listing}, ensure_ascii=False
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        result = LocalizedListing.model_validate(_content_json(message.content))
        if result.sku != listing["sku"] or result.price_fen != listing["price_fen"]:
            raise LLMError("Commerce 本地化改写了受保护的 SKU 或价格事实")
        return result.model_copy(update={"locale": locale, "mock": False}).model_dump(mode="json")
