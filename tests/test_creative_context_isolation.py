"""创意边界、追加分叉、Run 绑定和恢复隔离；离线契约不冒充语义实测。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_comic_assets import _style
from test_comic_director_coordinator import _parts
from test_web_studio import app as app_fixture

from kantoku.config import ToolError
from kantoku.core.runtime.store import RuntimeStore
from kantoku.domains.comic.models import ComicProjectInput, CreativeBriefFork, CreativeBriefUpdate
from kantoku.domains.comic.projects import ComicContextBuilder, ComicProjectStore
from kantoku.shells.web_studio import StudioApplication

app = app_fixture
OLD = "山海经穷奇站在悬崖边看远方村庄"
NEW = "给我生成一个JOJO版大耳朵图图里面牛爷爷打电话场景"


def _project(application: StudioApplication) -> str:
    return application.comic_projects.create(ComicProjectInput.model_validate({
        "title": OLD, "description": OLD,
        "brief": {"original_request": OLD, "hard_constraints": ["山海经", "穷奇"],
                  "soft_preferences": ["东方幻想"]},
    })).project.project_id


def _execute(application: StudioApplication, project_id: str, **options: Any) -> dict[str, Any]:
    return application.create_comic_director(project_id, {
        "expected_project_version": application.comic_projects.get(
            project_id,
        ).project.current_version,
        "creation_mode": "professional", **options,
    })


@pytest.fixture
def observed(app: StudioApplication, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def stage(skill_id: str, _inputs: Any, context: Any) -> dict[str, Any]:
        seen.append(json.loads(json.dumps(context)))
        parts = _parts()[skill_id]
        brief = context["creative_brief"]
        # 明确的离线阶段替身，测试工程隔离而非模型创造力。
        for output in parts.values():
            for key, value in output.items():
                if isinstance(value, str):
                    output[key] = f"{brief['original_request']}：{key}"
            if "hard_constraints" in output:
                output["hard_constraints"] = brief["hard_constraints"]
            if "creative_choices" in output:
                output["creative_choices"] = [f"视觉安排服务当前叙事：{brief['original_request']}"]
        return parts

    monkeypatch.setattr(app.comic_director, "stage_executor", lambda skill, inputs, context:
                        stage(skill, inputs, context["context"]))
    monkeypatch.setattr(app, "_comic_director_model", lambda _messages: json.dumps({
        "public_summary": "公开方案符合当前任务。", "confidence": 0.9,
        "findings": [], "suggested_patches": [],
    }))
    return seen


def _boundary(app: StudioApplication, monkeypatch: pytest.MonkeyPatch, *, fork: bool) -> list[Any]:
    calls: list[Any] = []

    def classify(messages: list[dict[str, str]]) -> str:
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        return json.dumps({
            "new_creative_direction": fork,
            "reason": "new creative direction" if fork else "same creative direction",
            "new_brief": {"original_request": payload["current_user_request"],
                          "hard_constraints": [payload["current_user_request"]]}
            if fork else None,
        }, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_intent_model", classify)
    return calls


def test_case1_modify_color_keeps_same_brief(
    app: StudioApplication, monkeypatch: pytest.MonkeyPatch, observed: list[Any],
) -> None:
    project_id = _project(app)
    calls = _boundary(app, monkeypatch, fork=False)
    result = _execute(app, project_id, task="修改穷奇颜色")
    assert result["status"] == "completed"
    run = app.runtime_store.get_run(result["run_id"])
    assert run.state["input_brief_version"] == 1
    assert not run.state["creative_context"]["fork_created"]
    assert calls[0]["current_brief"]["original_request"] == OLD
    assert observed[0]["creative_brief"]["original_request"] == OLD
    assert len(app.comic_projects.brief_versions(project_id)) == 1


@pytest.mark.parametrize("mode", ["fast", "professional"])
def test_three_requested_ideas_generate_independently_with_trace_and_same_runtime(
    app: StudioApplication, monkeypatch: pytest.MonkeyPatch, observed: list[Any], mode: str,
) -> None:
    # 同一作品中的连续创意，使用明确离线语义替身，不冒充在线导演质量验收。
    project_id = _project(app)
    _boundary(app, monkeypatch, fork=True)
    ideas = [OLD, "中式修仙少女站在竹林", "JOJO儿童风电话场景"]
    previous_requests: list[str] = []
    results: list[dict[str, Any]] = []
    for version, idea in enumerate(ideas, 1):
        observed.clear()
        result = _execute(app, project_id, task=idea, creation_mode=mode)
        assert result["status"] == "completed" and result["ready_for_prompt"]
        draft = result["director_spec"]
        assert draft["schema_version"] == 2
        assert idea in draft["creative_decision"]["intent_summary"]
        assert idea in draft["director_plan"]["visual_strategy"]
        assert draft["creative_brief_version"] == version
        run = app.runtime_store.get_run(result["run_id"])
        assert run.state["input_brief_version"] == version
        assert run.state["director_debug"]["brief_request"] == idea
        assert run.state["director_debug"]["memory_used"] == []
        assert run.state["previous_run_id"] is None
        assert all(item["creative_brief"]["original_request"] == idea for item in observed)
        serialized = json.dumps(observed, ensure_ascii=False)
        assert not any(old in serialized for old in previous_requests)
        assert [item["status"] for item in
                result["director_execution_summary"]["stage_statuses"]] == ["completed"] * 5
        results.append(result)
        previous_requests.append(idea)
    assert len({item["run_id"] for item in results}) == 3
    assert len({item["director_spec"]["creative_decision"]["intent_summary"]
                for item in results}) == 3
    assert len(app.runtime_store.list_runs(domain="comic")) == 3


@pytest.mark.parametrize("mode", ["fast", "professional"])
def test_case2_new_direction_forks_and_case4_debug_uses_current_brief(
    app: StudioApplication, monkeypatch: pytest.MonkeyPatch, observed: list[Any], mode: str,
) -> None:
    project_id = _project(app)
    style = app.comic_assets.create(project_id, _style(), expected_project_version=1)
    original = app.comic_projects.get(project_id, version=1).creative_brief
    calls = _boundary(app, monkeypatch, fork=True)
    result = _execute(app, project_id, task=NEW, creation_mode=mode)
    assert result["status"] == "completed"
    assert len(calls) == 1
    run = app.runtime_store.get_run(result["run_id"])
    brief = app.comic_projects.get(project_id).creative_brief
    assert brief.original_request == NEW
    assert brief.version == 2 and brief.parent_brief_version == 1
    assert brief.parent_brief_id == original.brief_id
    assert brief.status == "active" and brief.soft_preferences == []
    built = ComicContextBuilder.build(app.comic_projects.get(project_id), task=NEW)
    assert built.stable_context["project"] == {"project_id": project_id}
    assert not any(word in built.model_dump_json() for word in ("穷奇", "山海经", "悬崖", "村庄"))
    assert run.state["input_brief_id"] == brief.brief_id
    assert run.state["input_brief_version"] == 2
    assert run.state["director_debug"]["brief_request"] == NEW
    assert run.state["director_debug"]["memory_used"] == []
    assert run.state["input_versions"] == {"creative_brief": 2}
    for context in observed:
        assert context["creative_brief"]["original_request"] == NEW
        assert context["current_task"] == NEW
        assert context["relevant_assets"] == []
        assert set(context["project"]) == {"project_id"}
        assert not any(word in json.dumps(context, ensure_ascii=False)
                       for word in ("穷奇", "山海经", "悬崖", "村庄"))
    events = app.runtime_store.list_events(run.id)
    debug = next(event.payload for event in events
                 if event.payload.get("director_event") == "director_input_snapshot")
    assert debug["creative_context"]["fork_created"]
    assert debug["creative_context"]["brief_used"] == f"{brief.brief_id}@v2"
    saved = next(event.payload for event in events
                 if event.payload.get("director_event") == "director_spec_created")
    assert saved["input_brief_version"] == 2
    assert saved["director_spec_id"] == result["director_spec"]["spec_id"]
    assert app.comic_projects.get(project_id, version=1).creative_brief == original
    assert app.comic_projects.brief_versions(project_id)[1].status == "archived"
    assert app.comic_assets.get(project_id, style.asset_id).state == "active"
    # 重入同一新创意不二次分叉，且旧风格不能在下一轮重新污染。
    observed.clear()
    again = _execute(app, project_id, task=NEW, creation_mode=mode)
    assert again["status"] == "completed" and len(calls) == 1
    assert all(item["relevant_assets"] == [] for item in observed)
    assert len(app.comic_projects.brief_versions(project_id)) == 2


def test_case3_failed_run_cannot_resume_for_new_creative_request(
    app: StudioApplication, monkeypatch: pytest.MonkeyPatch, observed: list[Any],
) -> None:
    project_id = _project(app)
    working = app.comic_director.stage_executor
    monkeypatch.setattr(app.comic_director, "stage_executor", lambda *_args: (_ for _ in ()).throw(
        ToolError("simulated director failure"),
    ))
    with pytest.raises(ToolError, match="simulated"):
        _execute(app, project_id, task=OLD)
    failed = app.runtime_store.list_runs(domain="comic")[0]
    assert failed.status.value == "failed"
    calls = _boundary(app, monkeypatch, fork=True)
    with pytest.raises(ToolError, match="新输入不能恢复"):
        _execute(app, project_id, task="少女校园场景", resume_run_id=failed.id)
    assert calls == [] and observed == []
    monkeypatch.setattr(app.comic_director, "stage_executor", working)
    result = _execute(app, project_id, task="少女校园场景")
    assert result["status"] == "completed"
    new_run = app.runtime_store.get_run(result["run_id"])
    assert new_run.state["previous_run_id"] is None
    assert new_run.state["director_debug"]["brief_request"] == "少女校园场景"
    assert all("穷奇" not in json.dumps(context, ensure_ascii=False) for context in observed)
    # 同任务可以恢复；跨 Brief 版本必须拒绝复用旧阶段。
    with pytest.raises(ToolError, match="输入版本"):
        _execute(app, project_id, resume_run_id=failed.id)


def test_unknown_boundary_never_falls_back_to_old_context(
    app: StudioApplication, monkeypatch: pytest.MonkeyPatch, observed: list[Any],
) -> None:
    project_id = _project(app)
    monkeypatch.setattr(app, "_comic_intent_model", lambda _messages: "not valid JSON")
    with pytest.raises(ToolError, match="未继承旧 Context") as failure:
        _execute(app, project_id, task=NEW)
    assert failure.value._kantoku_public_failure["error_id"]
    assert failure.value._kantoku_public_failure["trace_id"]
    assert observed == [] and app.runtime_store.list_runs(domain="comic") == []
    assert app.comic_projects.get(project_id).creative_brief.version == 1


def test_fork_lifecycle_is_append_only_cas_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "comic.db"
    projects = ComicProjectStore(path)
    snapshot = projects.create(ComicProjectInput(title=OLD))
    project_id = snapshot.project.project_id
    revision = projects.replace_brief(project_id, CreativeBriefUpdate(
        expected_version=1, original_request=OLD, soft_preferences=["蓝色"],
    ))
    fork = CreativeBriefFork(
        expected_version=2, original_request=NEW,
        parent_brief_id=revision.creative_brief.brief_id, parent_brief_version=2,
        reason="new creative direction",
    )
    projects.fork_brief(project_id, fork)
    # 重复提交相同内容幂等，不会新增第二个生命周期事件。
    projects.fork_brief(project_id, fork)
    with pytest.raises(ToolError, match="其他操作更新"):
        projects.fork_brief(project_id, fork.model_copy(update={"original_request": "新科幻创意"}))
    restarted = ComicProjectStore(path)
    assert [item.status for item in restarted.brief_versions(project_id)] == [
        "active", "archived", "archived",
    ]
    assert restarted.get(project_id, version=1) == snapshot
    assert restarted.get(project_id, version=2) == revision
    with restarted._connect() as connection:
        assert connection.execute("SELECT count(*) FROM comic_entity_versions WHERE "
                                  "entity_type='creative_brief_lifecycle'").fetchone()[0] == 1
    assert RuntimeStore(path).list_runs() == []


def test_boundary_rejects_invented_new_hard_constraints(app: StudioApplication) -> None:
    project_id = _project(app)
    brief = app.comic_projects.get(project_id).creative_brief
    with pytest.raises(ToolError, match="不是当前用户原文"):
        ComicContextBuilder.check_intent_boundary(NEW, brief, model_call=lambda _messages:
            json.dumps({"new_creative_direction": True, "reason": "new creative direction",
                        "new_brief": {"original_request": NEW, "hard_constraints": ["穷奇"]}}))


def test_explicit_asset_reference_can_reuse_old_asset(
    app: StudioApplication, monkeypatch: pytest.MonkeyPatch, observed: list[Any],
) -> None:
    project_id = _project(app)
    asset = app.comic_assets.create(project_id, _style(), expected_project_version=1)
    _boundary(app, monkeypatch, fork=True)
    result = _execute(app, project_id, task=NEW, asset_ids=[asset.asset_id])
    assert result["status"] == "completed"
    assert observed[0]["relevant_assets"][0]["asset_id"] == asset.asset_id


def test_failed_run_same_request_recovers_after_store_restart(
    app: StudioApplication, monkeypatch: pytest.MonkeyPatch, observed: list[Any],
) -> None:
    project_id = _project(app)
    working = app.comic_director.stage_executor

    def fail_second_stage(skill_id: str, inputs: Any, context: Any) -> dict[str, Any]:
        if skill_id == "comic.visual_direction":
            raise ToolError("interrupted director stage")
        return working(skill_id, inputs, context)

    monkeypatch.setattr(app.comic_director, "stage_executor", fail_second_stage)
    with pytest.raises(ToolError, match="interrupted"):
        _execute(app, project_id, task=OLD)
    failed = app.runtime_store.list_runs(domain="comic")[0]
    restarted = RuntimeStore(app.runtime_store.path)
    assert restarted.get_run(failed.id).state["input_brief_version"] == 1
    observed.clear()
    monkeypatch.setattr(app.comic_director, "stage_executor", working)
    resumed = _execute(app, project_id, resume_run_id=failed.id)
    assert resumed["status"] == "completed"
    run = app.runtime_store.get_run(resumed["run_id"])
    assert run.state["previous_run_id"] == failed.id
    assert run.state["director_debug"]["reused_stages"] == ["comic.creative_understanding"]
    assert len(observed) == 2
    assert all(context["creative_brief"]["original_request"] == OLD for context in observed)


def test_boundary_uses_shared_model_and_no_keyword_fallback(
    app: StudioApplication, monkeypatch: pytest.MonkeyPatch, observed: list[Any],
) -> None:
    project_id = _project(app)
    calls = []
    original_model = app._comic_director_model
    monkeypatch.delattr(app, "_comic_intent_model")

    def shared_model(messages: list[dict[str, str]]) -> str:
        if '"title": "CreativeIntentBoundary"' in messages[0]["content"]:
            calls.append(json.loads(messages[1]["content"]))
            return json.dumps({
                "new_creative_direction": True, "reason": "题材和人物关系改变",
                "new_brief": {"original_request": NEW, "hard_constraints": ["JOJO", "牛爷爷"]},
            }, ensure_ascii=False)
        return original_model(messages)

    monkeypatch.setattr(app, "_comic_director_model", shared_model)
    result = _execute(app, project_id, task=NEW)
    assert result["status"] == "completed"
    assert calls[0]["current_user_request"] == NEW
    assert calls[0]["current_brief"]["original_request"] == OLD
    assert app.comic_projects.get(project_id).creative_brief.hard_constraints == ["JOJO", "牛爷爷"]


def test_branch_assets_stay_isolated_after_brief_edit(
    app: StudioApplication, monkeypatch: pytest.MonkeyPatch, observed: list[Any],
) -> None:
    project_id = _project(app)
    old_style = app.comic_assets.create(project_id, _style(), expected_project_version=1)
    _boundary(app, monkeypatch, fork=True)
    _execute(app, project_id, task=NEW)
    snapshot = app.comic_projects.get(project_id)
    app.comic_projects.replace_brief(project_id, CreativeBriefUpdate(
        expected_version=snapshot.project.current_version, original_request=NEW,
        hard_constraints=[NEW], soft_preferences=["保持电话场景"],
    ))
    snapshot = app.comic_projects.get(project_id)
    edited_style = _style().model_copy(update={"name": "旧作品风格的修订"})
    app.comic_assets.change(
        project_id, old_style.asset_id, action="edit", draft=edited_style,
        expected_project_version=snapshot.project.current_version, expected_asset_version=1,
    )
    observed.clear()
    result = _execute(app, project_id, task=NEW)
    assert result["status"] == "completed"
    assert app.comic_projects.get(project_id).creative_brief.version == 3
    assert all(context["relevant_assets"] == [] for context in observed)
