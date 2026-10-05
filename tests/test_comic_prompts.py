"""Phase 6：镜头 Prompt 的有界编译、版本与共享 Artifact。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kantoku.config import ToolError
from kantoku.core.runtime.models import ArtifactType
from kantoku.core.runtime.store import RuntimeStore
from kantoku.domains.comic.assets import ComicAssetStore
from kantoku.domains.comic.models import (
    ComicAssetDraft,
    ComicProjectInput,
    ComicPromptDraft,
    ComicShotDraft,
    ComicStoryboardDraft,
    DirectorSpecDraft,
    ShotStatus,
)
from kantoku.domains.comic.projects import ComicProjectStore
from kantoku.domains.comic.prompts import ComicPromptStore, compiler_for_model
from kantoku.domains.comic.storyboards import ComicStoryboardStore


def _setup(path: Path):
    runtime = RuntimeStore(path)
    projects = ComicProjectStore(path)
    assets = ComicAssetStore(projects, runtime)
    storyboards = ComicStoryboardStore(projects, assets)
    prompts = ComicPromptStore(projects, storyboards, assets, runtime)
    project = projects.create(ComicProjectInput.model_validate({
        "title": "东方仙侠少女雨夜战斗",
        "brief": {
            "original_request": "东方仙侠少女雨夜拔剑",
            "hard_constraints": ["东方仙侠", "少女", "雨夜"],
        },
    })).project
    character = assets.create(project.project_id, ComicAssetDraft.model_validate({
        "name": "阿青", "details": {
            "kind": "character", "appearance": "黑发少女", "outfit": "青色披风",
        }, "fixed_constraints": ["青色披风"],
    }), expected_project_version=1)
    unrelated = assets.create(project.project_id, ComicAssetDraft.model_validate({
        "name": "阿白", "details": {"kind": "character", "appearance": "银发少年"},
    }), expected_project_version=2)
    scene = assets.create(project.project_id, ComicAssetDraft.model_validate({
        "name": "竹林", "details": {"kind": "scene", "location": "雨夜竹林"},
    }), expected_project_version=3)
    director = projects.save_director(project.project_id, DirectorSpecDraft.model_validate({
        "visual_direction": "用空间展现人物抉择", "storytelling_goal": "表现拔剑前的坚定",
        "camera_language": "镜头贴近人物行动", "composition": "人物与竹林有清晰空间关系",
        "lighting": "雨幕中保留轮廓", "color_language": "冷暖层次",
        "emotion": "坚定", "character_focus": "拔剑姿态",
        "constraints": ["东方仙侠", "少女", "雨夜"],
        "creative_choices": ["让环境先建立危险感"],
    }), expected_project_version=4)
    storyboard = storyboards.create(
        project.project_id, ComicStoryboardDraft(title="雨夜迎战"),
        expected_project_version=5, director_spec_version=director.version,
    )
    shot = storyboards.add_shot(
        storyboard.storyboard_id, ComicShotDraft.model_validate({
            "purpose": "交代拔剑时刻", "subject": "阿青", "action": "拔剑",
            "environment": "雨夜竹林", "character_asset_versions": [
                {"asset_id": character.asset_id, "version": 1},
            ], "scene_asset_versions": [
                {"asset_id": scene.asset_id, "version": 1},
            ],
        }), expected_project_version=6, expected_storyboard_version=1,
    )
    return runtime, projects, assets, storyboards, prompts, shot, unrelated


def _draft(text: str = "东方仙侠 少女 雨夜 青色披风 拔剑") -> ComicPromptDraft:
    return ComicPromptDraft(
        director_summary="表现雨夜拔剑前的坚定", positive_prompt=text,
        negative_prompt="避免角色服装漂移",
    )


def _run(runtime: RuntimeStore) -> str:
    return runtime.create_run("comic", "comic.prompt.compile", {}, "prompt.compile").id


def test_first_shot_prompt_history_is_empty_not_a_generation_blocker(tmp_path: Path) -> None:
    _runtime, _projects, _assets, _boards, prompts, shot, _unrelated = _setup(tmp_path / "db")
    assert prompts.versions(shot.shot_id) == []
    with pytest.raises(ToolError, match="找不到"):
        prompts.versions("missing-shot")


def test_compiler_uses_only_pinned_shot_context_and_checks_constraints(tmp_path: Path) -> None:
    runtime, projects, assets, boards, prompts, shot, unrelated = _setup(tmp_path / "db.sqlite")
    snapshot, director, storyboard, current_shot, selected = prompts.source(shot.shot_id)
    seen = []

    def call(messages):
        seen.append(messages)
        return _draft().model_dump_json()

    compiler = compiler_for_model("qwen-image-3.0")
    draft = compiler.compile(
        snapshot=snapshot, director=director, storyboard=storyboard,
        shot=current_shot, assets=selected, model_target="qwen-image-3.0",
        model_call=call,
    )
    assert draft.positive_prompt == _draft().positive_prompt
    context = json.loads(seen[0][1]["content"])
    assert len(context["relevant_memory"]) == 2
    assert unrelated.asset_id not in seen[0][1]["content"]
    assert "chat_history" not in context
    assert "positive_prompt" not in context
    with pytest.raises(ToolError, match="遗漏不可变约束"):
        compiler.compile(
            snapshot=snapshot, director=director, storyboard=storyboard,
            shot=current_shot, assets=selected, model_target="qwen-image-3.0",
            model_call=lambda _messages: _draft("雨夜拔剑").model_dump_json(),
        )
    assert runtime.list_artifacts(type=ArtifactType.IMAGE) == []


def test_prompt_versions_artifacts_and_stale_shot_rejected(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    runtime, projects, assets, boards, prompts, shot, _unused = _setup(path)
    first = prompts.save(
        shot.shot_id, _draft(), expected_project_version=7, expected_shot_version=1,
        model_target="qwen-image-3.0", compiler_version="comic-prompt-1",
        run_id=_run(runtime),
    )
    assert first.version == 1
    assert first.character_asset_versions[0].version == 1
    artifact = runtime.get_artifact(first.artifact_id)
    assert artifact.type == ArtifactType.PROMPT
    assert artifact.metadata["positive_prompt"] == first.positive_prompt
    assert artifact.metadata["shot_version"] == 1
    assert runtime.list_artifacts(type=ArtifactType.IMAGE) == []
    with pytest.raises(ToolError, match="不可变约束"):
        prompts.save(
            shot.shot_id, _draft("雨夜拔剑"), expected_project_version=8,
            expected_shot_version=1, model_target="qwen-image-3.0",
            compiler_version="comic-prompt-1", run_id=_run(runtime),
        )
    assert len(runtime.list_artifacts(type=ArtifactType.PROMPT)) == 1
    revised_shot = boards.edit_shot(
        shot.shot_id, ComicShotDraft.model_validate({
            **shot.model_dump(include=set(ComicShotDraft.model_fields)),
            "action": "向来敌拔剑",
        }), expected_project_version=8, expected_version=1, status=ShotStatus.PLANNED,
    )
    with pytest.raises(ToolError, match="作品已由其他操作更新"):
        prompts.save(
            shot.shot_id, _draft(), expected_project_version=8, expected_shot_version=1,
            model_target="qwen-image-3.0", compiler_version="comic-prompt-1",
            run_id=_run(runtime),
        )
    second = prompts.save(
        shot.shot_id, _draft("东方仙侠 少女 雨夜 青色披风 向来敌拔剑"),
        expected_project_version=9, expected_shot_version=revised_shot.version,
        model_target="qwen-image-3.0", compiler_version="comic-prompt-1",
        run_id=_run(runtime),
    )
    assert second.version == 2 and second.artifact_id != first.artifact_id
    assert [item.version for item in prompts.versions(shot.shot_id)] == [2, 1]
    reopened_runtime = RuntimeStore(path)
    reopened_projects = ComicProjectStore(path)
    reopened_assets = ComicAssetStore(reopened_projects, reopened_runtime)
    reopened_boards = ComicStoryboardStore(reopened_projects, reopened_assets)
    assert ComicPromptStore(
        reopened_projects, reopened_boards, reopened_assets, reopened_runtime,
    ).get(shot.shot_id, version=1) == first
    assert projects.get(shot.project_id).project.current_version == 10
