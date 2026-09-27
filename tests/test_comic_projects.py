"""Comic Phase 2：作品和 Brief 的持久化、版本与上下文边界。"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kantoku.config import ToolError
from kantoku.core.runtime.store import RuntimeStore
from kantoku.domains.comic.models import ComicProjectInput, CreativeBriefUpdate
from kantoku.domains.comic.projects import ComicContextBuilder, ComicProjectStore


def _sample_input() -> ComicProjectInput:
    return ComicProjectInput.model_validate({
        "title": "东方仙侠少女雨夜战斗漫画",
        "description": "一个雨夜战斗的作品",
        "brief": {
            "original_request": "东方仙侠少女雨夜战斗漫画",
            "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
            "soft_preferences": ["电影感", "冷色调"],
            "creative_freedom": ["构图优化", "光影优化", "镜头设计"],
        },
    })


def test_project_brief_versions_survive_restart_and_select_context(tmp_path: Path) -> None:
    path = tmp_path / "kantoku.db"
    RuntimeStore(path)
    store = ComicProjectStore(path)
    first = store.create(_sample_input())
    project_id = first.project.project_id
    assert first.project.current_version == 1
    assert first.creative_brief.hard_constraints == ["东方仙侠", "少女", "雨夜", "战斗"]

    context = ComicContextBuilder.build(first, task="规划当前作品")
    assert context.stable_context["creative_brief"]["soft_preferences"] == ["电影感", "冷色调"]
    assert context.current_task == "规划当前作品"
    assert context.relevant_memory == []
    assert context.source_versions == {"project": 1, "creative_brief": 1}

    updated = store.replace_brief(project_id, CreativeBriefUpdate.model_validate({
        "expected_version": 1,
        "original_request": "东方仙侠少女雨夜战斗漫画，加入一只白鹤",
        "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗", "白鹤"],
        "soft_preferences": ["电影感"],
        "creative_freedom": ["构图优化"],
    }))
    assert updated.project.current_version == 2
    assert updated.creative_brief.version == 2
    restarted = ComicProjectStore(path)
    assert restarted.get(project_id).creative_brief.hard_constraints[-1] == "白鹤"
    assert restarted.get(project_id, version=1) == first
    assert restarted.get(project_id, version=2) == updated
    assert ComicContextBuilder.build(restarted.get(project_id, version=1)).source_versions == {
        "project": 1, "creative_brief": 1,
    }


def test_brief_update_is_atomic_conflict_safe_and_same_put_is_idempotent(tmp_path: Path) -> None:
    store = ComicProjectStore(tmp_path / "comic.db")
    created = store.create(_sample_input())
    project_id = created.project.project_id
    request = CreativeBriefUpdate.model_validate({
        "expected_version": 1,
        "original_request": "新方向",
        "hard_constraints": ["雨夜"],
    })
    changed = store.replace_brief(project_id, request)
    assert store.replace_brief(project_id, request) == changed
    with pytest.raises(ToolError, match="作品已由其他操作更新"):
        store.replace_brief(project_id, CreativeBriefUpdate.model_validate({
            "expected_version": 1, "original_request": "相互冲突的修改",
        }))
    assert store.get(project_id) == changed
    with sqlite3.connect(store.path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM comic_entity_versions WHERE project_id=?",
            (project_id,),
        ).fetchone()[0]
    assert count == 4  # 作品和 Brief 各两个不可变修订。


def test_parallel_brief_updates_cannot_overwrite_each_other(tmp_path: Path) -> None:
    store = ComicProjectStore(tmp_path / "comic.db")
    project_id = store.create(_sample_input()).project.project_id

    def update(original_request: str) -> bool:
        try:
            store.replace_brief(project_id, CreativeBriefUpdate(
                expected_version=1, original_request=original_request,
            ))
            return True
        except ToolError as error:
            assert "作品已由其他操作更新" in str(error)
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(update, ["第一种方案", "第二种方案"]))
    assert sorted(results) == [False, True]
    assert store.get(project_id).project.current_version == 2
    assert store.get(project_id, version=1).creative_brief.original_request == (
        "东方仙侠少女雨夜战斗漫画"
    )


def test_project_scope_and_legacy_rows_are_untouched(tmp_path: Path) -> None:
    path = tmp_path / "existing.db"
    RuntimeStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE shot (episode TEXT NOT NULL, shot_no INTEGER NOT NULL, "
            "desc TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO shot VALUES ('旧作品', 1, '原有镜头')")
    store = ComicProjectStore(path)
    first = store.create(_sample_input())
    second = store.create(_sample_input())  # 同名作品也必须互相隔离。
    assert first.project.project_id != second.project.project_id
    assert store.get(first.project.project_id).creative_brief.project_id != (
        store.get(second.project.project_id).creative_brief.project_id
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM shot").fetchall() == [
            ("旧作品", 1, "原有镜头"),
        ]


def test_brief_validation_does_not_invent_preferences(tmp_path: Path) -> None:
    store = ComicProjectStore(tmp_path / "comic.db")
    created = store.create(ComicProjectInput(title="只给标题的作品"))
    assert created.creative_brief.original_request == "只给标题的作品"
    assert created.creative_brief.hard_constraints == []
    assert created.creative_brief.soft_preferences == []
    assert created.creative_brief.creative_freedom == []
    with pytest.raises(ValueError, match="不允许重复"):
        ComicProjectInput.model_validate({
            "title": "无效作品",
            "brief": {"original_request": "原文", "hard_constraints": ["雨夜", "雨夜"]},
        })
