"""按当前镜头裁剪上下文，隔离无关角色并限制历史窗口。"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from kantoku.config import ToolError
from kantoku.memory import Persona, list_personas
from kantoku.schemas.storyboard import Shot

CONTEXT_VERSION = "context-v1"


def estimate_tokens(text: str) -> int:
    """无供应商 tokenizer 时给出保守近似值，仅用于裁剪前后同口径比较。"""
    if not isinstance(text, str):
        raise ToolError("待估算上下文必须是字符串")
    ascii_count = sum(character.isascii() for character in text)
    non_ascii_count = len(text) - ascii_count
    return math.ceil(ascii_count / 4) + math.ceil(non_ascii_count / 1.5)


def _persona_payload(persona: Persona) -> dict[str, Any]:
    """只输出生图需要的稳定人设字段。"""
    return persona.model_dump(mode="json")


def _continuity_payload(shot: Shot) -> dict[str, Any]:
    """历史镜头只保留连续性线索，不重复携带完整业务对象。"""
    return {
        "shot_no": shot.shot_no,
        "desc": shot.desc,
        "camera": shot.camera,
        "characters": shot.characters,
    }


def select_recent_history(shot: Shot, history: Sequence[Shot], n: int = 3) -> list[Shot]:
    """只选镜号更小的最近 n 镜，统一所有 Prompt 的历史窗口口径。"""
    if type(n) is not int or n < 0:
        raise ToolError("历史窗口 n 必须是非负整数")
    if any(not isinstance(item, Shot) for item in history):
        raise ToolError("历史记录必须全部是 Shot")

    previous = sorted(
        (item for item in history if item.shot_no < shot.shot_no),
        key=lambda item: item.shot_no,
    )
    return previous[-n:] if n else []


def build_context(shot: Shot, history: Sequence[Shot], n: int = 3) -> str:
    """构建当前镜上下文；只含相关人设和最近 n 个更早镜头。"""
    recent = select_recent_history(shot, history, n)

    personas = list_personas(shot.characters)
    found_names = {persona.name for persona in personas}
    missing_names = [name for name in dict.fromkeys(shot.characters) if name not in found_names]
    if missing_names:
        raise ToolError("缺少当前镜头的角色卡", detail="、".join(missing_names))

    payload = {
        "context_version": CONTEXT_VERSION,
        "current_shot": shot.model_dump(mode="json"),
        "personas": [_persona_payload(persona) for persona in personas],
        "recent_continuity": [_continuity_payload(item) for item in recent],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
