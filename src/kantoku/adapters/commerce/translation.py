"""Commerce 本地化的显式 Mock LLM Tool。"""

from __future__ import annotations

from typing import Any


class MockTranslationAdapter:
    """不调用外部模型的测试翻译器。"""

    def __call__(self, listing: dict[str, Any], locale: str) -> dict[str, Any]:
        translated = dict(listing)
        translated.update({
            "title": f"[MOCK ru-RU] {listing.get('title', '')}",
            "description": f"[MOCK translation] {listing.get('description', '')}",
            "locale": locale,
            "mock": True,
        })
        return translated
