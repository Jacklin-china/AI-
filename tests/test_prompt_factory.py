"""Prompt 拼装与配方版本持久化测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from kantoku.config import ToolError
from kantoku.memory import Persona
from kantoku.schemas.qc import ReworkPlan
from kantoku.schemas.storyboard import Shot
from kantoku.tools import prompt_factory


def _persona(name: str = "小雨") -> Persona:
    return Persona(
        name=name,
        appearance="17岁，黑色齐耳短发，圆脸",
        outfit="红色围裙",
        style_tokens=["日系动画", "柔和电影光"],
    )


def _shot(characters: list[str] | None = None) -> Shot:
    return Shot(
        shot_no=7,
        desc="小雨在便利店货架前抬头看向门口",
        dialogue="欢迎光临",
        camera="中景，轻微推近",
        duration_s=4,
        characters=["小雨"] if characters is None else characters,
    )


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "nested" / "recipes.db"
    settings = SimpleNamespace(storage=SimpleNamespace(sqlite_path=path))
    monkeypatch.setattr(prompt_factory, "get_settings", lambda: settings)
    return path


def test_build_prompt_locks_character_style_and_quality() -> None:
    prompt = prompt_factory.build_prompt(
        _shot(),
        _persona(),
        scene_constraint="深夜便利店，蓝绿色冷光，收银台在画面右侧",
    )

    assert "小雨：外貌=17岁，黑色齐耳短发，圆脸；服装=红色围裙" in prompt
    assert "日系动画, 柔和电影光" in prompt
    assert "自然皮肤、头发和布料材质" in prompt
    assert "无水印" in prompt
    assert "【场景锁定】深夜便利店，蓝绿色冷光，收银台在画面右侧" in prompt


def test_build_prompt_rejects_missing_extra_or_reordered_personas() -> None:
    shot = _shot(["小雨", "老陈"])
    with pytest.raises(ToolError, match="Prompt 人设与镜头人物不一致"):
        prompt_factory.build_prompt(shot, [_persona("小雨")])
    with pytest.raises(ToolError, match="Prompt 人设与镜头人物不一致"):
        prompt_factory.build_prompt(shot, [_persona("老陈"), _persona("小雨")])
    with pytest.raises(ToolError, match="Prompt 人设不允许重复"):
        prompt_factory.build_prompt(_shot(), [_persona(), _persona()])


def test_empty_shot_explicitly_forbids_added_people() -> None:
    prompt = prompt_factory.build_prompt(_shot([]), [])

    assert "本镜为空镜，不添加人物" in prompt
    with pytest.raises(ToolError, match="场景约束不能为空"):
        prompt_factory.build_prompt(_shot([]), [], scene_constraint="  ")


def test_prompt_uses_only_recent_past_continuity() -> None:
    history = [
        _shot().model_copy(update={"shot_no": number, "desc": f"历史镜头 {number}"})
        for number in (2, 5, 6, 8)
    ]

    prompt = prompt_factory.build_prompt(_shot(), _persona(), history=history, history_window=2)

    assert "镜5：历史镜头 5" in prompt
    assert "镜6：历史镜头 6" in prompt
    assert "历史镜头 2" not in prompt
    assert "历史镜头 8" not in prompt


def test_recipe_records_provenance_and_seed_semantics() -> None:
    recipe = prompt_factory.create_recipe(
        episode="雨夜便利店",
        shot=_shot(),
        persona=_persona(),
        model="configured-image-model",
        size="1920x1080",
        reference_asset_ids=["persona-xiaoyu-v1"],
        seed=None,
        scene_constraint="深夜便利店，蓝绿色冷光",
    )

    assert recipe.prompt_version == prompt_factory.PROMPT_VERSION
    assert recipe.reference_asset_ids == ["persona-xiaoyu-v1"]
    assert recipe.reproducible is False
    assert recipe.model == "configured-image-model"
    assert recipe.scene_constraint == "深夜便利店，蓝绿色冷光"


def test_rework_recipe_is_stable_targeted_and_preserves_base_provenance() -> None:
    base = prompt_factory.create_recipe(
        episode="雨夜便利店",
        shot=_shot(),
        persona=_persona(),
        model="configured-image-model",
        size="1920x1080",
        reference_asset_ids=["persona-xiaoyu-v1"],
        seed=42,
    )
    plan = ReworkPlan(
        source_request_id="request-001",
        preserve_constraints=["保留人物身份、场景和已通过细节。"],
        correction_directives=["降低笑容幅度，让情绪克制自然。"],
        evidence="人物笑容僵硬，眼神与剧情情绪不一致",
    )

    first = prompt_factory.create_rework_recipe(
        base, plan, target_request_id="request-001-v2"
    )
    second = prompt_factory.create_rework_recipe(
        base, plan, target_request_id="request-001-v2"
    )

    assert first == second
    assert first.prompt_version.startswith("prompt-v1-rework-")
    assert first.source_request_id == "request-001"
    assert first.target_request_id == "request-001-v2"
    assert first.reference_asset_ids == base.reference_asset_ids
    assert first.seed == 42
    assert base.prompt in first.prompt
    assert "【只修正以下问题】" in first.prompt
    assert plan.evidence in first.prompt

    prompt_factory.save_recipe(first)
    assert (
        prompt_factory.load_recipe(first.episode, first.shot_no, first.prompt_version)
        == first
    )


def test_recipe_rejects_invalid_size_duplicate_references_and_boolean_seed() -> None:
    base = {
        "episode": "雨夜便利店",
        "shot_no": 7,
        "prompt_version": "prompt-v1",
        "prompt": "有效 prompt",
        "scene_constraint": None,
        "model": "configured-image-model",
        "size": "bad-size",
        "reference_asset_ids": [],
        "seed": None,
    }
    with pytest.raises(ValidationError):
        prompt_factory.PromptRecipe.model_validate(base)
    with pytest.raises(ValidationError):
        prompt_factory.PromptRecipe.model_validate(
            {**base, "size": "1920x1080", "reference_asset_ids": ["same", "same"]}
        )
    with pytest.raises(ValidationError):
        prompt_factory.PromptRecipe.model_validate(
            {**base, "size": "1920x1080", "seed": True}
        )
    with pytest.raises(ValidationError, match="同时记录"):
        prompt_factory.PromptRecipe.model_validate(
            {**base, "size": "1920x1080", "source_request_id": "request-001"}
        )


def test_save_load_is_idempotent_but_rejects_same_version_drift(
    isolated_database: Path,
) -> None:
    recipe = prompt_factory.create_recipe(
        episode="雨夜便利店",
        shot=_shot(),
        persona=_persona(),
        model="configured-image-model",
        size="1920x1080",
    )
    prompt_factory.save_recipe(recipe)
    prompt_factory.save_recipe(recipe)

    assert (
        prompt_factory.load_recipe("雨夜便利店", 7, prompt_factory.PROMPT_VERSION) == recipe
    )
    with sqlite3.connect(isolated_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM recipe").fetchone()[0] == 1

    changed = recipe.model_copy(update={"prompt": recipe.prompt + "\n擅自修改"})
    with pytest.raises(ToolError, match="请升级 PROMPT_VERSION"):
        prompt_factory.save_recipe(changed)


def test_list_recipes_returns_one_version_in_shot_order(isolated_database: Path) -> None:
    first = prompt_factory.create_recipe(
        episode="雨夜便利店",
        shot=_shot().model_copy(update={"shot_no": 8}),
        persona=_persona(),
        model="configured-image-model",
        size="1920x1080",
    )
    second = prompt_factory.create_recipe(
        episode="雨夜便利店",
        shot=_shot(),
        persona=_persona(),
        model="configured-image-model",
        size="1920x1080",
    )
    prompt_factory.save_recipe(first)
    prompt_factory.save_recipe(second)

    recipes = prompt_factory.list_recipes("雨夜便利店", prompt_factory.PROMPT_VERSION)

    assert [recipe.shot_no for recipe in recipes] == [7, 8]


def test_corrupt_prompt_fingerprint_is_rejected(isolated_database: Path) -> None:
    recipe = prompt_factory.create_recipe(
        episode="雨夜便利店",
        shot=_shot(),
        persona=_persona(),
        model="configured-image-model",
        size="1920x1080",
    )
    prompt_factory.save_recipe(recipe)
    with sqlite3.connect(isolated_database) as connection:
        connection.execute("UPDATE recipe SET prompt = ?", ("tampered",))
        connection.commit()

    with pytest.raises(ToolError, match="指纹不匹配"):
        prompt_factory.load_recipe("雨夜便利店", 7, prompt_factory.PROMPT_VERSION)


def test_existing_recipe_table_is_migrated_without_deleting_data(
    isolated_database: Path,
) -> None:
    isolated_database.parent.mkdir(parents=True)
    with sqlite3.connect(isolated_database) as connection:
        connection.execute(
            """
            CREATE TABLE recipe (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode TEXT NOT NULL,
                shot_no INTEGER NOT NULL,
                prompt_version TEXT NOT NULL,
                prompt TEXT NOT NULL,
                prompt_sha256 TEXT NOT NULL,
                model TEXT NOT NULL,
                size TEXT NOT NULL,
                reference_asset_ids_json TEXT NOT NULL,
                seed INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (episode, shot_no, prompt_version)
            )
            """
        )

    recipe = prompt_factory.create_recipe(
        episode="雨夜便利店",
        shot=_shot(),
        persona=_persona(),
        model="configured-image-model",
        size="1920x1080",
        scene_constraint="深夜便利店",
    )
    prompt_factory.save_recipe(recipe)

    assert prompt_factory.load_recipe("雨夜便利店", 7, "prompt-v1") == recipe
    with sqlite3.connect(isolated_database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(recipe)").fetchall()
        }
    assert "scene_constraint" in columns
