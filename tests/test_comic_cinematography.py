"""摄影公开文字解析、阶段隔离恢复和真实待修订门禁。"""

import hashlib
import json

import pytest
from pydantic import ValidationError
from test_comic_director_coordinator import SKILLS, _parts, _project, _request, _setup
from test_comic_director_skills import _load_registry

from kantoku.config import ToolError
from kantoku.core.runtime.models import ExecutionStatus
from kantoku.core.runtime.store import RuntimeStore
from kantoku.domains.comic.cinematography import parse_cinematography
from kantoku.domains.comic.coordinator import director_execution_summary
from kantoku.domains.comic.critic import (
    DirectorCriticEngine,
    director_hash,
    require_approved_director,
)
from kantoku.domains.comic.director import execute_director_stage
from kantoku.domains.comic.models import CinematographyPlan, ComicProjectInput, DirectorSpecDraft

REQUEST = "中式修仙少女站在树梢看远方村庄"
PROSE = "采用低角度远景，突出人物与环境关系，使用黄昏逆光强化孤独感。"


def test_old_json_and_new_envelope_keep_actual_camera_choices():
    camera = _parts()[SKILLS[2]]["cinematography"]
    old, diagnostics = parse_cinematography(json.dumps(camera, ensure_ascii=False))
    assert old.status == "complete" and not diagnostics["extraction_used"]
    envelope = {"public_decision": {"summary": "人物与村庄形成距离关系"},
                "structured_plan": camera, "creative_reason": "用空间而不是战斗表现观察"}
    new, _diagnostics = parse_cinematography("```json\n" + json.dumps(envelope) + "\n```")
    assert new.shot_size == old.shot_size and new.public_decision == "人物与村庄形成距离关系"
    assert new.creative_reason == envelope["creative_reason"]
    result = _load_registry().execute(SKILLS[2], {"_contract_output": envelope}, {})
    assert result["public_decision"]["summary"] == "人物与村庄形成距离关系"
    assert result["structured_plan"]["shot_size"] == old.shot_size
    assert result["cinematography"]["shot_size"] == old.shot_size  # 兼容字段只是投影。
    empty_notes, _debug = parse_cinematography({
        "public_decision": {}, "structured_plan": camera, "creative_reason": "",
    })
    assert empty_notes.status == "complete" and empty_notes.public_decision is None


def test_public_prose_has_one_semantic_extraction_without_creative_formula():
    calls = []

    def extract(messages):
        calls.append(messages)
        assert messages[1]["content"] == PROSE
        return json.dumps({"shot_size": "远景", "camera_angle": "低角度",
                           "lighting": "黄昏逆光", "lighting_direction": "黄昏逆光",
                           "composition_strategy": "人物与环境关系",
                           "creative_reason": "强化孤独感"})

    plan, debug = parse_cinematography(PROSE, model_call=extract)
    assert len(calls) == 1 and debug["extraction_used"]
    assert plan.status == "needs_revision"
    assert plan.public_decision == PROSE
    assert plan.camera_angle == "低角度" and plan.light_direction == "黄昏逆光"
    assert plan.material_language is None  # 没有默认布料/石面或历史雨夜元素。
    assert debug["raw_output"] == PROSE


def test_invalid_extraction_retains_public_output_without_cot_or_secrets():
    plan, debug = parse_cinematography(
        '<think>private-chain</think>' + PROSE,
        model_call=lambda messages: '{"reasoning":"private-chain","shot_size":',
    )
    assert plan.status == "missing" and plan.public_decision == PROSE
    assert plan.shot_size is None
    assert "private-chain" not in json.dumps(debug, ensure_ascii=False)
    assert debug["extracted_output"] == ""
    with pytest.raises(ValidationError):
        CinematographyPlan(status="complete", shot_size="远景")
    edited = CinematographyPlan(status="missing", shot_size="远景")
    assert edited.status == "needs_revision" and edited.camera_angle is None


@pytest.mark.parametrize("mode", ["fast", "professional"])
@pytest.mark.parametrize("output", ["partial", "unparseable", "transport"])
def test_camera_failure_saves_real_v2_draft_and_can_resume(tmp_path, mode, output):
    coordinator, store, runtime, calls = _setup(tmp_path)
    project_id = _project(store)
    original = coordinator.stage_executor

    def execute(skill, inputs, context):
        if skill == SKILLS[2]:
            if output == "transport":
                raise ToolError("摄影请求网络失败")
            return {"shot_size": "远景"} if output == "partial" else "not a camera contract"
        return original(skill, inputs, context)

    coordinator.stage_executor = execute
    result = coordinator.execute(_request(store, project_id, mode))
    run = runtime.get_run(result.run_id)
    assert run.status is ExecutionStatus.WAITING and result.needs_review
    assert run.state["stage_statuses"][SKILLS[0]] == "completed"
    assert run.state["stage_statuses"][SKILLS[1]] == "completed"
    assert run.state["stage_statuses"][SKILLS[2]] == "needs_revision"
    assert SKILLS[2] not in run.state["completed_stages"]
    spec = store.get_director(project_id)
    assert spec.schema_version == 2 and spec.cinematography.status != "complete"
    assert spec.critic_result.verdict == "needs_revision"
    assert spec.cinematography.material_language is None
    with pytest.raises(ToolError, match="摄影方案待修订"):
        require_approved_director(spec)
    events = [item.payload for item in runtime.list_events(run.id)]
    names = {item["director_event"] for item in events}
    assert {"node_warning", "stage_output_saved", "director_draft_created"} <= names
    assert "cinematography_completed" not in names
    warning = next(item for item in events if item["director_event"] == "stage_output_saved")
    assert warning["trace_id"] == "trace-director-contract" and warning["skill_id"] == SKILLS[2]
    assert warning["missing_fields"]
    assert run.state["stage_failures"][SKILLS[2]]["error_id"]
    reopened = RuntimeStore(runtime.path).get_run(run.id)
    assert director_execution_summary(reopened) == director_execution_summary(run)
    # 显式恢复只重新做未完成的摄影和审核，不重做前两个模型阶段。
    calls.clear()
    coordinator.stage_executor = original
    resumed = coordinator.execute(_request(
        store, project_id, mode, previous_run_id=run.id, rerun_from="cinematography",
    ))
    assert calls == [SKILLS[2]]
    assert runtime.get_run(resumed.run_id).status is ExecutionStatus.COMPLETED
    assert resumed.director_spec.version == 2


@pytest.mark.parametrize("mode", ["fast", "professional"])
def test_current_tree_village_request_gets_real_director_spec_with_parsed_camera(tmp_path, mode):
    coordinator, store, runtime, _calls = _setup(tmp_path)
    project_id = store.create(ComicProjectInput.model_validate({
        "title": REQUEST, "brief": {"original_request": REQUEST,
                                    "hard_constraints": ["少女", "树梢", "村庄"]},
    })).project.project_id
    pieces = _parts()
    pieces[SKILLS[0]]["creative_decision"].update(
        intent_summary=REQUEST, narrative_context="人物远望村庄",
        hard_constraints=["少女", "树梢", "村庄"], narrative_focus="树梢与村庄的距离")
    pieces[SKILLS[1]]["director_plan"].update(
        visual_strategy="以树梢高度连接近处人物与远方村庄", visual_focus="少女远望村庄",
        composition_strategy="枝叶间留出远方聚落", color_strategy="黄昏环境色")
    camera = {name: "原公开方案中的" + name for name in (
        "shot_size", "camera_angle", "camera_distance", "spatial_feel", "lens_or_spatial_feel",
        "lighting", "light_source", "light_direction", "color_relationship", "depth_strategy",
        "material_language")}
    camera.update(shot_size="远景", camera_angle="平视", lighting="黄昏侧光")
    public = "；".join(f"{key}={value}" for key, value in camera.items())
    model_calls = []

    def model(messages):
        model_calls.append(messages)
        return public if len(model_calls) == 1 else json.dumps(camera)

    def execute(skill, inputs, context):
        assert context["context"]["creative_brief"]["original_request"] == REQUEST
        if skill == SKILLS[2]:
            return execute_director_stage(skill, inputs, context, model_call=model)
        return pieces[skill]

    coordinator.stage_executor = execute
    result = coordinator.execute(_request(store, project_id, mode, task=REQUEST))
    assert result.director_spec.cinematography.status == "complete"
    assert runtime.get_run(result.run_id).status is ExecutionStatus.COMPLETED
    assert len(model_calls) == 2
    assert "穷奇" not in result.director_spec.model_dump_json()
    assert "雨夜" not in json.dumps(model_calls, ensure_ascii=False)


def test_complete_legacy_camera_fingerprint_is_unchanged_and_incomplete_cannot_pass():
    spec = DirectorSpecDraft.model_validate(_parts()[SKILLS[4]]["director_spec"])
    payload = spec.model_dump(exclude={"critic_result"})
    payload["cinematography"].pop("status")
    payload["cinematography"].pop("public_decision")
    payload["cinematography"].pop("creative_reason")
    for key in ("style_boundary", "character_expression", "character_pose", "character_presence"):
        payload["director_plan"].pop(key)
    legacy_hash = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    assert director_hash(spec) == legacy_hash
    data = spec.model_dump()
    data["cinematography"] = CinematographyPlan(status="missing").model_dump()
    partial = DirectorSpecDraft.model_validate(data)
    calls = []
    critic = DirectorCriticEngine(lambda messages: calls.append(messages))
    brief = ComicProjectInput.model_validate({"title": REQUEST, "brief": {
        "original_request": REQUEST, "hard_constraints": spec.constraints,
    }}).brief
    result = critic.review(partial, brief)
    assert result.verdict == "needs_revision" and calls == []
    assert result.allowed_patches == []
