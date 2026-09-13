"""角色卡契约与 SQLite 持久化测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from kantoku.config import SchemaError, ToolError
from kantoku.memory import Persona, list_personas, load_persona, parse_persona, save_persona
from kantoku.memory import persona as persona_module


def _persona(*, name: str = "小雨", outfit: str = "红色围裙") -> Persona:
    return Persona(
        name=name,
        appearance="17岁，黑色齐耳短发，圆脸，眼神困倦",
        outfit=outfit,
        style_tokens=["日系动画", "柔和电影光"],
    )


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "nested" / "persona.db"
    settings = SimpleNamespace(storage=SimpleNamespace(sqlite_path=path))
    monkeypatch.setattr(persona_module, "get_settings", lambda: settings)
    return path


def test_persona_is_strict_and_strips_text() -> None:
    persona = Persona(
        name="  小雨  ",
        appearance="  黑色短发  ",
        outfit="  红色围裙  ",
        style_tokens=["  日系动画  "],
    )

    assert persona.name == "小雨"
    assert persona.style_tokens == ["日系动画"]
    with pytest.raises(ValidationError):
        Persona.model_validate(
            {
                "name": "小雨",
                "appearance": "黑色短发",
                "outfit": "红色围裙",
                "style_tokens": [],
                "unexpected": True,
            }
        )


def test_save_load_and_upsert_persona(isolated_database: Path) -> None:
    save_persona(_persona(outfit="旧围裙"))
    save_persona(_persona(outfit="新围裙"))

    loaded = load_persona("  小雨 ")

    assert loaded == _persona(outfit="新围裙")
    with sqlite3.connect(isolated_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM persona").fetchone()[0] == 1


def test_list_personas_preserves_requested_order_and_skips_missing() -> None:
    save_persona(_persona(name="小雨"))
    save_persona(_persona(name="老陈", outfit="灰色夹克"))

    selected = list_personas(["老陈", "不存在", "小雨", "老陈"])

    assert [persona.name for persona in selected] == ["老陈", "小雨"]
    assert [persona.name for persona in list_personas()] == ["小雨", "老陈"]


def test_unknown_and_empty_persona_queries() -> None:
    assert load_persona("不存在") is None
    assert list_personas([]) == []
    with pytest.raises(ToolError, match="角色名称不能为空"):
        load_persona("  ")
    with pytest.raises(ToolError, match="角色名称不能为空"):
        list_personas(["小雨", " "])


def test_corrupt_style_tokens_are_rejected(isolated_database: Path) -> None:
    save_persona(_persona())
    with sqlite3.connect(isolated_database) as connection:
        connection.execute(
            "UPDATE persona SET style_tokens_json = ? WHERE name = ?",
            ("not-json", "小雨"),
        )
        connection.commit()

    with pytest.raises(ToolError, match="角色卡数据库内容无效"):
        load_persona("小雨")


def test_parse_persona_hides_invalid_raw_json() -> None:
    raw = '{"private":"不要泄漏"}'

    with pytest.raises(SchemaError, match="角色卡 JSON 校验失败") as caught:
        parse_persona(raw)

    assert caught.value.raw == raw
    assert raw not in str(caught.value)
