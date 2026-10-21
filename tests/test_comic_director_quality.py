"""Module 1.5：离线验证真实应用契约，不以替身响应冒充线上艺术质量。"""

from __future__ import annotations

import copy
import json

import pytest
from test_comic_creation_mode import _execute
from test_comic_creation_mode import app as app_fixture
from test_comic_director_coordinator import SKILLS, _parts

from kantoku.config import ToolError
from kantoku.core.runtime.models import ExecutionStatus
from kantoku.domains.comic.cinematography import require_execution_choices
from kantoku.domains.comic.critic import DirectorCriticEngine, critic_spec_context, director_hash
from kantoku.domains.comic.director import execute_director_stage
from kantoku.domains.comic.director_provenance import stage_provenance, value_hash
from kantoku.domains.comic.models import (
    CinematographyPlan,
    ComicAssetDraft,
    ComicProjectInput,
    DirectorPlan,
    DirectorSpecDraft,
)

app = app_fixture
REQUEST = "东方修仙少女夜晚站山巅"
DECISIONS = {
    "shot_size": "全景", "camera_angle": "低角度仰拍",
    "light_direction": "人物左后方", "color_relationship": "冷蓝月色与灰白服饰组成冷色方案",
}


def _responses():
    parts = _parts()
    parts[SKILLS[0]]["creative_decision"].update({
        "intent_summary": REQUEST, "narrative_context": "山巅上人物面对夜晚群山",
        "hard_constraints": [], "emotional_target": "沉静坚定",
        "audience_experience": "体会山势与人物选择的关系", "narrative_focus": "人和山巅",
    })
    parts[SKILLS[1]]["director_plan"].update({
        "visual_strategy": "人物立于山巅，中景岩石衔接远方群山",
        "visual_focus": "白衣少女在山巅站立",
        "subject_environment_relation": "少女与连绵群山形成尺度对比",
        "composition_strategy": "人物位于左三分线，右侧保留山谷空间",
        "color_strategy": DECISIONS["color_relationship"],
        "style_boundary": "电影级东方幻想写实质感，岩石与布料材质清晰",
        "character_presence": "东方修仙少女，黑色长发，白色古风长裙，玉簪",
        "character_pose": "站立远望，身体朝向山谷，双手自然下垂",
        "creative_choices": ["山谷负空间使站立动作成为视觉焦点"],
    })
    parts[SKILLS[2]]["cinematography"].update({
        **DECISIONS, "camera_distance": "人物正前方六米",
        "camera_language": "固定低角度全景，呈现人物和山势",
        "light_source": "月亮作为唯一主光源", "lighting": "月光照亮衣褶和岩面",
        "spatial_feel": "近处山石与远方山谷分离", "depth_strategy": "岩面、人物、群山三层",
        "material_language": "布料轻柔，岩面粗糙", "movement": None,
    })
    return parts


def _generate(
    app, monkeypatch, *, ambiguity=None, resolve=True, critic_fail=False, interaction="guided",
    director_fields=None, critic_findings=None, brief_text=REQUEST,
):
    project_id = app.comic_projects.create(ComicProjectInput.model_validate({
        "title": "导演质量契约", "brief": {"original_request": brief_text},
    })).project.project_id
    conversation = app.create_conversation({"interaction_mode": interaction, "domain": "comic"})
    parts = _responses()
    if brief_text != REQUEST:
        parts[SKILLS[0]]["creative_decision"].update(
            intent_summary=brief_text, narrative_context=brief_text)
    parts[SKILLS[1]]["director_plan"].update(director_fields or {})
    calls = []
    camera_calls = 0

    def model(messages):
        nonlocal camera_calls
        calls.append(copy.deepcopy(messages))
        for skill_id, title in zip(SKILLS[:3], (
            "CreativeDecision", "DirectorPlan", "CinematographyPlan",
        ), strict=True):
            if f'"title": "{title}"' in messages[0]["content"]:
                data = copy.deepcopy(next(iter(parts[skill_id].values())))
                if skill_id == SKILLS[2]:
                    camera_calls += 1
                    if ambiguity and (camera_calls == 1 or not resolve):
                        data.update(ambiguity)
                return json.dumps(data, ensure_ascii=False)
        if critic_fail:
            return json.dumps({"public_summary": "缺少 confidence", "findings": [],
                               "suggested_patches": []}, ensure_ascii=False)
        return json.dumps({"public_summary": "符合当前输入", "confidence": 0.9,
                           "findings": critic_findings or [], "suggested_patches": []},
                          ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", model)
    result = _execute(app, project_id, "professional", task=brief_text,
                      conversation_id=conversation["id"])
    return project_id, result, calls


def _draft(spec):
    return {key: value for key, value in spec.items() if key in DirectorSpecDraft.model_fields}


def test_one_final_decision_and_model_choices_are_bound_to_real_stage_requests(app, monkeypatch):
    project_id, result, calls = _generate(app, monkeypatch)
    assert len(calls) == 4 and result["status"] == "completed"
    spec = app.comic_projects.get_director(project_id)
    assert spec.execution_policy == "single_image"
    assert spec.cinematography.status == "complete" and not spec.cinematography.unresolved_decisions
    assert spec.knowledge_refs
    run = app.runtime_store.get_run(result["run_id"])
    camera_debug = run.state["director_debug"]["stages"][SKILLS[2]]
    for field, chosen in DECISIONS.items():
        assert getattr(spec.cinematography, field) == chosen
        provenance = spec.field_provenance[f"cinematography.{field}"]
        assert provenance.source_type == "model_choice"
        assert provenance.trust_status == "creative_choice"
        assert provenance.value_sha256 == value_hash(chosen)
        assert {e.reference for e in provenance.evidence if e.source_type == "model_choice"} == set(
            camera_debug["model_request_ids"])
        method = next(e for e in provenance.evidence if e.source_type == "skill_method")
        assert method.reference == SKILLS[2]
        assert method.sha256 == camera_debug["knowledge"]["sha256"]
    story = spec.field_provenance["creative_decision.intent_summary"]
    assert story.source_type == "user_fact" and story.trust_status == "confirmed_fact"
    assert story.evidence[0].reference == app.comic_projects.get(project_id).creative_brief.brief_id
    assert not result["ready_for_prompt"]  # 保持真实的用户确认门。
    assert "_model_requests" not in spec.model_dump_json()


def test_projected_camera_summary_keeps_component_provenance(app, monkeypatch):
    project_id, _result, _calls = _generate(
        app, monkeypatch, ambiguity={"camera_language": None},
    )
    spec = app.comic_projects.get_director(project_id)
    source = spec.field_provenance["cinematography.camera_language"]
    assert source.value_sha256 == value_hash(spec.cinematography.camera_language)
    assert source.source_type == "model_choice" and source.trust_status == "creative_choice"
    assert source.derived_from == (
        "cinematography.shot_size,cinematography.camera_angle,cinematography.spatial_feel"
    )
    assert spec.field_provenance["camera_language"].derived_from == "cinematography.camera_language"
    assert "cinematography.camera_language" in critic_spec_context(spec)["field_provenance"]
    draft = _draft(spec.model_dump(mode="json"))
    draft["cinematography"]["camera_angle"] = "平视"
    draft.pop("field_provenance")  # 现有 UI 仍不发送新元数据。
    saved = app.create_comic_director(project_id, {
        "expected_project_version": 2, "expected_director_version": 1, "draft": draft,
    })
    assert "平视" in saved["camera_language"] and "低角度" not in saved["camera_language"]
    assert saved["field_provenance"]["cinematography.camera_language"]["derived_from"] == (
        "cinematography.shot_size,cinematography.camera_angle,cinematography.spatial_feel"
    )


@pytest.mark.parametrize("ambiguity", [
    {"shot_size": "全景或特写"}, {"camera_angle": "低角度或者平视"},
    {"light_direction": "人物左侧或右侧"}, {"color_relationship": "冷色或者暖色"},
])
def test_multiple_candidates_receive_only_one_decision_refinement(app, monkeypatch, ambiguity):
    _project_id, result, calls = _generate(app, monkeypatch, ambiguity=ambiguity)
    camera_calls = [messages for messages in calls
                    if '"title": "CinematographyPlan"' in messages[0]["content"]]
    assert len(camera_calls) == 2 and result["status"] == "completed"
    assert not result["director_spec"]["cinematography"]["unresolved_decisions"]
    assert "仅收敛" in camera_calls[1][-1]["content"]
    for field in ambiguity:
        assert result["director_spec"]["cinematography"][field] == DECISIONS[field]


@pytest.mark.parametrize("missing", ["light_direction", "camera_distance", "material_language"])
def test_missing_camera_parameters_are_completed_by_director_before_review(
    app, monkeypatch, missing,
):
    _project_id, result, calls = _generate(app, monkeypatch, ambiguity={missing: None})
    camera_calls = [messages for messages in calls
                    if '"title": "CinematographyPlan"' in messages[0]["content"]]
    assert result["status"] == "completed" and len(camera_calls) == 2
    assert missing in camera_calls[1][-1]["content"]
    assert result["director_spec"]["cinematography"][missing]
    assert result["director_spec"]["field_provenance"][f"cinematography.{missing}"][
        "source_type"] == "model_choice"


def test_generated_quality_issue_returns_to_director_and_is_reviewed_again(app, monkeypatch):
    parts = _responses()
    calls = []
    reviews = 0

    def model(messages):
        nonlocal reviews
        payload = json.loads(messages[1]["content"])
        for skill, title in zip(SKILLS[:3], ("CreativeDecision", "DirectorPlan",
                                            "CinematographyPlan"), strict=True):
            if f'"title": "{title}"' in messages[0]["content"]:
                calls.append(skill)
                data = copy.deepcopy(next(iter(parts[skill].values())))
                if payload["context"].get("generation_feedback") and skill == SKILLS[1]:
                    assert payload["context"]["generation_feedback"][0]["level"] == "blocking"
                    assert "Director Skill Context" in messages[0]["content"]
                    data["composition_strategy"] = "人物位于右三分线，左侧山谷形成清楚空间关系"
                return json.dumps(data, ensure_ascii=False)
        calls.append(SKILLS[3])
        reviews += 1
        findings = [] if reviews > 1 else [{
            "code": "VISUAL_CAUSALITY_MISMATCH", "severity": "error",
            "field_path": "director_plan.composition_strategy",
            "evidence": payload["director_spec"]["director_plan"]["composition_strategy"],
            "expected": "明确人物位置与山谷空间关系", "suggested_action": "补齐构图中的人物位置",
        }]
        return json.dumps({"public_summary": "真实质量检查", "confidence": 0.9,
                           "findings": findings, "suggested_patches": []}, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", model)
    project = app.comic_projects.create(ComicProjectInput(
        title="质量修正", brief={"original_request": REQUEST}))
    cid = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})["id"]
    result = _execute(app, project.project.project_id, "professional", task=REQUEST,
                      conversation_id=cid)
    assert result["status"] == "completed" and reviews == 2
    assert calls == [*SKILLS[:4], SKILLS[1], SKILLS[2], SKILLS[3]]
    assert "右三分线" in result["director_spec"]["director_plan"]["composition_strategy"]
    assert result["director_execution_summary"]["critic_feedback"] == []
    run = app.runtime_store.get_run(result["run_id"])
    assert run.state["generation_repair_count"] == 1 and run.state["revision_count"] == 0


def test_user_world_conflict_is_explained_without_auto_rewriting_user_edit(app, monkeypatch):
    project_id, first, calls = _generate(app, monkeypatch)
    draft = _draft(first["director_spec"])
    conflict = "改成现代城市街道，但是保持仙侠山巅为同一地点"
    draft["director_plan"]["subject_environment_relation"] = conflict
    edited = app.create_comic_director(project_id, {
        "expected_project_version": 2, "expected_director_version": 1, "draft": draft})

    def critic(messages):
        assert '"title": "SemanticReview"' in messages[0]["content"]
        return json.dumps({"public_summary": "地点设定存在冲突", "confidence": 0.9,
                           "findings": [{"code": "WORLD_CONFLICT", "severity": "error",
                                         "field_path": "director_plan.subject_environment_relation",
                                         "evidence": conflict,
                                         "expected": "同一镜头不能同时把城市街道和山巅作为同一地点",
                                         "suggested_action": (
                                             "保留仙侠山巅或选择城市街道并调整世界设定") }],
                           "suggested_patches": []}, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", critic)
    result = _execute(app, project_id, "professional", review_current=True,
                      expected_director_version=edited["version"])
    assert result["status"] == "waiting"
    feedback = result["director_spec"]["critic_feedback"][0]
    assert feedback["level"] == "blocking"
    assert "同一地点" in feedback["reason"] and "相互矛盾" in feedback["impact"]
    assert "仙侠山巅" in feedback["suggestion"] and "WORLD_CONFLICT" not in json.dumps(feedback)
    assert result["director_spec"]["director_plan"]["subject_environment_relation"] == conflict
    assert not app.runtime_store.get_run(result["run_id"]).state.get("generation_repair_count")


def test_still_ambiguous_is_retained_as_unconfirmable_draft(
    app, monkeypatch,
):
    project_id, result, calls = _generate(
        app, monkeypatch, ambiguity={"camera_angle": "低角度或者平视"}, resolve=False,
    )
    run = app.runtime_store.get_run(result["run_id"])
    assert run.state["director_candidate"]["cinematography"]["camera_angle"] == "低角度或者平视"
    assert run.state["field_provenance"]["cinematography.camera_angle"][
        "trust_status"] == "unresolved"
    assert result["status"] == "waiting" and len(calls) == 6
    assert run.status is ExecutionStatus.WAITING and run.state["generation_repair_count"] == 1
    assert result["director_spec"]["draft"]
    assert not result["director_spec"]["validation"]["can_confirm"]
    assert result["director_execution_summary"]["critic_feedback"] == []
    assert result["director_execution_summary"]["stages"][0]["output"]
    assert app.comic_projects.get(project_id).project.director_id


def test_manual_edit_ignores_forged_sources_and_survives_review_and_restore(app, monkeypatch):
    project_id, first, _calls = _generate(app, monkeypatch)
    original = app.comic_projects.get_director(project_id)
    draft = _draft(first["director_spec"])
    draft.pop("execution_policy")  # 现有 UI 不发送新元数据，服务端继承且不能绕过。
    draft["cinematography"]["camera_angle"] = "平视"
    forged = draft["field_provenance"]["cinematography.camera_angle"]
    forged.update(source_type="user_fact", trust_status="confirmed_fact",
                  value_sha256=value_hash("平视"))
    saved = app.create_comic_director(project_id, {
        "expected_project_version": 2, "expected_director_version": 1, "draft": draft,
    })
    provenance = saved["field_provenance"]["cinematography.camera_angle"]
    assert provenance["source_type"] == "manual_edit"
    assert provenance["trust_status"] == "creative_choice"
    assert provenance["previous_source"] == "model_choice"
    assert saved["execution_policy"] == "single_image"
    assert saved["field_provenance"]["cinematography.shot_size"] == first["director_spec"][
        "field_provenance"]["cinematography.shot_size"]
    reviewed = _execute(app, project_id, "professional", review_current=True,
                        expected_director_version=2)
    assert reviewed["director_spec"]["field_provenance"]["cinematography.camera_angle"] == (
        provenance
    )
    restored = app.restore_comic_director(project_id, {"version": 1, "expected_project_version": 4})
    assert restored["field_provenance"] == original.model_dump(mode="json")["field_provenance"]
    assert director_hash(app.comic_projects.get_director(project_id)) == director_hash(original)


def test_instruction_edit_is_model_choice_and_never_a_confirmed_user_fact(app, monkeypatch):
    project_id, first, _calls = _generate(app, monkeypatch)

    def revision(messages):
        draft = json.loads(messages[1]["content"])["current_draft"]
        draft["cinematography"]["camera_angle"] = "平视"
        draft["field_provenance"] = {}  # 模型自报来源不影响服务端比较。
        return json.dumps(draft, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", revision)
    saved = app.create_comic_director(project_id, {
        "expected_project_version": 2, "expected_director_version": 1,
        "revision_instruction": "改为平视", "draft": None,
    })
    item = saved["field_provenance"]["cinematography.camera_angle"]
    assert item["source_type"] == "model_choice" and item["trust_status"] == "creative_choice"
    assert item["evidence"][0]["reference"].startswith("director-request-")
    assert saved["field_provenance"]["cinematography.shot_size"] == first["director_spec"][
        "field_provenance"]["cinematography.shot_size"]


def test_critic_context_has_one_copy_of_decisions_and_deduplicated_sources(app, monkeypatch):
    project_id, _result, calls = _generate(app, monkeypatch)
    spec = app.comic_projects.get_director(project_id)
    spec.cinematography.public_decision = json.dumps(
        spec.cinematography.model_dump(), ensure_ascii=False,
    )
    compact = critic_spec_context(spec)
    assert "visual_direction" not in compact and "composition" not in compact
    assert "public_decision" not in compact["cinematography"]
    assert compact["creative_decision"]["hard_constraints"] == spec.constraints
    source_id = compact["field_provenance"]["cinematography.camera_angle"]
    assert source_id in compact["provenance_sources"]
    source = compact["provenance_sources"][source_id]
    assert source["source_type"] == "model_choice"
    assert any(e["source_type"] == "skill_method" for e in source["evidence"])
    full = spec.model_dump_json()
    assert len(json.dumps(compact, ensure_ascii=False)) < len(full) / 2
    critic_payload = json.loads(calls[-1][1]["content"])
    assert "creative_brief" in critic_payload and "relevant_assets" in critic_payload


def test_missing_critic_structure_preserves_candidate_but_blocks_confirmation(app, monkeypatch):
    project_id, result, calls = _generate(app, monkeypatch, critic_fail=True)
    spec = app.comic_projects.get_director(project_id)
    assert len(calls) == 5 and result["status"] == "waiting"
    assert spec.critic_status == "unavailable" and spec.critic_result is None
    assert spec.cinematography.status == "complete" and spec.field_provenance
    assert app.runtime_store.get_run(result["run_id"]).status is ExecutionStatus.WAITING
    assert app.runtime_store.get_run(result["run_id"]).state["generation_incomplete"] is True
    with pytest.raises(ToolError, match="内部验证"):
        app.confirm_comic_director(project_id, {
            "version": spec.version, "expected_project_version": 2,
        })
    assert not app.comic_projects.human_director_confirmed(
        app.comic_projects.get_director(project_id))


def test_assets_are_facts_only_for_exact_corresponding_fields(app):
    snapshot = app.comic_projects.create(ComicProjectInput.model_validate({
        "title": REQUEST, "brief": {"original_request": REQUEST},
    }))
    asset = app.comic_assets.create(snapshot.project.project_id, ComicAssetDraft.model_validate({
        "name": "风格设定", "details": {"kind": "style", "art_direction": "电影感",
        "color_language": "冷蓝月光与银白衣饰", "materials": "布料", "camera_language": "全景"},
    }), expected_project_version=1)
    provenance = stage_provenance(
        "director_plan", {"color_strategy": "冷蓝月光与银白衣饰", "visual_focus": "银白色发带"},
        snapshot=snapshot, assets=[asset], knowledge=None,
        model_requests=["director-request-test"], run_id="run-test",
    )
    assert provenance["director_plan.color_strategy"].source_type == "asset_fact"
    assert provenance["director_plan.color_strategy"].trust_status == "confirmed_fact"
    assert provenance["director_plan.color_strategy"].evidence[0].reference == asset.asset_id
    assert provenance["director_plan.visual_focus"].source_type == "model_choice"


def test_autonomous_homepage_keeps_existing_input_and_decision_policy(app, monkeypatch):
    _project_id, result, calls = _generate(
        app, monkeypatch, ambiguity={"camera_angle": "低角度或者平视"}, interaction="autonomous",
    )
    assert result["status"] == "completed" and len(calls) == 4
    assert result["director_spec"]["execution_policy"] is None
    assert result["director_spec"]["field_provenance"] == {}
    assert "可执行的单张图片方案" not in calls[2][0]["content"]


@pytest.mark.parametrize("value", ["全景/近景", "全景至近景", "全景和特写"])
def test_multiple_shot_sizes_cannot_be_marked_complete(value):
    camera = _responses()[SKILLS[2]]["cinematography"]
    camera["shot_size"] = value
    plan = require_execution_choices(CinematographyPlan.model_validate(camera))
    assert plan.status == "needs_revision" and plan.unresolved_decisions == ["shot_size"]


def test_reference_eye_height_does_not_mean_a_second_camera_position():
    camera = _responses()[SKILLS[2]]["cinematography"]
    camera["camera_angle"] = "机位略低于少女视线水平，微仰视角，不夸张低角度"
    plan = require_execution_choices(CinematographyPlan.model_validate(camera))
    assert plan.status == "complete" and plan.unresolved_decisions == []


@pytest.mark.parametrize("value,complete", [("低机位平视", True), ("低机位平视（略仰）", False)])
def test_camera_height_and_view_angle_are_distinct(value, complete):
    camera = _responses()[SKILLS[2]]["cinematography"]
    camera["camera_angle"] = value
    plan = require_execution_choices(CinematographyPlan.model_validate(camera))
    assert (plan.status == "complete") == complete


@pytest.mark.parametrize("value", ["按当前主体和环境关系取景", "视情况选择", "待定"])
def test_undecided_shot_size_is_not_an_execution_plan(value):
    camera = _responses()[SKILLS[2]]["cinematography"]
    camera["shot_size"] = value
    plan = require_execution_choices(CinematographyPlan.model_validate(camera))
    assert plan.status == "needs_revision"


def test_schema_output_does_not_accept_model_declared_provenance():
    plan = _responses()[SKILLS[1]]["director_plan"]
    plan["field_provenance"] = {"camera_angle": "user_fact"}
    with pytest.raises(ToolError):
        execute_director_stage(SKILLS[1], {}, {"context": {}},
                               model_call=lambda _: json.dumps(plan, ensure_ascii=False))


def test_character_pose_is_resolved_once_without_rewriting_facts():
    plan = _responses()[SKILLS[1]]["director_plan"]
    plan["character_pose"] = "双手垂于身侧或轻拢袖中"
    calls = []

    def model(messages):
        calls.append(messages)
        result = dict(plan)
        if len(calls) == 2:
            result["character_pose"] = "双手垂于身侧"
        return json.dumps(result, ensure_ascii=False)

    result = execute_director_stage(SKILLS[1], {}, {
        "context": {}, "execution_plan_required": True,
    }, model_call=model)
    assert len(calls) == 2
    assert result["director_plan"]["character_pose"] == "双手垂于身侧"
    assert result["director_plan"]["visual_focus"] == plan["visual_focus"]
    assert "character_pose" in calls[1][-1]["content"]


def test_negative_exclusions_are_not_unresolved_choices():
    from kantoku.domains.comic.cinematography import PLAN_EXECUTION_FIELDS, unresolved_execution

    plan = _responses()[SKILLS[1]]["director_plan"]
    plan["style_boundary"] = "写实东方插画，不采用夸张喜剧或赛博霓虹"
    plan["character_pose"] = "双手垂于身侧，不奔跑或跳跃"
    plan["subject_environment_relation"] = "少女站在夜晚山巅，画面中无其他人物或载具"
    plan["style_boundary"] += "，不指向任何特定官方设定图或既有版权画面"
    assert unresolved_execution(DirectorPlan.model_validate(plan).model_dump(),
                                PLAN_EXECUTION_FIELDS) == []


def test_real_failure_exclusions_do_not_block_director_confirmation(app, monkeypatch):
    # 真实失败 run 的公开原文：此前被本地审核误判，尚未进入语义审核。
    fields = {
        "subject_environment_relation": (
            "火麟飞独自站在超兽武装世界荒野断崖制高点的岩台上，时间为黄昏低光时刻，"
            "身体处于画面中左位置，面向画面右侧急剧跌落的深谷与层层堆叠的风暴云远空，"
            "脚下只有粗粝裸岩与稀疏枯草，崖缘之外是云海与天光，画面中无其他人或载具。"),
        "style_boundary": (
            "写实电影级动画质感，2.5D渲染与厚涂材质结合，岩石保留硬边与颗粒，"
            "云海使用柔和体积分层，人物造型遵循超兽武装式锐利机械装甲与流畅服装剪裁，"
            "不指向任何特定官方设定图或既有版权画面。"),
    }
    fields.update({
        "composition_strategy": (
            "中景，人物位于画面中央略偏左，确保手部印诀不被任何线条或树叶切过。"),
        "character_expression": "眉目收束，眼神专注沉静，不显露夸张愤怒或痛苦。",
    })
    project_id, result, calls = _generate(app, monkeypatch, director_fields=fields)
    assert result["status"] == "completed" and len(calls) == 4
    run = app.runtime_store.get_run(result["run_id"])
    assert not run.state.get("generation_incomplete")
    assert not run.state.get("generation_repair_count")
    spec = app.comic_projects.get_director(project_id)
    for field, value in fields.items():
        assert getattr(spec.director_plan, field) == value
    assert app.confirm_comic_director(project_id, {
        "version": spec.version, "expected_project_version": 2,
    })["user_confirmed"]


@pytest.mark.parametrize("field,value", [
    ("style_boundary", "无论古风或赛博风均可"),
    ("subject_environment_relation", "人物站在山巅或城市街道，画面中无其他人物或载具"),
    ("camera_angle", "不采用仰拍或俯拍"),
])
def test_exclusions_never_supply_missing_choices_or_hide_positive_alternatives(field, value):
    from kantoku.domains.comic.cinematography import unresolved_execution

    assert unresolved_execution({field: value}, (field,)) == [field]


def test_critic_patch_replaces_source_and_retains_untouched_facts(app, monkeypatch):
    project_id, _first, _calls = _generate(app, monkeypatch)
    original = app.comic_projects.get_director(project_id)
    value = "人物位于右三分线，左侧保留山谷"
    before = original.director_plan.composition_strategy

    def critic(messages):
        return json.dumps({
            "public_summary": "构图可局部调整", "confidence": 0.9,
            "findings": [{"code": "COMPOSITION_CAUSALITY", "severity": "warning",
                          "field_path": "director_plan.composition_strategy", "evidence": before,
                          "expected": value, "suggested_action": "修订构图"}],
            "suggested_patches": [{"field": "director_plan.composition_strategy",
                                   "reason": "明确视觉焦点", "value": value,
                                   "expected_value": before}],
        }, ensure_ascii=False)

    engine = DirectorCriticEngine(critic)
    result = engine.review(original, app.comic_projects.get(project_id).creative_brief)
    revised = engine.apply_patches(original, result)
    provenance = revised.field_provenance["director_plan.composition_strategy"]
    assert provenance.source_type == "model_choice" and provenance.previous_source == "model_choice"
    assert provenance.evidence[0].reference == result.model_request_id
    assert revised.field_provenance["creative_decision.intent_summary"] == (
        original.field_provenance["creative_decision.intent_summary"]
    )
    assert revised.field_provenance["composition"].derived_from == (
        "director_plan.composition_strategy"
    )
