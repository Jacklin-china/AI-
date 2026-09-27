"""Phase 3 导演决策：上下文边界、硬约束与不可变版本。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kantoku.config import ToolError
from kantoku.domains.comic.director import plan_director_spec
from kantoku.domains.comic.models import (
    ComicProjectInput,
    CreativeBriefUpdate,
    DirectorSpecDraft,
)
from kantoku.domains.comic.projects import ComicProjectStore


def _project(store: ComicProjectStore) -> str:
    return store.create(ComicProjectInput.model_validate({
        "title": "东方仙侠少女雨夜战斗场景",
        "brief": {
            "original_request": "制作一个东方仙侠少女雨夜战斗场景",
            "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
            "soft_preferences": ["电影感", "冷色调"],
            "creative_freedom": ["镜头设计", "环境细节"],
        },
    })).project.project_id


def _draft(*, lighting: str = "雨幕冷色环境光中保留人物轮廓") -> DirectorSpecDraft:
    return DirectorSpecDraft.model_validate({
        "visual_direction": "以人物在雨夜中的抉择为视觉中心",
        "storytelling_goal": "让观众感到她独自迎战的决心",
        "camera_language": "先建立人物与环境的距离，再接近面部观察情绪",
        "composition": "让环境空间参与叙事，人物处于清晰视觉焦点",
        "lighting": lighting,
        "color_language": "冷色雨景与少量暖色人物轮廓形成层次",
        "emotion": "孤独但坚定",
        "character_focus": "保持少女的东方仙侠身份和动作状态",
        "constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
        "creative_choices": ["环境先于人物出现，交代战斗的处境和人物的孤立。"],
    })


def test_director_uses_only_selected_context_and_keeps_hard_constraints(tmp_path: Path) -> None:
    store = ComicProjectStore(tmp_path / "comic.db")
    project_id = _project(store)
    seen: list[list[dict[str, str]]] = []

    def model_call(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return _draft().model_copy(update={"constraints": ["雨夜"]}).model_dump_json()

    draft = plan_director_spec(
        store.get(project_id), task="设计这场战斗的视觉表达", model_call=model_call,
    )
    assert draft.constraints == ["东方仙侠", "少女", "雨夜", "战斗"]
    assert draft.storytelling_goal != draft.visual_direction
    context = json.loads(seen[0][1]["content"])
    assert context["stable_context"]["project"]["project_id"] == project_id
    assert context["current_task"] == "设计这场战斗的视觉表达"
    assert context["relevant_memory"] == []
    assert "chat_history" not in context
    assert "低机位 + 暗色" not in seen[0][0]["content"]


def test_director_versions_are_editable_restorable_and_brief_bound(tmp_path: Path) -> None:
    store = ComicProjectStore(tmp_path / "comic.db")
    project_id = _project(store)
    first = store.save_director(project_id, _draft(), expected_project_version=1, source="model")
    assert first.version == 1
    assert first.creative_brief_version == 1
    second = store.save_director(
        project_id, _draft(lighting="人物背后散射的天光"), expected_project_version=2,
    )
    assert second.version == 2
    assert second.spec_id == first.spec_id
    assert store.get_director(project_id) == second
    assert store.get(project_id, version=2).project.director_version == 1
    assert [item.version for item in store.director_versions(project_id)] == [2, 1]

    restored = store.restore_director(project_id, version=1, expected_project_version=3)
    assert restored.version == 3
    assert restored.restored_from_version == 1
    assert restored.source == "restored"
    assert restored.lighting == first.lighting
    assert store.get_director(project_id) == restored

    updated = store.replace_brief(project_id, CreativeBriefUpdate.model_validate({
        "expected_version": 4,
        "original_request": "少女雨夜战斗，旁边加入白鹤",
        "hard_constraints": ["少女", "雨夜", "白鹤"],
    }))
    assert updated.project.director_version is None
    with pytest.raises(ToolError, match="尚无导演方案"):
        store.get_director(project_id)
    with pytest.raises(ToolError, match="旧版创作理解"):
        store.restore_director(project_id, version=1, expected_project_version=5)
    assert len(store.director_versions(project_id)) == 3
    latest = store.save_director(
        project_id, _draft().model_copy(update={"constraints": ["少女", "雨夜", "白鹤"]}),
        expected_project_version=5,
    )
    assert latest.version == 4
    assert latest.creative_brief_version == 2
    assert latest.spec_id == first.spec_id
    assert ComicProjectStore(store.path).get_director(project_id) == latest


def test_director_rejects_stale_project_missing_constraints_and_invalid_model(
    tmp_path: Path,
) -> None:
    store = ComicProjectStore(tmp_path / "comic.db")
    project_id = _project(store)
    with pytest.raises(ToolError, match="硬约束"):
        store.save_director(
            project_id, _draft().model_copy(update={"constraints": ["雨夜"]}),
            expected_project_version=1,
        )
    with pytest.raises(ToolError, match="结构化方案"):
        plan_director_spec(store.get(project_id), task=None, model_call=lambda _: "bad")
    assert store.director_versions(project_id) == []
    store.save_director(project_id, _draft(), expected_project_version=1)
    with pytest.raises(ToolError, match="刷新"):
        store.save_director(project_id, _draft(), expected_project_version=1)


def test_migrates_phase2_database_without_changing_legacy_rows(tmp_path: Path) -> None:
    path = tmp_path / "phase2.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE comic_schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO comic_schema_migrations VALUES (1,'2026-01-01')")
        connection.execute(
            "CREATE TABLE comic_projects (project_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "description TEXT NOT NULL, status TEXT NOT NULL, current_version INTEGER NOT NULL, "
            "brief_id TEXT NOT NULL, brief_version INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE comic_entity_versions (project_id TEXT NOT NULL, entity_type TEXT "
            "NOT NULL, entity_id TEXT NOT NULL, version INTEGER NOT NULL, payload_json TEXT "
            "NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute("CREATE TABLE shot (episode TEXT, shot_no INTEGER)")
        connection.execute("INSERT INTO shot VALUES ('旧作品',1)")
    store = ComicProjectStore(path)
    project_id = _project(store)
    assert store.get(project_id).project.director_id is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM shot").fetchall() == [("旧作品", 1)]
        assert connection.execute(
            "SELECT version FROM comic_schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (3,)]
