"""上下文裁剪的相关角色、历史窗口和成本近似测试。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kantoku.agent import context
from kantoku.config import ToolError
from kantoku.memory import Persona
from kantoku.schemas.storyboard import Shot


def _shot(number: int, *, characters: list[str] | None = None) -> Shot:
    return Shot(
        shot_no=number,
        desc=f"便利店镜头 {number}",
        dialogue="欢迎光临",
        camera="中景",
        duration_s=4,
        characters=characters or [],
    )


def _persona(name: str) -> Persona:
    return Persona(
        name=name,
        appearance=f"{name}的稳定外貌",
        outfit=f"{name}的固定服装",
        style_tokens=["日系动画", "电影光"],
    )


def test_build_context_keeps_only_current_personas_and_recent_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = MagicMock(return_value=[_persona("小雨")])
    monkeypatch.setattr(context, "list_personas", loader)
    history = [_shot(6), _shot(2), _shot(4), _shot(8), _shot(5)]

    raw = context.build_context(_shot(7, characters=["小雨"]), history, n=2)
    payload = json.loads(raw)

    loader.assert_called_once_with(["小雨"])
    assert [item["name"] for item in payload["personas"]] == ["小雨"]
    assert [item["shot_no"] for item in payload["recent_continuity"]] == [5, 6]
    assert payload["current_shot"]["shot_no"] == 7
    assert payload["context_version"] == "context-v1"


def test_build_context_rejects_missing_current_persona(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context, "list_personas", MagicMock(return_value=[_persona("小雨")]))

    with pytest.raises(ToolError, match="缺少当前镜头的角色卡") as caught:
        context.build_context(_shot(3, characters=["小雨", "老陈"]), [])

    assert caught.value.detail == "老陈"


def test_empty_scene_needs_no_persona_and_zero_history_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = MagicMock(return_value=[])
    monkeypatch.setattr(context, "list_personas", loader)

    payload = json.loads(context.build_context(_shot(2), [_shot(1)], n=0))

    loader.assert_called_once_with([])
    assert payload["personas"] == []
    assert payload["recent_continuity"] == []


@pytest.mark.parametrize("n", [-1, 1.5, True])
def test_build_context_rejects_invalid_window(n: object) -> None:
    with pytest.raises(ToolError, match="历史窗口 n 必须是非负整数"):
        context.build_context(_shot(2), [], n=n)  # type: ignore[arg-type]


def test_build_context_rejects_non_shot_history() -> None:
    with pytest.raises(ToolError, match="历史记录必须全部是 Shot"):
        context.build_context(_shot(2), ["not-shot"])  # type: ignore[list-item]


def test_estimate_tokens_is_deterministic_and_rejects_non_text() -> None:
    assert context.estimate_tokens("abcd角色") == 3
    assert context.estimate_tokens("") == 0
    with pytest.raises(ToolError, match="待估算上下文必须是字符串"):
        context.estimate_tokens(1)  # type: ignore[arg-type]
