"""Phase 4：项目范围资产、Artifact 引用、版本锁定及有限上下文。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kantoku.config import ToolError
from kantoku.core.runtime.models import ArtifactType
from kantoku.core.runtime.store import RuntimeStore
from kantoku.domains.comic.assets import ComicAssetStore
from kantoku.domains.comic.director import plan_director_spec
from kantoku.domains.comic.models import (
    ComicAssetDraft,
    ComicAssetRef,
    ComicProjectInput,
    DirectorSpecDraft,
)
from kantoku.domains.comic.projects import ComicContextBuilder, ComicProjectStore


def _stores(path: Path) -> tuple[ComicProjectStore, ComicAssetStore, RuntimeStore]:
    runtime = RuntimeStore(path)
    projects = ComicProjectStore(path)
    return projects, ComicAssetStore(projects, runtime), runtime


def _project(projects: ComicProjectStore, title: str = "竹林雨夜") -> str:
    return projects.create(ComicProjectInput.model_validate({
        "title": title,
        "brief": {
            "original_request": "阿青在竹林雨夜迎战",
            "hard_constraints": ["阿青", "竹林", "雨夜"],
        },
    })).project.project_id


def _character(*, outfit: str = "蓝色长袍", refs: list[str] | None = None) -> ComicAssetDraft:
    return ComicAssetDraft.model_validate({
        "name": "阿青", "aliases": ["主角阿青"],
        "details": {
            "kind": "character", "appearance": "黑发少年剑士",
            "outfit": outfit, "features": ["左眉有细小疤痕"],
        },
        "fixed_constraints": ["黑发", "少年剑士"],
        "reference_artifact_ids": refs or [],
    })


def _scene() -> ComicAssetDraft:
    return ComicAssetDraft.model_validate({
        "name": "竹林", "details": {
            "kind": "scene", "location": "竹林边缘", "time_of_day": "夜晚",
            "weather": "细雨", "lighting": "林间透入微弱月光",
            "atmosphere": "静谧但危险", "environment_features": ["石径"],
        },
    })


def _style() -> ComicAssetDraft:
    return ComicAssetDraft.model_validate({
        "name": "作品视觉风格", "details": {
            "kind": "style", "art_direction": "东方仙侠电影感",
            "color_language": "冷色雨夜与人物暖色边光",
            "materials": "湿润织物与竹叶", "camera_language": "镜头服务人物抉择",
        },
    })


def _director() -> DirectorSpecDraft:
    return DirectorSpecDraft.model_validate({
        "visual_direction": "让竹林空间凸显人物孤立",
        "storytelling_goal": "展现阿青迎战的决心",
        "camera_language": "先建立空间，再接近人物",
        "composition": "人物与石径形成纵深",
        "lighting": "雨夜月光下保持人物轮廓清晰",
        "color_language": "冷色环境与微暖人物轮廓",
        "emotion": "克制而坚定", "character_focus": "剑士的姿态",
        "constraints": ["阿青", "竹林", "雨夜"],
        "creative_choices": ["先看空间以建立人物的处境。"],
    })


def test_assets_versions_lock_and_context_select_only_relevant(tmp_path: Path) -> None:
    path = tmp_path / "comic.db"
    projects, assets, runtime = _stores(path)
    project_id = _project(projects)
    conversation = runtime.create_conversation("参考素材")
    reference = runtime.create_artifact(
        type=ArtifactType.IMAGE, node_id="reference", source="test",
        location=str(tmp_path / "reference.png"), conversation_id=conversation.id,
    )
    character = assets.create(
        project_id, _character(refs=[reference.id]), expected_project_version=1,
    )
    scene = assets.create(project_id, _scene(), expected_project_version=2)
    style = assets.create(project_id, _style(), expected_project_version=3)
    unrelated = assets.create(project_id, ComicAssetDraft.model_validate({
        "name": "阿白", "details": {"kind": "character", "appearance": "银发弓手"},
    }), expected_project_version=4)
    director = projects.save_director(project_id, _director(), expected_project_version=5)
    snapshot = projects.get(project_id)
    selected = assets.select_relevant(project_id, task="阿青穿过竹林迎战")
    assert {item.asset_id for item in selected} == {
        character.asset_id, scene.asset_id, style.asset_id,
    }
    assert unrelated.asset_id not in {item.asset_id for item in selected}
    context = ComicContextBuilder.build(
        snapshot, task="阿青穿过竹林迎战", director=director, assets=selected,
    )
    assert context.stable_context["director_spec"]["storytelling_goal"] == "展现阿青迎战的决心"
    assert context.source_versions[f"asset:{character.asset_id}"] == 1
    assert len(context.relevant_memory) == 3
    assert context.relevant_memory[0]["reference_artifact_ids"] == [reference.id]
    model_messages: list[list[dict[str, str]]] = []

    def model_call(messages: list[dict[str, str]]) -> str:
        model_messages.append(messages)
        return _director().model_dump_json()

    plan_director_spec(
        snapshot, task="阿青穿过竹林迎战", assets=selected, model_call=model_call,
    )
    sent_context = json.loads(model_messages[0][1]["content"])
    assert len(sent_context["relevant_memory"]) == 3
    assert unrelated.asset_id not in model_messages[0][1]["content"]

    edited = assets.change(
        project_id, character.asset_id, expected_project_version=6,
        expected_asset_version=1, action="edit",
        draft=_character(outfit="增加深色披风", refs=[reference.id]),
    )
    assert edited.version == 2
    locked = assets.change(
        project_id, character.asset_id, expected_project_version=7,
        expected_asset_version=2, action="lock", target_version=1,
    )
    assert locked.pinned_version == 1
    next_edit = assets.change(
        project_id, character.asset_id, expected_project_version=8,
        expected_asset_version=3, action="edit", draft=_character(outfit="银白披风"),
    )
    assert next_edit.version == 4
    pinned = assets.select_relevant(project_id, task="阿青行动")
    assert pinned[0].version == 1
    assert pinned[0].details.outfit == "蓝色长袍"
    assert assets.get(project_id, character.asset_id).details.outfit == "银白披风"
    assert [item.version for item in assets.versions(project_id, character.asset_id)] == [
        4, 3, 2, 1,
    ]
    assert [item.asset_id for item in assets.list(project_id, project_version=2)] == [
        character.asset_id,
    ]
    assert ComicAssetStore(ComicProjectStore(path), RuntimeStore(path)).get(
        project_id, character.asset_id,
    ).version == 4


def test_cross_project_scope_reference_validation_and_no_unrelated_recall(tmp_path: Path) -> None:
    projects, assets, runtime = _stores(tmp_path / "comic.db")
    first_project = _project(projects)
    second_project = _project(projects, "另一个同名角色作品")
    first = assets.create(first_project, _character(), expected_project_version=1)
    second = assets.create(second_project, _character(), expected_project_version=1)
    assert first.asset_id != second.asset_id
    with pytest.raises(ToolError, match="找不到指定作品资产"):
        assets.get(second_project, first.asset_id)
    with pytest.raises(ToolError, match="不属于当前作品"):
        assets.select_relevant(second_project, refs=[ComicAssetRef(asset_id=first.asset_id)])
    assert assets.select_relevant(second_project, task="不相关的战斗") == []

    conversation = runtime.create_conversation("参考素材")
    document = runtime.create_artifact(
        type=ArtifactType.DOCUMENT, node_id="reference", source="test",
        conversation_id=conversation.id,
    )
    with pytest.raises(ToolError, match="图片 Artifact"):
        assets.create(
            second_project, _character(refs=[document.id]), expected_project_version=2,
        )
    empty_image = runtime.create_artifact(
        type=ArtifactType.IMAGE, node_id="reference", source="test",
        conversation_id=conversation.id,
    )
    with pytest.raises(ToolError, match="图片 Artifact"):
        assets.create(
            second_project, _character(refs=[empty_image.id]),
            expected_project_version=2,
        )
    with pytest.raises(ToolError, match="找不到指定 Artifact"):
        assets.create(
            second_project, _character(refs=["artifact-missing"]),
            expected_project_version=2,
        )
    assert assets.select_relevant(
        second_project, refs=[ComicAssetRef(asset_id=second.asset_id)],
    )[0].asset_id == second.asset_id
    with pytest.raises(ToolError, match="重复资产"):
        assets.select_relevant(second_project, refs=[
            ComicAssetRef(asset_id=second.asset_id), ComicAssetRef(asset_id=second.asset_id),
        ])


def test_asset_tombstone_restore_and_conflict_preserve_history(tmp_path: Path) -> None:
    projects, assets, _ = _stores(tmp_path / "comic.db")
    project_id = _project(projects)
    first = assets.create(project_id, _scene(), expected_project_version=1)
    with pytest.raises(ToolError, match="刷新"):
        assets.change(
            project_id, first.asset_id, expected_project_version=1,
            expected_asset_version=1, action="delete",
        )
    deleted = assets.change(
        project_id, first.asset_id, expected_project_version=2,
        expected_asset_version=1, action="delete",
    )
    assert deleted.state == "deleted"
    assert assets.list(project_id) == []
    assert assets.list(project_id, include_deleted=True)[0].state == "deleted"
    assert assets.list(project_id, project_version=2)[0].version == 1
    with pytest.raises(ToolError, match="已删除"):
        assets.select_relevant(project_id, refs=[ComicAssetRef(asset_id=first.asset_id)])
    restored = assets.change(
        project_id, first.asset_id, expected_project_version=3,
        expected_asset_version=2, action="restore", target_version=1,
    )
    assert restored.version == 3
    assert restored.restored_from_version == 1
    assert restored.state == "active"
    assert [item.source for item in assets.versions(project_id, first.asset_id)] == [
        "restored", "deleted", "created",
    ]
    with pytest.raises(ValueError, match="至少需要"):
        ComicAssetDraft.model_validate({
            "name": "空资产", "details": {"kind": "style"},
        })
