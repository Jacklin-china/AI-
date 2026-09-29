"""当前创意隔离、可编辑草稿、不可变版本和服务端确认回归。"""

import json

import pytest
from test_comic_assets import _style
from test_comic_creation_mode import _execute
from test_comic_creation_mode import app as app_fixture
from test_comic_creation_mode import model as model_fixture
from test_comic_director_coordinator import SKILLS, _project
from test_creative_context_isolation import NEW, OLD, _boundary
from test_creative_context_isolation import observed as observed_fixture

from kantoku.config import ToolError
from kantoku.domains.comic.models import ComicProjectInput, DirectorSpecDraft
from kantoku.domains.comic.projects import ComicContextBuilder
from kantoku.shells.web_studio import StudioApplication

app = app_fixture
model = model_fixture
observed = observed_fixture


def _body(application, project_id, spec, **extra):
    return {
        "expected_project_version": application.comic_projects.get(
            project_id).project.current_version,
        "expected_director_version": spec["version"], **extra,
    }


def test_navigation_metadata_never_enters_any_context(app):
    snapshot = app.comic_projects.create(ComicProjectInput.model_validate({
        "title": OLD, "description": OLD, "brief": {"original_request": NEW},
    }))
    context = ComicContextBuilder.build(snapshot).model_dump_json()
    assert NEW in context
    assert not any(word in context for word in ("山海经", "穷奇", "悬崖", "村庄"))


@pytest.mark.parametrize("mode", ["fast", "professional"])
def test_explicit_new_request_cannot_be_classified_as_old_modification(
    app, monkeypatch, observed, mode,
):
    project_id = app.comic_projects.create(ComicProjectInput.model_validate({
        "title": OLD, "description": OLD, "brief": {"original_request": OLD},
    })).project.project_id
    calls = _boundary(app, monkeypatch, fork=False)
    result = _execute(app, project_id, mode, task=NEW, creative_operation="new")
    assert result["status"] == "completed" and not result["ready_for_prompt"]
    assert calls == []
    context = json.dumps(observed, ensure_ascii=False)
    assert NEW in context and "山海经" not in context and "穷奇" not in context
    assert app.runtime_store.get_run(result["run_id"]).state["input_brief_version"] == 2


@pytest.mark.parametrize("mode", ["fast", "professional"])
def test_edit_save_review_confirm_restore_uses_same_core(app, model, mode):
    project_id = _project(app.comic_projects)
    first = _execute(app, project_id, mode)
    spec = first["director_spec"]
    assert not spec["user_confirmed"] and not first["ready_for_prompt"]
    with pytest.raises(ToolError, match="确认"):
        app.create_comic_storyboard(project_id, {
            "expected_project_version": 2, "generate": True,
        })
    confirmed = app.confirm_comic_director(project_id, {
        "version": spec["version"], "expected_project_version": 2,
    })
    assert confirmed["user_confirmed"]
    original = app.comic_projects.get_director(project_id)
    draft = original.model_dump(include=set(DirectorSpecDraft.model_fields), mode="json")
    draft["creative_decision"]["emotional_target"] = "坚定而平静"
    saved = app.create_comic_director(project_id, _body(app, project_id, spec, draft=draft))
    assert saved["version"] == 2 and saved["critic_result"] is None
    assert not saved["user_confirmed"]
    with pytest.raises(ToolError):
        app.confirm_comic_director(project_id, {"version": 2, "expected_project_version": 3})
    model.clear()
    reviewed = _execute(app, project_id, mode, review_current=True, expected_director_version=2)
    assert model == [SKILLS[3]]  # 不重新生成前三阶段，审核当前手工稿。
    assert reviewed["director_spec"]["creative_decision"]["emotional_target"] == "坚定而平静"
    assert reviewed["director_spec"]["version"] == 3 and not reviewed["ready_for_prompt"]
    app.confirm_comic_director(project_id, {"version": 3, "expected_project_version": 4})
    restored = app.restore_comic_director(project_id, {"version": 1, "expected_project_version": 4})
    assert restored["version"] == 4 and not restored["user_confirmed"]
    assert app.comic_projects.director_versions(project_id)[-1] == original
    assert app.get_comic_director(project_id)["version"] == 4
    assert len(app.comic_projects.director_versions(project_id)) == 4
    assert app.comic_projects.runtime_store is app.runtime_store
    # 真正重建应用实例，审批和当前修订由同一 Core 数据库恢复。
    app.confirm_comic_director(project_id, {"version": 4, "expected_project_version": 5})
    restarted = StudioApplication()
    assert restarted.get_comic_director(project_id)["user_confirmed"]
    tasks = restarted.list_comic_project_tasks(project_id)["tasks"]
    assert any(item.get("director_spec", {}).get("version") == 2 for item in tasks
               if item.get("director_spec"))


def test_edit_cannot_change_bound_constraints_or_forge_review(app, model):
    project_id = _project(app.comic_projects)
    spec = _execute(app, project_id, "fast")["director_spec"]
    draft = {key: value for key, value in spec.items() if key in DirectorSpecDraft.model_fields}
    draft["creative_decision"]["hard_constraints"] = ["机器人"]
    with pytest.raises(ToolError, match="硬约束"):
        app.create_comic_director(project_id, _body(app, project_id, spec, draft=draft))
    assert app.comic_projects.get_director(project_id).version == 1


def test_instruction_revision_only_uses_current_bound_draft(app, model, monkeypatch):
    project_id = _project(app.comic_projects)
    spec = _execute(app, project_id, "fast")["director_spec"]
    seen = []

    def edit(messages):
        context = json.loads(messages[1]["content"])
        seen.append(context)
        draft = context["current_draft"]
        draft["director_plan"]["composition_strategy"] = "加大雨幕环境占比"
        draft["critic_result"] = None
        return json.dumps(draft, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", edit)
    saved = app.create_comic_director(project_id, _body(
        app, project_id, spec, revision_instruction="增加环境比例，保持人物设定",
    ))
    assert seen[0]["revision_instruction"] == "增加环境比例，保持人物设定"
    assert saved["director_plan"]["composition_strategy"] == "加大雨幕环境占比"
    assert saved["creative_brief_version"] == 1 and saved["version"] == 2
    assert not saved["user_confirmed"]


def test_confirmed_draft_with_changed_asset_cannot_start_next_workflow(app, model, monkeypatch):
    project_id = _project(app.comic_projects)
    asset = app.comic_assets.create(project_id, _style(), expected_project_version=1)
    spec = _execute(app, project_id, "fast", asset_ids=[asset.asset_id])["director_spec"]
    app.confirm_comic_director(project_id, {"version": 1, "expected_project_version": 3})
    assert spec["asset_versions"] == {f"asset:{asset.asset_id}": 1}
    revised_asset = _style()
    revised_asset.details.color_language = "暖色晨光与柔和环境光"
    changed = app.comic_assets.change(
        project_id, asset.asset_id, action="edit", draft=revised_asset,
        expected_project_version=3, expected_asset_version=1,
    )
    assert changed.version == 2
    calls = []
    monkeypatch.setattr(app, "_comic_storyboard_model", lambda messages: calls.append(messages))
    with pytest.raises(ToolError, match="资产版本"):
        app.create_comic_storyboard(project_id, {
            "expected_project_version": app.comic_projects.get(
                project_id).project.current_version, "generate": True,
        })
    assert calls == []
