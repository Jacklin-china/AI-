"""Commerce Listing 本地化 Skill。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from kantoku.config import ToolError


def execute(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Mapping[str, Any]:
    """zh-CN 原样返回；ru-RU 使用注入的翻译 Tool。"""
    locale = str(inputs["locale"])
    listing = inputs["listing"]
    if not isinstance(listing, dict):
        raise ToolError("listing 必须是对象")
    if locale == "zh-CN":
        localized = dict(listing)
    elif locale == "ru-RU":
        translator = context.get("translator")
        if not callable(translator):
            raise ToolError("ru-RU 本地化需要 llm_translation Tool")
        localized = cast(Callable[[dict[str, Any], str], dict[str, Any]], translator)(
            dict(listing), locale
        )
    else:
        raise ToolError("暂不支持该 locale", detail=locale)
    return {"localized_listing": localized, "locale": locale}
