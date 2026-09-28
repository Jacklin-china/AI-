"""Phase 7.2.5：真实应用入口、模式投影、节点编辑与持久化恢复。"""

from __future__ import annotations

import json
import sqlite3
from http.client import HTTPConnection
from typing import Any

import pytest
from loguru import logger
from test_comic_director_coordinator import SKILLS, _parts, _project
from test_web_studio import app as app_fixture
from test_web_studio import server as server_fixture

from kantoku.config import ToolError
from kantoku.core.runtime.models import ExecutionStatus
from kantoku.domains.comic.critic import require_approved_director
from kantoku.domains.comic.models import (
    ComicProjectInput,
    CreativeBriefUpdate,
    DirectorSpecDraft,
    DirectorSpecRequest,
)
from kantoku.shells import web_studio

# 复用已有离线 fixture，不复制应用初始化逻辑。
app = app_fixture
server = server_fixture


@pytest.fixture
def model(app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def call(messages: list[dict[str, str]]) -> str:
        instruction = messages[0]["content"]
        for skill_id, name in zip(SKILLS[:3], (
            "CreativeDecision", "DirectorPlan", "CinematographyPlan",
        ), strict=True):
            if f'"title": "{name}"' in instruction:
                calls.append(skill_id)
                context = json.loads(messages[1]["content"])
                assert "chat_history" not in context["context"]
                return json.dumps(next(iter(_parts()[skill_id].values())), ensure_ascii=False)
        calls.append("comic.director_critic")
        return json.dumps({
            "public_summary": "约束与视觉因果符合故事。", "confidence": 0.9,
            "findings": [], "suggested_patches": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", call)
    return calls


def _execute(application: web_studio.StudioApplication, project_id: str, mode: str,
             **options: Any) -> dict[str, Any]:
    return application.create_comic_director(project_id, {
        "expected_project_version": application.comic_projects.get(
            project_id,
        ).project.current_version,
        "creation_mode": mode, **options,
    })


def test_director_input_snapshot_logs_current_request_and_version_trace(
    app: web_studio.StudioApplication, model: list[str],
) -> None:
    project_id = _project(app.comic_projects)
    conversation = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})
    messages: list[str] = []
    sink = logger.add(lambda record: messages.append(record.record["message"]))
    try:
        result = _execute(app, project_id, "professional", task="强调人物迎战前的决心",
                          conversation_id=conversation["id"])
    finally:
        logger.remove(sink)
    snapshot = next(message for message in messages if "Director Input Snapshot" in message)
    assert f"project_id={project_id}" in snapshot
    assert f"conversation_id={conversation['id']}" in snapshot
    assert "user_request=强调人物迎战前的决心" in snapshot
    assert "brief_version=1" in snapshot
    assert "memory_used=[]" in snapshot and "asset_used={}" in snapshot
    assert "previous_run=-" in snapshot
    trace_id = app.runtime_store.get_run(result["run_id"]).state["trace_id"]
    assert f"trace_id={trace_id}" in snapshot


@pytest.mark.parametrize("cancel_at", ["CreativeDecision", "CinematographyPlan", "critic"])
def test_cancelled_run_stops_at_stage_boundary_without_saved_director(
    app: web_studio.StudioApplication, model: list[str], monkeypatch: pytest.MonkeyPatch,
    cancel_at: str,
) -> None:
    project_id = _project(app.comic_projects)
    original = app._comic_director_model

    def cancelling(messages: list[dict[str, str]]) -> str:
        instruction = messages[0]["content"]
        if (cancel_at != "critic" and f'"title": "{cancel_at}"' in instruction) or (
            cancel_at == "critic" and "findings 每项" in instruction
        ):
            run = app.runtime_store.list_runs(domain="comic")[0]
            cancelled = app.cancel_core_run(run.id)
            assert cancelled["status"] == "cancelled"
        return original(messages)

    monkeypatch.setattr(app, "_comic_director_model", cancelling)
    result = _execute(app, project_id, "professional")
    assert result["status"] == "cancelled"
    assert result["director_spec"] is None
    assert app.comic_projects.get(project_id).project.director_version is None
    assert len(model) == {"CreativeDecision": 1, "CinematographyPlan": 3, "critic": 4}[cancel_at]
    events = app.runtime_store.list_events(result["run_id"])
    assert not any(event.payload.get("director_event") == "director_spec_created"
                   for event in events)


def test_diverse_creative_contracts_use_current_project_context_not_previous_outputs(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """离线响应验证真实链路隔离，不声称证明线上模型的艺术质量。"""
    cases = [
        ("山海经穷奇站在悬崖边看远方村庄", "异兽与村庄的距离表达观察而非攻击"),
        ("中式修仙少女穿白色古装站在树梢看远方村庄", "树梢人物与村落遥相呼应，保持白衣"),
        ("少年雨夜坐在屋檐下思念故乡", "屋檐遮蔽与雨中远处灯光形成归属反差"),
        ("未来城市机器人咖啡师", "机械手与咖啡器具表现未来日常的温度"),
    ]
    seen: list[str] = []

    def call(messages: list[dict[str, str]]) -> str:
        payload = json.loads(messages[1]["content"])
        if "inputs" not in payload:
            # 故意返回非法上下文字段，必须忽略而不使人物类任务失败。
            return json.dumps({
                "public_summary": "方案符合当前故事。", "confidence": 0.9,
                "findings": [{"code": "MODEL_PATH", "severity": "warning",
                              "field_path": "asset.character_id", "evidence": "无",
                              "expected": "无", "suggested_action": "无"}],
                "suggested_patches": [],
            }, ensure_ascii=False)
        context = payload["context"]
        request = context["creative_brief"]["original_request"]
        assert request == cases[len(seen) // 3][0]
        assert "chat_history" not in context
        assert context["relevant_assets"] == []
        seen.append(request)
        plan_text = next(plan for text, plan in cases if text == request)
        parts = _parts()
        parts[SKILLS[0]]["creative_decision"].update({
            "intent_summary": request, "narrative_context": request,
            "hard_constraints": [request],
            "emotional_target": plan_text, "audience_experience": plan_text,
            "narrative_focus": request,
        })
        parts[SKILLS[1]]["director_plan"].update({
            "visual_strategy": plan_text, "visual_focus": request,
            "creative_choices": [plan_text], "composition_strategy": plan_text,
            "color_strategy": "依据当前场景的环境色形成主体关系",
        })
        parts[SKILLS[2]]["cinematography"].update({
            "shot_size": "按当前主体和环境关系取景", "camera_angle": "平视",
            "camera_distance": "同时容纳主体与环境", "spatial_feel": plan_text,
            "lighting": "当前场景的环境光", "light_source": "场景内光源",
            "light_direction": "侧方", "color_relationship": "主体和背景分离",
            "depth_strategy": plan_text, "material_language": "保留当前主体材质",
        })
        for skill_id, name in zip(SKILLS[:3], (
            "CreativeDecision", "DirectorPlan", "CinematographyPlan",
        ), strict=True):
            if f'"title": "{name}"' in messages[0]["content"]:
                return json.dumps(next(iter(parts[skill_id].values())), ensure_ascii=False)
        pytest.fail("unexpected model stage")

    monkeypatch.setattr(app, "_comic_director_model", call)
    plans = []
    for request, _strategy in cases:
        project = app.comic_projects.create(ComicProjectInput.model_validate({
            "title": request,
            "brief": {"original_request": request, "hard_constraints": [request]},
        }))
        result = _execute(app, project.project.project_id, "professional", task=request)
        assert result["status"] == "completed"
        assert result["director_spec"]["creative_decision"]["intent_summary"] == request
        plans.append(result["director_spec"]["director_plan"]["visual_strategy"])
    assert len(set(plans)) == 4
    assert len(seen) == 12


@pytest.mark.parametrize("mode", ["fast", "professional"])
def test_creation_mode_executes_through_shared_coordinator(
    app: web_studio.StudioApplication, model: list[str], mode: str,
) -> None:
    project_id = _project(app.comic_projects)
    result = _execute(app, project_id, mode)
    spec = DirectorSpecDraft.model_validate({
        key: value for key, value in result["director_spec"].items()
        if key in DirectorSpecDraft.model_fields
    })
    require_approved_director(spec)
    assert result["status"] == "completed"
    assert model == SKILLS[:4]
    assert app.comic_director.runtime_store is app.runtime_store
    run = app.runtime_store.get_run(result["run_id"])
    assert run.workflow == "comic.director"
    assert run.state["completed_stages"] == SKILLS
    names = [event.payload.get("director_event")
             for event in app.runtime_store.list_events(run.id)]
    assert "director_mode_selected" in names
    assert names.count("director_stage_visible") == names.count("director_stage_completed") == 5
    assert app.list_core_runs() == []
    summary = result["director_execution_summary"]
    assert summary["mode"] == mode
    if mode == "fast":
        assert summary["stages"] == []
        assert "critic_result" not in summary
        assert summary["available_actions"] == ["view"]
    else:
        assert [item["stage"] for item in summary["stages"]] == [
            skill.removeprefix("comic.") for skill in SKILLS
        ]
        assert all(item["input_versions"]["creative_brief"] == 1 for item in summary["stages"])
        assert all(item["output_summary"] for item in summary["stages"])
        assert summary["critic_result"]["verdict"] == "pass"


def test_modes_have_same_v2_contract_and_do_not_add_runtime_tables(
    app: web_studio.StudioApplication, model: list[str],
) -> None:
    def tables() -> set[str]:
        with sqlite3.connect(app.runtime_store.path) as connection:
            return {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}

    initial = tables()
    fast = _execute(app, _project(app.comic_projects), "fast")
    professional = _execute(app, _project(app.comic_projects), "professional")
    assert set(fast["director_spec"]) == set(professional["director_spec"])
    assert fast["director_spec"]["schema_version"] == 2
    assert professional["director_spec"]["schema_version"] == 2
    assert tables() == initial
    assert len(app.runtime_store.list_runs()) == 2


def test_professional_stage_edit_appends_version_and_reaudits(
    app: web_studio.StudioApplication, model: list[str],
) -> None:
    project_id = _project(app.comic_projects)
    first = _execute(app, project_id, "professional")
    plan = dict(first["director_spec"]["director_plan"])
    plan["composition_strategy"] = "让少女在竹林留白中迎战，保留雨夜空间关系"
    model.clear()
    edited = _execute(app, project_id, "professional", previous_run_id=first["run_id"],
                      rerun_from="visual_direction", stage_edits={"visual_direction": plan})
    assert edited["director_spec"]["version"] == 2
    assert edited["director_spec"]["composition"] == plan["composition_strategy"]
    assert model == ["comic.cinematography", "comic.director_critic"]
    historical = app.list_comic_project_tasks(project_id)["tasks"]
    original = next(item for item in historical if item["run_id"] == first["run_id"])
    assert original["director_spec"] == first["director_spec"]
    assert app.runtime_store.get_run(first["run_id"]).status is ExecutionStatus.COMPLETED


@pytest.mark.parametrize("mode", ["fast", "professional"])
def test_failure_resume_keeps_completed_outputs(
    app: web_studio.StudioApplication, model: list[str], monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    project_id = _project(app.comic_projects)
    real = app._comic_director_model

    def broken(messages: list[dict[str, str]]) -> str:
        if '"title": "DirectorPlan"' in messages[0]["content"]:
            raise ToolError("模拟视觉导演失败")
        return real(messages)

    monkeypatch.setattr(app, "_comic_director_model", broken)
    with pytest.raises(ToolError):
        _execute(app, project_id, mode)
    failed = app.list_comic_project_tasks(project_id)["tasks"][0]
    assert failed["status"] == "failed"
    assert failed["director_spec"] is None
    assert failed["director_execution_summary"]["error_id"]
    model.clear()
    monkeypatch.setattr(app, "_comic_director_model", real)
    recovered = _execute(app, project_id, mode, resume_run_id=failed["run_id"])
    assert recovered["status"] == "completed"
    assert model == SKILLS[1:4]
    recovered_run = app.runtime_store.get_run(recovered["run_id"])
    assert recovered_run.state["previous_run_id"] == failed["run_id"]


def test_restart_recovers_persisted_run_not_new_runtime(
    app: web_studio.StudioApplication, model: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _project(app.comic_projects)
    first = _execute(app, project_id, "professional")
    run = app.runtime_store.get_run(first["run_id"])
    state = dict(run.state)
    state["completed_stages"] = SKILLS[:2]
    state["stage_outputs"] = {key: value for key, value in state["stage_outputs"].items()
                              if key in SKILLS[:2]}
    app.runtime_store.update_run(run.id, status=ExecutionStatus.RUNNING, state=state,
                                 current_node="cinematography", force=True)
    with pytest.raises(ToolError, match="仍在执行"):
        _execute(app, project_id, "professional", resume_run_id=run.id)
    restarted = web_studio.StudioApplication()
    monkeypatch.setattr(restarted, "_comic_director_model", app._comic_director_model)
    assert restarted.list_comic_project_tasks(project_id)["tasks"][0]["recovery_required"]
    model.clear()
    recovered = _execute(restarted, project_id, "professional", resume_run_id=run.id)
    assert recovered["status"] == "completed"
    assert model == SKILLS[2:4]
    assert restarted.runtime_store.path == app.runtime_store.path


def test_rejects_mode_change_constraint_edit_and_critic_forgery(
    app: web_studio.StudioApplication, model: list[str],
) -> None:
    project_id = _project(app.comic_projects)
    first = _execute(app, project_id, "professional")
    with pytest.raises(ToolError, match="模式"):
        _execute(app, project_id, "fast", resume_run_id=first["run_id"])
    with pytest.raises(ValueError):
        DirectorSpecRequest.model_validate({
            "creation_mode": "professional", "expected_project_version": 2,
            "previous_run_id": first["run_id"], "rerun_from": "director_critic",
            "stage_edits": {"director_critic": {"verdict": "pass"}},
        })
    decision = dict(first["director_spec"]["creative_decision"])
    decision["hard_constraints"] = ["现代都市"]
    blocked = _execute(app, project_id, "professional", previous_run_id=first["run_id"],
                       rerun_from="creative_understanding",
                       stage_edits={"creative_understanding": decision})
    assert blocked["status"] == "waiting"
    assert blocked["director_spec"] is None
    assert app.comic_projects.get_director(project_id).version == 1


def test_mode_http_entry_preserves_legacy_api_and_prompt_readiness(
    server: int, app: web_studio.StudioApplication, model: list[str],
) -> None:
    project_id = _project(app.comic_projects)
    connection = HTTPConnection("127.0.0.1", server, timeout=10)
    try:
        connection.request(
            "POST", f"/api/comic/projects/{project_id}/director-spec",
            body=json.dumps({"creation_mode": "fast", "expected_project_version": 1}),
            headers={"X-Studio-Token": app.token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 201, result
        assert result["director_spec"]["schema_version"] == 2
        assert result["director_execution_summary"]["stages"] == []
        connection.request("GET", f"/api/comic/projects/{project_id}/director-spec",
                           headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        saved = json.loads(response.read())
        assert response.status == 200
        assert saved["spec_id"] == result["director_spec"]["spec_id"]
        # 实际 Prompt Compiler 的审核门禁认可此方案，没有图片模型调用。
        require_approved_director(app.comic_projects.get_director(project_id))
    finally:
        connection.close()


def test_stage_contract_rejects_cot_and_does_not_fake_result(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        **_parts()[SKILLS[0]]["creative_decision"], "reasoning": "private",
    }))
    project_id = _project(app.comic_projects)
    with pytest.raises(ToolError, match="公开决策"):
        _execute(app, project_id, "fast")
    assert app.comic_projects.director_versions(project_id) == []


def test_completed_resume_returns_original_result_without_model(
    app: web_studio.StudioApplication, model: list[str],
) -> None:
    project_id = _project(app.comic_projects)
    first = _execute(app, project_id, "fast")
    model.clear()
    recovered = _execute(app, project_id, "fast", resume_run_id=first["run_id"])
    assert recovered == first
    assert model == []
    assert len(app.runtime_store.list_runs()) == 1


def test_resume_rejects_changed_brief_before_any_model_call(
    app: web_studio.StudioApplication, model: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _project(app.comic_projects)
    real = app._comic_director_model

    def broken(messages: list[dict[str, str]]) -> str:
        if '"title": "DirectorPlan"' in messages[0]["content"]:
            raise ToolError("模拟失败")
        return real(messages)

    monkeypatch.setattr(app, "_comic_director_model", broken)
    with pytest.raises(ToolError):
        _execute(app, project_id, "fast")
    failed_id = app.runtime_store.list_runs()[0].id
    app.comic_projects.replace_brief(project_id, CreativeBriefUpdate(
        expected_version=1, original_request="晴天东方仙侠少女", hard_constraints=["晴天"],
    ))
    model.clear()
    monkeypatch.setattr(app, "_comic_director_model", real)
    with pytest.raises(ToolError, match="输入版本"):
        _execute(app, project_id, "fast", resume_run_id=failed_id)
    assert model == []
    assert len(app.runtime_store.list_runs()) == 1


@pytest.mark.parametrize("mode", ["fast", "professional"])
def test_mode_director_enters_existing_prompt_compiler_without_image_call(
    app: web_studio.StudioApplication, model: list[str], monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(web_studio.get_settings().image, "model", "qwen-image-3.0")
    project_id = _project(app.comic_projects)
    director = _execute(app, project_id, mode)["director_spec"]
    storyboard = app.create_comic_storyboard(project_id, {
        "expected_project_version": 2, "draft": {"title": "雨夜迎战"},
    })["storyboard"]
    shot = app.create_comic_shot(storyboard["storyboard_id"], {
        "expected_project_version": 3, "expected_storyboard_version": 1,
        "shot": {"purpose": "雨夜拔剑", "subject": "东方仙侠少女", "action": "拔剑战斗"},
    })
    monkeypatch.setattr(app, "_comic_storyboard_model", lambda _: json.dumps({
        "director_summary": "人物在雨夜独自迎战",
        "positive_prompt": "东方仙侠 少女 雨夜 战斗，用人物和环境空间关系表现抉择",
        "negative_prompt": "避免偏离用户约束",
    }))
    prompt = app.compile_comic_prompt(shot["shot_id"], {
        "expected_project_version": 4, "expected_shot_version": 1,
    })
    assert prompt["director_spec_version"] == director["version"]
    assert prompt["shot_id"] == shot["shot_id"]
    artifact = app.runtime_store.get_artifact(prompt["artifact_id"])
    assert artifact.type.value == "prompt"
    assert all(item.type.value != "image" for item in app.runtime_store.list_artifacts())
