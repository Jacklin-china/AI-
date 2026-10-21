"""版本删除：Prompt 版本与导演方案版本的「只删历史、不动当前」约束。

删除功能此前不存在（版本只增不删是刻意设计），因此本文件覆盖三条边界：
① 删除只影响目标版本，其他版本与当前指针不变；
② 当前正在使用的版本必须被拒绝（否则 current_version 会悬空）；
③ 作品级乐观并发校验必须生效，且删除本身也计入一次作品修订。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kantoku.config import ToolError
from kantoku.core.runtime.store import RuntimeStore
from kantoku.domains.comic.assets import ComicAssetStore
from kantoku.domains.comic.models import (
    ComicAssetDraft,
    ComicProjectInput,
    ComicPromptDraft,
    ComicShotDraft,
    ComicStoryboardDraft,
    DirectorSpecDraft,
)
from kantoku.domains.comic.projects import ComicProjectStore
from kantoku.domains.comic.prompts import ComicPromptStore
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
    scene = assets.create(project.project_id, ComicAssetDraft.model_validate({
        "name": "竹林", "details": {"kind": "scene", "location": "雨夜竹林"},
    }), expected_project_version=2)
    director = projects.save_director(project.project_id, DirectorSpecDraft.model_validate({
        "visual_direction": "用空间展现人物抉择", "storytelling_goal": "表现拔剑前的坚定",
        "camera_language": "镜头贴近人物行动", "composition": "人物与竹林有清晰空间关系",
        "lighting": "雨幕中保留轮廓", "color_language": "冷暖层次",
        "emotion": "坚定", "character_focus": "拔剑姿态",
        "constraints": ["东方仙侠", "少女", "雨夜"],
        "creative_choices": ["让环境先建立危险感"],
    }), expected_project_version=3)
    storyboard = storyboards.create(
        project.project_id, ComicStoryboardDraft(title="雨夜迎战"),
        expected_project_version=4, director_spec_version=director.version,
    )
    shot = storyboards.add_shot(
        storyboard.storyboard_id, ComicShotDraft.model_validate({
            "purpose": "交代拔剑时刻", "subject": "阿青", "action": "拔剑",
            "environment": "雨夜竹林", "character_asset_versions": [
                {"asset_id": character.asset_id, "version": 1},
            ], "scene_asset_versions": [
                {"asset_id": scene.asset_id, "version": 1},
            ],
        }), expected_project_version=5, expected_storyboard_version=1,
    )
    return runtime, projects, assets, storyboards, prompts, project, shot


def _prompt_draft(text: str = "东方仙侠 少女 雨夜 青色披风 拔剑") -> ComicPromptDraft:
    return ComicPromptDraft(
        director_summary="表现雨夜拔剑前的坚定", positive_prompt=text,
        negative_prompt="避免角色服装漂移",
    )


def _save_prompt(prompts, runtime, shot, *, expected_project_version: int,
                 text: str = "东方仙侠 少女 雨夜 青色披风 拔剑"):
    run_id = runtime.create_run("comic", "comic.prompt.compile", {}, "prompt.compile").id
    return prompts.save(
        shot.shot_id, _prompt_draft(text),
        expected_project_version=expected_project_version,
        expected_shot_version=shot.version, model_target="qwen-image-3.0",
        compiler_version="test-compiler-v1", run_id=run_id,
    )


def test_prompt_version_delete_removes_only_the_target_version(tmp_path: Path) -> None:
    runtime, projects, _assets, _boards, prompts, project, shot = _setup(tmp_path / "db.sqlite")
    _save_prompt(prompts, runtime, shot, expected_project_version=6)
    second = _save_prompt(prompts, runtime, shot, expected_project_version=7,
                          text="东方仙侠 少女 雨夜 青色披风 拔剑 更近的景别")
    assert [item.version for item in prompts.versions(shot.shot_id)] == [2, 1]

    result = prompts.delete_version(
        shot.shot_id, version=1, expected_project_version=8,
    )

    # 只掉了 v1；当前指针仍在 v2，内容不变。
    assert result["deleted_version"] == 1
    assert [item.version for item in prompts.versions(shot.shot_id)] == [2]
    assert prompts.get(shot.shot_id).version == second.version
    assert result["project_version"] == 9
    assert projects.get(project.project_id).project.current_version == 9


def test_prompt_current_version_cannot_be_deleted(tmp_path: Path) -> None:
    runtime, _projects, _assets, _boards, prompts, _project, shot = _setup(tmp_path / "db.sqlite")
    _save_prompt(prompts, runtime, shot, expected_project_version=6)

    with pytest.raises(ToolError, match="当前正在使用的 Prompt 版本不能删除"):
        prompts.delete_version(shot.shot_id, version=1, expected_project_version=7)

    # 被拒绝的删除不得留下任何副作用。
    assert [item.version for item in prompts.versions(shot.shot_id)] == [1]


def test_prompt_version_delete_rejects_stale_project_version(tmp_path: Path) -> None:
    runtime, _projects, _assets, _boards, prompts, _project, shot = _setup(tmp_path / "db.sqlite")
    _save_prompt(prompts, runtime, shot, expected_project_version=6)
    _save_prompt(prompts, runtime, shot, expected_project_version=7,
                 text="东方仙侠 少女 雨夜 青色披风 拔剑 更近的景别")

    with pytest.raises(ToolError, match="作品已由其他操作更新"):
        prompts.delete_version(shot.shot_id, version=1, expected_project_version=7)

    assert [item.version for item in prompts.versions(shot.shot_id)] == [2, 1]


def test_prompt_version_delete_rejects_missing_version(tmp_path: Path) -> None:
    runtime, _projects, _assets, _boards, prompts, _project, shot = _setup(tmp_path / "db.sqlite")
    _save_prompt(prompts, runtime, shot, expected_project_version=6)

    with pytest.raises(ToolError, match="找不到指定 Prompt 版本"):
        prompts.delete_version(shot.shot_id, version=9, expected_project_version=7)


def test_director_version_delete_removes_only_the_target_version(tmp_path: Path) -> None:
    _runtime, projects, _assets, _boards, _prompts, project, _shot = _setup(tmp_path / "db.sqlite")
    assert [item.version for item in projects.director_versions(project.project_id)] == [1]

    second = projects.save_director(project.project_id, DirectorSpecDraft.model_validate({
        "visual_direction": "改用长镜头跟随", "storytelling_goal": "表现拔剑前的坚定",
        "camera_language": "镜头贴近人物行动", "composition": "人物与竹林有清晰空间关系",
        "lighting": "雨幕中保留轮廓", "color_language": "冷暖层次",
        "emotion": "坚定", "character_focus": "拔剑姿态",
        "constraints": ["东方仙侠", "少女", "雨夜"],
        "creative_choices": ["让环境先建立危险感"],
    }), expected_project_version=6)
    assert [item.version for item in projects.director_versions(project.project_id)] == [2, 1]

    result = projects.delete_director_version(
        project.project_id, version=1, expected_project_version=7,
    )

    assert result["deleted_version"] == 1
    assert [item.version for item in projects.director_versions(project.project_id)] == [2]
    assert projects.get_director(project.project_id).version == second.version
    assert projects.get(project.project_id).project.current_version == 8


def test_director_current_version_cannot_be_deleted(tmp_path: Path) -> None:
    _runtime, projects, _assets, _boards, _prompts, project, _shot = _setup(tmp_path / "db.sqlite")
    projects.save_director(project.project_id, DirectorSpecDraft.model_validate({
        "visual_direction": "改用长镜头跟随", "storytelling_goal": "表现拔剑前的坚定",
        "camera_language": "镜头贴近人物行动", "composition": "人物与竹林有清晰空间关系",
        "lighting": "雨幕中保留轮廓", "color_language": "冷暖层次",
        "emotion": "坚定", "character_focus": "拔剑姿态",
        "constraints": ["东方仙侠", "少女", "雨夜"],
        "creative_choices": ["让环境先建立危险感"],
    }), expected_project_version=6)

    with pytest.raises(ToolError, match="当前正在使用的导演方案版本不能删除"):
        projects.delete_director_version(
            project.project_id, version=2, expected_project_version=7,
        )

    assert [item.version for item in projects.director_versions(project.project_id)] == [2, 1]
