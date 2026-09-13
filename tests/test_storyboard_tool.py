"""分镜生成工具及 SQLite 落库测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kantoku.config import SchemaError, ToolError
from kantoku.schemas.storyboard import Shot, Storyboard
from kantoku.tools import storyboard as storyboard_tool


def _board(*, episode: str = "雨夜重逢", desc_prefix: str = "镜头") -> Storyboard:
    return Storyboard(
        episode=episode,
        shots=[
            Shot(
                shot_no=number,
                desc=f"{desc_prefix}{number}",
                dialogue="",
                camera="中景",
                duration_s=3,
                characters=["阿青"],
            )
            for number in range(1, 21)
        ],
    )


@pytest.fixture
def isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    database_path = tmp_path / "nested" / "kantoku.db"
    settings = SimpleNamespace(storage=SimpleNamespace(sqlite_path=database_path))
    monkeypatch.setattr(storyboard_tool, "get_settings", lambda: settings)
    return database_path


def test_generate_storyboard_uses_structured_call_and_persists(
    monkeypatch: pytest.MonkeyPatch,
    isolated_database: Path,
) -> None:
    board = _board()
    settings = SimpleNamespace(
        storage=SimpleNamespace(sqlite_path=isolated_database),
        llm=SimpleNamespace(structured_temperature=0.2),
    )
    monkeypatch.setattr(storyboard_tool, "get_settings", lambda: settings)
    chat_mock = MagicMock(return_value=SimpleNamespace(content=board.model_dump_json()))
    monkeypatch.setattr(storyboard_tool, "chat", chat_mock)

    result = storyboard_tool.generate_storyboard("  一个雨夜重逢的故事  ")

    assert result == board
    assert storyboard_tool.load_storyboard("雨夜重逢") == board
    request = chat_mock.call_args
    assert request.kwargs["temperature"] == 0.2
    assert request.kwargs["response_format"] == {"type": "json_object"}
    assert request.args[0][1] == {"role": "user", "content": "一个雨夜重逢的故事"}
    assert "动物、AI 等非人角色也算" in request.args[0][0]["content"]


def test_save_storyboard_replaces_episode_as_one_version(isolated_database: Path) -> None:
    storyboard_tool.save_storyboard(_board(desc_prefix="旧"))
    storyboard_tool.save_storyboard(_board(desc_prefix="新"))

    loaded = storyboard_tool.load_storyboard("雨夜重逢")

    assert loaded is not None
    assert len(loaded.shots) == 20
    assert loaded.shots[0].desc == "新1"
    with sqlite3.connect(isolated_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM shot").fetchone()[0] == 20


def test_load_storyboard_returns_none_for_unknown_episode(isolated_database: Path) -> None:
    assert storyboard_tool.load_storyboard("不存在") is None


def test_generate_storyboard_rejects_empty_or_wrong_count(
    monkeypatch: pytest.MonkeyPatch,
    isolated_database: Path,
) -> None:
    with pytest.raises(ToolError, match="剧本不能为空"):
        storyboard_tool.generate_storyboard("  ")

    short_board = Storyboard(episode="短", shots=_board().shots[:1])
    monkeypatch.setattr(
        storyboard_tool,
        "chat",
        MagicMock(return_value=SimpleNamespace(content=short_board.model_dump_json())),
    )
    settings = SimpleNamespace(
        storage=SimpleNamespace(sqlite_path=isolated_database),
        llm=SimpleNamespace(structured_temperature=0.2),
    )
    monkeypatch.setattr(storyboard_tool, "get_settings", lambda: settings)

    with pytest.raises(SchemaError, match="分镜数量不符合要求") as caught:
        storyboard_tool.generate_storyboard("有效剧本")

    assert caught.value.raw == short_board.model_dump_json()
    assert not isolated_database.exists()


def test_load_storyboard_wraps_corrupt_character_json(isolated_database: Path) -> None:
    storyboard_tool.save_storyboard(_board())
    with sqlite3.connect(isolated_database) as connection:
        connection.execute("UPDATE shot SET characters_json = ? WHERE shot_no = 1", ("not-json",))
        connection.commit()

    with pytest.raises(ToolError, match="分镜数据库内容无效"):
        storyboard_tool.load_storyboard("雨夜重逢")


def test_schema_file_has_required_columns() -> None:
    schema = storyboard_tool.SCHEMA_PATH.read_text(encoding="utf-8")
    for column in (
        "episode",
        "shot_no",
        "desc",
        "dialogue",
        "camera",
        "duration_s",
        "characters_json",
    ):
        assert column in schema
