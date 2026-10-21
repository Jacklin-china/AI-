"""当前创作会话的上下文锁和公开审核反馈；全部为离线回归。"""

import copy
import json

import pytest
from test_comic_director_quality import SKILLS, _generate, _responses
from test_home_quick_domain import _workspace_production_request
from test_home_quick_domain import app as app_fixture
from test_home_quick_domain import production as production_fixture

from kantoku.config import ToolError
from kantoku.core.runtime.models import ArtifactType
from kantoku.domains.comic.critic import public_critic_feedback
from kantoku.domains.comic.models import (
    ComicPromptContexts,
    ComicPromptDraft,
    CreativeBriefFork,
    DirectorSpecDraft,
)
from kantoku.domains.comic.projects import ComicContextBuilder, ComicProjectStore
from kantoku.domains.comic.prompts import StructuredImagePromptCompiler

app = app_fixture
production = production_fixture


def semantic_context(scopes=("action",), *, new=False):
    """An explicit offline model decision, never a production keyword shortcut."""
    def decide(messages):
        if "edit_scopes" in messages[0]["content"]:
            return json.dumps({"edit_scopes": sorted(scopes)})
        request = json.loads(messages[-1]["content"])["current_user_request"]
        return json.dumps({"new_creative_direction": new, "reason": "离线语义判定",
                           "new_brief": {"original_request": request} if new else None})
    return decide


@pytest.fixture
def creative(app, production, monkeypatch):
    request = _workspace_production_request(
        app, creative_request="东方修仙少女夜晚山巅")
    seen = []

    def model(messages):
        context = json.loads(messages[1]["content"])
        seen.append(context)
        if "negative_prompt" not in messages[0]["content"]:
            return json.dumps({"title": "当前画面", "description": "角色与环境",
                               "shots": [{"purpose": "表现角色", "subject": "东方修仙少女阿青",
                                          "action": "站立远望", "environment": "夜晚山巅"}]},
                              ensure_ascii=False)
        parts = {
            "character_context": {"identity": "东方修仙少女阿青", "appearance": "黑色长发",
                                  "age": "18岁", "clothing": "白色古风长裙",
                                  "fixed_features": "玉簪"},
            "world_context": {"world_setting": "仙侠世界", "location": "山巅岩台",
                              "time": "夜晚", "environment": "云海与远山"},
            "style_context": {"art_style": "电影级东方幻想",
                              "photographic_style": "真实摄影质感", "color": "蓝灰冷色",
                              "materials": "丝绸与岩石"},
            "shot_context": {"action": context["current_shot"]["action"], "pose": "持剑姿态",
                             "composition": "人物位于左侧三分之一", "camera": "远景、低机位",
                             "lighting_changes": "月光从右上方照射"},
        }
        # 验证恶意/漂移的后续模型输出不能覆盖锁；使用原 Compiler 支持的契约。
        if context.get("reused_bibles"):
            parts["character_context"]["identity"] = "全新的女性战士"
        if "规定的对象" not in messages[0]["content"]:
            parts = {field: "；".join(body.values()) for field, body in parts.items()}
        return json.dumps({**parts, "director_summary": "沿用已确认导演方案",
                           "negative_prompt": "现代建筑、额外人物、文字水印"}, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_storyboard_model", model)
    return request, seen


def compile_first(app, request):
    project_id = request["production_project_id"]
    board = app.create_comic_storyboard(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "generate": True,
    })
    shot = board["shots"][0]
    return app.compile_comic_prompt(shot["shot_id"], {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_shot_version": shot["version"], "complete_prompt": True,
        "image_mode": "external", "conversation_id": request["conversation_id"],
    })


@pytest.mark.parametrize("instruction,section,changed", [
    ("把天空改成血红色", "world_context", {"environment"}),
    ("换成夜景", "world_context", {"environment", "time"}),
    ("把天空改成血红色", "style_context", {"color"}),
])
def test_partial_prompt_edit_cannot_rewrite_unedited_bible_fields(
    app, creative, instruction, section, changed,
):
    request, _ = creative
    first = compile_first(app, request)
    source = app.comic_prompts.source(first["shot_id"])
    snapshot, director, board, shot, assets = source
    lock = app.comic_projects.creative_context_lock(shot.project_id, request["conversation_id"])
    bibles = app.comic_projects.locked_bibles(shot.project_id, lock)
    reused = ComicContextBuilder.retained_prompt_fields(
        bibles, instruction, ComicContextBuilder.edit_scopes(instruction))
    original = ComicPromptContexts.from_sections({name: first[name] for name in
                                                  ComicPromptContexts.model_fields})
    output = original.model_dump()
    for field in output[section]:
        output[section][field] = "模型的新设定"
    output.update(director_summary="局部修改", negative_prompt=first["negative_prompt"])

    def model(messages):
        assert json.loads(messages[1]["content"])["locked_context_fields"]
        return json.dumps(output, ensure_ascii=False)

    draft = StructuredImagePromptCompiler("external").compile(
        snapshot=snapshot, director=director, storyboard=board, shot=shot, assets=assets,
        model_target="external", model_call=model, complete_prompt=True, reused_context=reused,
        allow_advisory=True)
    actual = ComicPromptContexts.model_fields[section].annotation.from_text(getattr(draft, section))
    for field in output[section]:
        if field in changed:
            assert "模型的新设定" in getattr(actual, field)
        else:
            assert getattr(actual, field) == getattr(getattr(original, section), field)
            assert draft.context_sources["retained_fields"][f"{section}.{field}"][
                "source_artifact_id"] == first["artifact_id"]


def test_action_only_followup_keeps_identity_world_style_and_full_prompt(app, production, creative):
    request, seen = creative
    project_id, cid = request["production_project_id"], request["conversation_id"]
    lock = app.comic_projects.creative_context_lock(project_id, cid)
    assert lock and lock["director_version"] == request["director_version"]
    first = compile_first(app, request)
    before = app.comic_projects.get_director(project_id)
    director_calls = list(production)
    result = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "conversation_id": cid, "creation_mode": "professional", "creative_operation": "auto",
        "task": "手持长剑战斗",
    })
    continued = result["shot_continuation"]
    prompt = continued["prompt"]
    for field in ("character_context", "world_context", "style_context"):
        assert prompt[field] == first[field]
        assert first[field] in prompt["positive_prompt"]
    assert "手持长剑战斗" in prompt["shot_context"]
    assert "全新的女性战士" not in prompt["positive_prompt"]
    assert continued["shot"]["subject"] == "东方修仙少女阿青"
    assert continued["shot"]["environment"] == "夜晚山巅"
    assert app.comic_projects.get_director(project_id) == before
    assert (app.comic_projects.get(project_id).creative_brief.original_request
            == "东方修仙少女夜晚山巅")
    assert production == [*director_calls, "comic.director_critic"]
    # 纯动作续接继承既有设定，不属于用户改写提示词：审核仍真实执行（见上方调用序列），
    # 建议类结论作为内部校验不外显，只有阻断级问题才对用户可见。
    assert result["generation_mode"] == "USER_EDIT" and result["review_required"]
    assert all(item["level"] == "blocking"
               for item in result["director_spec"]["critic_feedback"])
    assert result["context_diff"]["instruction"] == "手持长剑战斗"
    assert any("creative_context_lock" in item.get("stable_context", {}) for item in seen)
    assert prompt["media_preparation"]["status"] == "awaiting_external_image"
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)
    assert not any(run.workflow == "comic.production.v1" for run in app.runtime_store.list_runs())
    assert all(item.request["kind"] == "comic.director.confirmation"
               for item in app.runtime_store.list_approvals())
    messages = app.runtime_store.list_conversation_messages(cid)
    assert any(item.content == "手持长剑战斗" for item in messages)
    assert next(item for item in messages if "等待外部图片" in item.content).artifact_id is None
    reopened = ComicProjectStore(app.comic_projects.path)
    persisted = reopened.creative_context_lock(project_id, cid)
    assert reopened.locked_bibles(project_id, persisted)["character_context"] == first[
        "character_context"]


def test_followup_before_first_prompt_uses_confirmed_director(app, creative):
    request, _seen = creative
    project_id = request["production_project_id"]
    result = app.create_comic_director(project_id, {
        "expected_project_version": request["expected_project_version"],
        "creation_mode": "professional", "conversation_id": request["conversation_id"],
        "task": "手持长剑战斗",
    })
    prompt = result["shot_continuation"]["prompt"]
    assert "东方修仙少女阿青" in prompt["character_context"]
    assert "夜晚" in prompt["world_context"]
    assert "电影级东方幻想" in prompt["style_context"]
    assert "手持长剑战斗" in prompt["shot_context"]


@pytest.mark.parametrize("brief,character,world,action", [
    ("东方修仙少女夜晚山巅", "东方修仙少女，黑色长发，白裙玉簪", "夜晚山巅、仙侠世界",
     "她站在山顶，背对镜头"),
    ("电影级超兽武装火麟飞站在悬崖上看着远方", "火麟飞，红色战斗服，深红短发",
     "暮色悬崖、超兽武装世界", "他摆出火云诀的起手式"),
])
def test_unconfirmed_action_continuation_retains_saved_design_and_confirmation_gate(
    app, monkeypatch, brief, character, world, action,
):
    # 首次方案包含真实语义契约的可选建议，仍应完整可查看和确认。
    warning = {"severity": "warning", "code": "COMPOSITION_CAUSALITY",
               "field_path": "director_plan.composition_strategy",
               "evidence": "人物位于左三分线", "expected": "可进一步突出人物",
               "suggested_action": "可加强主体分离"}
    project_id, first, _ = _generate(app, monkeypatch, brief_text=brief,
                                    director_fields={"character_presence": character,
                                                     "subject_environment_relation": world},
                                    critic_findings=[warning])
    assert first["status"] == "completed" and not first["ready_for_prompt"]
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    before = app.comic_projects.get_director(project_id)
    assert before.critic_result.verdict == "needs_revision"
    assert not app.comic_projects.human_director_confirmed(before)
    parts, seen = _responses(), []
    parts[SKILLS[0]]["creative_decision"].update(
        intent_summary=f"{character}：{action}", emotional_target="本轮动作的专注感")
    parts[SKILLS[1]]["director_plan"].update(
        character_pose=action, visual_focus="当前人物的动作",
        character_presence="新的女性战士", subject_environment_relation="现代城市街道",
        style_boundary="卡通", color_strategy="暖色")
    parts[SKILLS[2]]["cinematography"]["color_relationship"] = "红色霓虹与绿色背景"

    def model(messages):
        seen.append(copy.deepcopy(messages))
        for skill, title in zip(SKILLS[:3],
                               ("CreativeDecision", "DirectorPlan", "CinematographyPlan"),
                               strict=True):
            if f'"title": "{title}"' in messages[0]["content"]:
                return json.dumps(next(iter(parts[skill].values())), ensure_ascii=False)
        return json.dumps({"public_summary": "可执行", "confidence": 0.9,
                           "findings": [], "suggested_patches": []})

    monkeypatch.setattr(app, "_comic_director_model", model)
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context())
    result = app.create_comic_director(project_id, {
        "expected_project_version": 2, "creation_mode": "professional", "conversation_id": cid,
        "task": action, "creative_operation": "auto",
    })
    assert result["status"] == "completed" and len(seen) == 4
    assert not result["ready_for_prompt"] and not result["director_spec"]["user_confirmed"]
    after = app.comic_projects.get_director(project_id)
    assert after.creative_brief_version == before.creative_brief_version
    assert app.comic_projects.get(project_id).creative_brief.original_request == brief
    assert after.creative_decision.narrative_context == (
        parts[SKILLS[0]]["creative_decision"]["narrative_context"])
    assert action in after.creative_decision.intent_summary
    assert after.creative_decision.emotional_target == "本轮动作的专注感"
    for field in ("character_presence", "subject_environment_relation", "style_boundary"):
        assert getattr(after.director_plan, field) == getattr(before.director_plan, field)
        assert after.field_provenance[f"director_plan.{field}"].trust_status == "creative_choice"
    assert after.director_plan.character_pose == action
    assert after.cinematography.color_relationship == "红色霓虹与绿色背景"
    assert json.loads(seen[-1][1]["content"])["current_shot_instruction"] == action
    for call in seen[:2]:
        lock = json.loads(call[1]["content"])["context"]["creative_context_lock"]
        assert lock["bibles"]["character_context"] == character
        assert lock["bibles"]["world_context"] == world
        assert lock["director_confirmed"] is False
        assert lock["director_version"] == before.version
        assert lock["current_director_decision"] is None
    assert app._comic_director_result(app.runtime_store.get_run(first["run_id"]))[
        "director_spec"]["version"] == before.version
    with pytest.raises(ToolError, match="确认"):
        app.create_comic_storyboard(project_id, {
            "expected_project_version": 3, "generate": True})
    assert app.confirm_comic_director(project_id, {
        "expected_project_version": 3, "version": after.version})["user_confirmed"]


def test_failed_unconfirmed_followup_does_not_invalidate_previous_director(app, monkeypatch):
    project_id, first, _ = _generate(app, monkeypatch)
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    before = app.comic_projects.get_director(project_id)
    def fail(_messages):
        raise ToolError("文本服务暂不可用")
    monkeypatch.setattr(app, "_comic_director_model", fail)
    with pytest.raises(ToolError, match="文本服务"):
        app.create_comic_director(project_id, {
            "expected_project_version": 2, "creation_mode": "professional",
            "conversation_id": cid, "task": "她背对镜头"})
    assert app.comic_projects.get_director(project_id) == before
    assert app.get_comic_director(project_id)["version"] == before.version
    assert app.comic_projects.get(project_id).creative_brief.version == 1
    assert app._comic_director_result(app.runtime_store.get_run(first["run_id"]))[
        "director_spec"]["spec_id"] == before.spec_id


def test_school_portrait_followups_get_fresh_intent_expression_and_durable_memory(app, monkeypatch):
    brief = "给我生成一张来自韶关实验中学卿辉的超写实照片"
    project_id, first, _ = _generate(app, monkeypatch, brief_text=brief, director_fields={
        "character_presence": "卿辉，黑色短发，蓝白校服，学生",
        "subject_environment_relation": "韶关实验中学教室，课桌边",
        "style_boundary": "超写实日常摄影", "character_expression": "平静自然",
    })
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    original = app.comic_projects.get_director(project_id)
    followups = ["他拿着笔放在嘴里，好像在烦恼昨天的考试",
                 "他把笔放下，露出释然的笑容"]
    calls = []

    def model(messages):
        payload = json.loads(messages[1]["content"])
        parts = _responses()
        for skill, title in zip(SKILLS[:3], ("CreativeDecision", "DirectorPlan",
                                            "CinematographyPlan"), strict=True):
            if f'"title": "{title}"' not in messages[0]["content"]:
                continue
            context = payload["context"]
            task = context["current_task"]
            calls.append((skill, copy.deepcopy(context)))
            worried = "考试" in task
            parts[SKILLS[0]]["creative_decision"].update(
                intent_summary=f"卿辉在教室中：{task}",
                emotional_target="烦恼" if worried else "释然",
                narrative_context="模型尝试改成另一个世界")
            parts[SKILLS[1]]["director_plan"].update(
                character_presence="模型尝试更换人物", subject_environment_relation="模型的新地点",
                style_boundary="模型的新画风", visual_focus=f"本轮动作：{task}",
                character_pose="笔靠在嘴边，坐在课桌边" if worried else "放下笔，身体放松",
                character_expression="眉头轻皱，眼神担忧" if worried else "释然的微笑")
            return json.dumps(next(iter(parts[skill].values())), ensure_ascii=False)
        return json.dumps({"public_summary": "本轮需求已核验", "confidence": 0.9,
                           "findings": [], "suggested_patches": []}, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", model)
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context())
    results = [first]
    for task in followups:
        results.append(app.create_comic_director(project_id, {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "conversation_id": cid, "creation_mode": "professional", "task": task,
        }))
    assert len({item["run_id"] for item in results}) == 3
    assert len(calls) == 6
    specs = [item["director_spec"] for item in results]
    assert [item["version"] for item in specs] == [1, 2, 3]
    assert "烦恼" in specs[1]["creative_decision"]["intent_summary"]
    assert "担忧" in specs[1]["director_plan"]["character_expression"]
    assert specs[1]["creative_decision"]["emotional_target"] == "烦恼"
    assert specs[2]["creative_decision"]["emotional_target"] == "释然"
    assert specs[2]["director_plan"]["character_pose"] == "放下笔，身体放松"
    for spec in specs[1:]:
        for field in ("character_presence", "subject_environment_relation", "style_boundary"):
            assert spec["director_plan"][field] == getattr(original.director_plan, field)
        assert spec["creative_decision"]["narrative_context"] == "模型尝试改成另一个世界"
        assert spec["cinematography"] == specs[0]["cinematography"]
    reopened = ComicProjectStore(app.comic_projects.path)
    assert reopened.creative_context_lock(project_id, cid)["director_version"] == 3
    assert reopened.get_director(project_id).director_plan.character_expression == "释然的微笑"
    assert app._comic_director_result(app.runtime_store.get_run(first["run_id"]))[
        "director_spec"]["director_plan"]["character_expression"] == "平静自然"
    before_messages = app.runtime_store.list_conversation_messages(cid)
    for _ in range(3):
        current = app.comic_projects.get_director(project_id)
        reviewed = app.create_comic_director(project_id, {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "creation_mode": "professional", "conversation_id": cid,
            "review_current": True, "expected_director_version": current.version,
        })
        assert app.runtime_store.get_run(reviewed["run_id"]).state["task_system"] is True
    assert len(calls) == 6  # Rechecking only runs the Critic, never the creation stages.
    assert app.runtime_store.list_conversation_messages(cid) == before_messages


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("repair_field", [None, "director_plan.composition_strategy",
                                         "cinematography.camera_language"])
def test_chat_turn_after_portrait_owns_new_plan_and_preserves_identity(
    app, monkeypatch, confirmed, repair_field,
):
    project_id, first, _ = _generate(app, monkeypatch,
        brief_text="日本女演员持野蓬的超写实电影感照片", director_fields={
            "character_presence": "持野蓬，成年女性，黑色及肩发，深色上衣、浅色内搭",
            "subject_environment_relation": "室内摄影棚休息区，窗边椅子",
            "style_boundary": "超写实电影摄影", "character_expression": "平静，目光看向画外",
            "character_pose": "坐在椅边", "continuity_rules": ["黑色及肩发、服装保持一致"],
        })
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    if confirmed:
        app.confirm_comic_director(project_id, {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "version": first["director_spec"]["version"],
        })
    original = app.comic_projects.get(project_id)
    original_director = app.comic_projects.get_director(project_id)
    calls = []
    reviews = []

    def model(messages):
        parts = _responses()
        for skill, title in zip(SKILLS[:3], ("CreativeDecision", "DirectorPlan",
                                            "CinematographyPlan"), strict=True):
            if f'"title": "{title}"' not in messages[0]["content"]:
                continue
            context = json.loads(messages[1]["content"])["context"]
            calls.append(context)
            assert "持野蓬" in context["creative_context_lock"]["character"]
            parts[SKILLS[0]]["creative_decision"].update(
                intent_summary="持野蓬在原休息区充满爱意地看向镜头", emotional_target="爱意")
            parts[SKILLS[1]]["director_plan"].update(
                character_presence="错误的新人物", subject_environment_relation="错误的新环境",
                style_boundary="错误的新风格", character_expression="含情脉脉，直视镜头",
                character_pose="坐在椅边，身体朝向镜头", visual_focus="人物爱意的眼神",
                continuity_rules=["保持人物身份和服装，视线朝向镜头"])
            parts[SKILLS[2]]["cinematography"].update(camera_language="平视，视线交于镜头")
            if context.get("generation_feedback"):
                if repair_field == "director_plan.composition_strategy":
                    parts[SKILLS[1]]["director_plan"]["composition_strategy"] = (
                        "人物位于右三分线，朝向镜头，左侧保留窗边环境")
                if repair_field == "cinematography.camera_language":
                    parts[SKILLS[2]]["cinematography"]["camera_language"] = "平视，双眼爱意直视镜头"
            return json.dumps(next(iter(parts[skill].values())), ensure_ascii=False)
        payload = json.loads(messages[1]["content"])
        reviews.append(payload)
        findings = []
        if repair_field and len(reviews) == 1:
            section, field = repair_field.split(".")
            findings = [{"severity": "warning", "code": "COMPOSITION_CAUSALITY",
                         "field_path": repair_field,
                         "evidence": payload["director_spec"][section][field],
                         "expected": "明确人物爱意的视线与构图",
                         "suggested_action": "让人物正对镜头，保留窗边环境"}]
        return json.dumps({"public_summary": "本轮方案已核验", "confidence": 0.9,
                           "findings": findings, "suggested_patches": []}, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", model)
    # This fixture exercises inheritance and director repair. Constraint semantics
    # have a separate model contract and are covered by the constraint suite.
    monkeypatch.setattr("kantoku.shells.web_studio.normalize_constraints",
                        lambda *_args, **_kwargs: ([], {"offline": True}))
    result = app.create_comic_director(project_id, {
        "expected_project_version": original.project.current_version,
        "conversation_id": cid, "creation_mode": "professional", "independent_plan": True,
        "task": "她一脸充满爱意的看着我",
    })
    spec = result["director_spec"]
    target_id = spec["project_id"]
    assert target_id != project_id and spec["spec_id"] != original_director.spec_id
    assert "shot_continuation" not in result
    assert len(calls) == (3 if not repair_field else
                         5 if repair_field.startswith("director_plan") else 4)
    assert len(reviews) == (2 if repair_field else 1)
    assert result["generation_mode"] == "FIRST_GENERATION"
    assert result["review_required"] is False
    assert spec["critic_feedback"] == [] and spec["critic_result"] is None
    assert result["director_execution_summary"]["critic_feedback"] == []
    assert app.get_comic_director(target_id)["review_required"] is False
    assert app.get_comic_director(target_id)["critic_feedback"] == []
    state = app.runtime_store.get_run(result["run_id"]).state
    assert state["stage_roles"][SKILLS[3]] == "internal_validator"
    assert state.get("generation_repair_count", 0) == bool(repair_field)
    assert spec["director_plan"]["character_expression"] == "含情脉脉，直视镜头"
    assert spec["creative_decision"]["emotional_target"] == "爱意"
    assert spec["cinematography"]["camera_language"] == (
        "平视，双眼爱意直视镜头" if repair_field == "cinematography.camera_language"
        else "平视，视线交于镜头")
    if repair_field == "director_plan.composition_strategy":
        assert "右三分线" in spec["director_plan"]["composition_strategy"]
    for field in ("character_presence", "subject_environment_relation", "style_boundary"):
        assert spec["director_plan"][field] == getattr(original_director.director_plan, field)
    assert app.comic_projects.get(project_id) == original
    assert app.comic_projects.get_director(project_id) == original_director
    assert not spec["user_confirmed"]
    app.confirm_comic_director(target_id, {
        "expected_project_version": app.comic_projects.get(target_id).project.current_version,
        "version": spec["version"],
    })
    # Editing the second plan must not invalidate the first plan or its approval.
    edited = app.create_comic_director(target_id, {
        "expected_project_version": app.comic_projects.get(target_id).project.current_version,
        "expected_director_version": spec["version"], "conversation_id": cid,
        "draft": {key: value for key, value in spec.items()
                  if key in DirectorSpecDraft.model_fields},
    })
    assert edited["generation_mode"] == "USER_EDIT" and edited["review_required"] is True
    if repair_field:
        reviews.clear()
        reviewed = app.create_comic_director(target_id, {
            "expected_project_version": app.comic_projects.get(target_id).project.current_version,
            "expected_director_version": edited["version"], "conversation_id": cid,
            "creation_mode": "professional", "review_current": True,
        })
        assert reviewed["review_required"] is True
        assert reviewed["director_spec"]["critic_feedback"][0]["affected_field"] == repair_field
        assert len(reviews) == 1  # User edits get advice, never an automatic rewrite.
        assert not app.runtime_store.get_run(reviewed["run_id"]).state.get(
            "generation_repair_count")
    reopened = ComicProjectStore(app.comic_projects.path)
    assert reopened.get(project_id) == original
    assert reopened.creative_context_lock(target_id, cid)["character"] == (
        original_director.director_plan.character_presence)
    assert app.comic_projects.human_director_confirmed(original_director) is confirmed
    assert app.runtime_store.get_run(result["run_id"]).state["task_system"] is False
    assert app.conversation(cid)["related_run_ids"].count(result["run_id"]) == 1


def test_independent_plan_reuses_canonical_bibles_without_changing_old_shots(
    app, creative, monkeypatch,
):
    request, _ = creative
    first_prompt = compile_first(app, request)
    source_id, cid = request["production_project_id"], request["conversation_id"]
    before = app.comic_projects.get(source_id)
    before_boards = app.comic_storyboards.list(source_id)
    monkeypatch.setattr("kantoku.shells.web_studio.normalize_constraints",
                        lambda *_args, **_kwargs: ([], {"offline": True}))
    result = app.create_comic_director(source_id, {
        "expected_project_version": before.project.current_version,
        "conversation_id": cid, "creation_mode": "professional", "independent_plan": True,
        "task": "手持长剑战斗",
    })
    target_id = result["director_spec"]["project_id"]
    app.confirm_comic_director(target_id, {
        "expected_project_version": app.comic_projects.get(target_id).project.current_version,
        "version": result["director_spec"]["version"],
    })
    # The semantic-constraint compiler uses this shared text adapter as well.
    def prompt_model(messages):
        payload = json.loads(messages[1]["content"])
        if "sections" in payload:
            assert payload["constraints"] == []
            return json.dumps({"complete": True, "scope_valid": True, "findings": []})
        return app._comic_storyboard_model(messages)

    monkeypatch.setattr(app, "_comic_constraint_model", prompt_model)
    new_prompt = compile_first(app, {"production_project_id": target_id, "conversation_id": cid})
    for field in ("character_context", "world_context", "style_context"):
        assert new_prompt[field] == first_prompt[field]
    assert app.comic_projects.get(source_id) == before
    assert app.comic_storyboards.list(source_id) == before_boards
    assert app.comic_prompts.get(first_prompt["shot_id"]).artifact_id == first_prompt["artifact_id"]
    reopened = ComicProjectStore(app.comic_projects.path)
    lock = reopened.creative_context_lock(target_id, cid)
    assert reopened.locked_bibles(target_id, lock)["character_context"] == first_prompt[
        "character_context"]


def test_pronoun_followup_receives_all_bibles_before_shot_understanding(app, production, creative):
    request, seen = creative
    project_id, cid = request["production_project_id"], request["conversation_id"]
    before = app.comic_projects.get_director(project_id)
    director_calls = list(production)
    result = app.create_comic_director(project_id, {
        "expected_project_version": request["expected_project_version"],
        "creation_mode": "professional", "conversation_id": cid,
        "task": "她站在山顶，背对镜头",
    })
    shot_input = next(item for item in seen if item.get("stable_context", {}).get(
        "creative_context_lock", {}).get("mode") == "shot_continuation")
    lock = shot_input["stable_context"]["creative_context_lock"]
    assert all(lock["bibles"][field] for field in
               ("character_context", "world_context", "style_context"))
    assert lock["confirmed_director_decision"]["spec_id"] == before.spec_id
    assert lock["confirmed_director_decision"]["version"] == before.version
    prompt = result["shot_continuation"]["prompt"]
    for field in ("character_context", "world_context", "style_context"):
        assert prompt[field] == lock["bibles"][field]
    assert "背对镜头" in prompt["shot_context"] and "夜晚" in prompt["world_context"]
    assert app.comic_projects.get_director(project_id) == before
    assert production == [*director_calls, "comic.director_critic"]
    assert "history" not in shot_input and "messages" not in shot_input


def test_explicit_world_change_passes_retained_bibles_and_decision_to_all_director_stages(
    app, creative, monkeypatch,
):
    request, _seen = creative
    original = app._comic_director_model
    calls = []

    def model(messages):
        if any(f'"title": "{name}"' in messages[0]["content"] for name in
               ("CreativeDecision", "DirectorPlan", "CinematographyPlan")):
            calls.append(json.loads(messages[1]["content"]))
        return original(messages)

    monkeypatch.setattr(app, "_comic_director_model", model)
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context({"world_context"}))
    project_id = request["production_project_id"]
    before = app.comic_projects.get_director(project_id)
    result = app.create_comic_director(project_id, {
        "expected_project_version": request["expected_project_version"],
        "creation_mode": "professional", "conversation_id": request["conversation_id"],
        "task": "改变世界观为现代城市",
    })
    # 视觉方向缺少固定人物/风格字段时，导演阶段会自动补全一次；
    # 这次修复保留上下文锁并允许这类内部补全，不把未完成方案交给用户。
    assert len(calls) >= 3 and not result["director_spec"]["user_confirmed"]
    for call in calls:
        lock = call["context"]["creative_context_lock"]
        assert set(lock["bibles"]) == {"character_context", "style_context"}
        assert lock["released_fields"] == ["world_context"]
        assert lock["director_version"] == before.version
        assert lock["confirmed_director_decision"] is None
        assert "角色身份" not in call["context"].get("history", "")


def test_continuity_does_not_depend_on_paginated_task_list(app, creative, monkeypatch):
    request, _seen = creative
    first = compile_first(app, request)
    monkeypatch.setattr(app.runtime_store, "list_runs", lambda **_kwargs: [])
    result = app.create_comic_director(request["production_project_id"], {
        "expected_project_version": app.comic_projects.get(
            request["production_project_id"]).project.current_version,
        "conversation_id": request["conversation_id"], "creation_mode": "professional",
        "task": "手持长剑战斗",
    })
    assert result["shot_continuation"]["prompt"]["character_context"] == first["character_context"]


def test_explicitly_edited_and_confirmed_bible_is_not_reverted_on_next_shot(app, creative):
    request, _seen = creative
    first = compile_first(app, request)
    draft = {key: value for key, value in first.items() if key in ComicPromptDraft.model_fields}
    draft["character_context"] = first["character_context"].replace("白色古风长裙", "蓝色古风长裙")
    draft["positive_prompt"] = first["positive_prompt"].replace(
        first["character_context"], draft["character_context"])
    project_id, cid = request["production_project_id"], request["conversation_id"]
    edited = app.edit_comic_prompt(first["shot_id"], {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_version": first["version"], "draft": draft,
    })
    app.confirm_comic_prompt(first["shot_id"], {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_version": edited["version"], "conversation_id": cid,
    })
    lock = app.comic_projects.creative_context_lock(project_id, cid)
    assert lock["bible_sources"]["character_context"]["artifact_id"] == edited["artifact_id"]
    assert lock["bible_sources"]["world_context"]["artifact_id"] == first["artifact_id"]
    result = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "conversation_id": cid, "creation_mode": "professional", "task": "手持长剑战斗",
    })
    assert result["shot_continuation"]["prompt"]["character_context"] == edited["character_context"]


def test_lock_does_not_leak_to_another_conversation_or_project(app, creative):
    request, _seen = creative
    first = compile_first(app, request)
    other = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    project_id = request["production_project_id"]
    assert app._comic_context_lock(project_id, other) is None
    foreign = app.create_comic_project({"title": "另一个作品"})["project"]["project_id"]
    lock = app.comic_projects.creative_context_lock(project_id, request["conversation_id"])
    with pytest.raises(ToolError):
        app.comic_projects.locked_bibles(foreign, lock)
    assert app.comic_prompts.get(first["shot_id"]).artifact_id == first["artifact_id"]


@pytest.mark.parametrize("text,expected", [
    ("手持长剑战斗", set()), ("不要换角色，手持长剑战斗", set()),
    ("换角色为男性剑客", {"character_context"}),
    ("角色不变，换世界为现代都市", {"world_context"}),
    ("换风格为水墨", {"style_context"}),
    ("角色换成少年，风格改为水墨", {"character_context", "style_context"}),
    ("修改人物的姿态，手持长剑战斗", set()),
    ("更换角色的动作", set()),
    ("不要换角色但是换风格为水墨", {"style_context"}),
    ("重新设计人物", {"character_context"}),
    ("改变世界观", {"world_context"}), ("修改画风", {"style_context"}),
    ("不要重新设计人物，修改人物姿态", set()),
    ("换个主题，赛博都市赛车", {"character_context", "world_context", "style_context"}),
    ("不要换主题，她摆出战斗姿态", set()),
])
def test_only_explicit_setting_changes_release_context(text, expected):
    assert ComicContextBuilder.explicit_context_changes(text) == expected


def test_explicit_character_change_retains_world_style_and_requires_confirmation(
    app, creative, monkeypatch,
):
    request, _seen = creative
    first = compile_first(app, request)
    project_id, cid = request["production_project_id"], request["conversation_id"]
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context({"character_context"}))
    result = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "creation_mode": "professional", "conversation_id": cid, "task": "换角色为男性剑客",
    })
    assert "shot_continuation" not in result
    assert not result["director_spec"]["user_confirmed"]
    lock = app.comic_projects.creative_context_lock(project_id, cid)
    assert set(lock["bible_sources"]) == {"world_context", "style_context"}
    assert app.comic_projects.locked_bibles(project_id, lock)["world_context"] == first[
        "world_context"]
    assert app._comic_context_lock(project_id, cid) is None


def test_critic_unresolved_execution_is_explained_on_real_application_response(app, monkeypatch):
    project_id, first, _calls = _generate(app, monkeypatch)
    draft = {key: value for key, value in first["director_spec"].items()
             if key in DirectorSpecDraft.model_fields}
    draft["cinematography"]["camera_angle"] = "低角度或者平视"
    edited = app.create_comic_director(project_id, {
        "expected_project_version": 2, "expected_director_version": 1, "draft": draft})
    result = app.create_comic_director(project_id, {
        "expected_project_version": 3, "creation_mode": "professional", "review_current": True,
        "expected_director_version": edited["version"],
        "conversation_id": app.runtime_store.get_run(first["run_id"]).state["conversation_id"],
    })
    assert result["status"] == "waiting"
    feedback = result["director_spec"]["critic_feedback"]
    issue = next(item for item in feedback
                 if item["affected_field"] == "cinematography.camera_angle")
    assert issue["level"] == "blocking" and "唯一" in issue["message"]
    assert "候选" in issue["reason"] and "最终值" in issue["suggestion"]
    assert result["director_execution_summary"]["critic_feedback"] == feedback
    with pytest.raises(ToolError, match="待修订"):
        app.confirm_comic_director(project_id, {
            "version": result["director_spec"]["version"],
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        })


def test_warning_only_review_can_be_confirmed_through_application(app, production, monkeypatch):
    original = app._comic_director_model

    def warning_model(messages):
        if any(f'"title": "{name}"' in messages[0]["content"] for name in
               ("CreativeDecision", "DirectorPlan", "CinematographyPlan")):
            return original(messages)
        return json.dumps({"public_summary": "构图可以进一步明确", "confidence": 0.9,
                           "suggested_patches": [], "findings": [{
                               "code": "COMPOSITION_CAUSALITY", "severity": "warning",
                               "field_path": "director_plan.composition_strategy",
                               "evidence": "保留前中后景层次引导视线",
                               "expected": "更具体地连接人物与环境",
                               "suggested_action": "可加强人物与环境之间的视觉关系",
                           }]}, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", warning_model)
    request = _workspace_production_request(app, confirm=False)
    project_id = request["production_project_id"]
    saved = app.confirm_comic_director(project_id, {
        "version": request["director_version"],
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
    })
    assert saved["user_confirmed"] is True
    assert app.comic_projects.get_director(project_id).critic_result.verdict == "needs_revision"
    assert saved["critic_result"] is None and saved["critic_feedback"] == []
    assert saved["review_required"] is False


@pytest.mark.parametrize("field,kind", [("character_concept", "missing_character"),
                                       ("environment", "missing_scene")])
def test_missing_required_context_explains_why_and_how_to_fix(field, kind):
    feedback = public_critic_feedback({"findings": [{
        "code": "CONTEXT_MISSING", "severity": "error", "field_path": field,
        "evidence": "", "suggested_action": "",
    }]})[0]
    assert feedback["level"] == "blocking" and feedback["issue_type"] == kind
    assert feedback["message"] and feedback["reason"] and "重新审核" in feedback["suggestion"]


@pytest.mark.parametrize("field,kind", [("character_presence", "missing_character"),
                                       ("subject_environment_relation", "missing_scene")])
def test_missing_context_with_evidence_keeps_human_category(field, kind):
    feedback = public_critic_feedback({"findings": [{
        "code": "CONTEXT_MISSING", "severity": "error", "field_path": field,
        "evidence": "当前字段未绑定稳定设定", "suggested_action": "补充固定上下文",
    }]})[0]
    assert feedback["level"] == "blocking" and feedback["issue_type"] == kind


@pytest.mark.parametrize("path,label", [
    ("cinematography.shot_size", "景别"), ("cinematography.camera_angle", "机位"),
    ("cinematography.light_direction", "主光方向"),
    ("cinematography.color_relationship", "色彩方案"),
])
def test_unresolved_execution_has_specific_human_reason_and_fix(path, label):
    feedback = public_critic_feedback({"findings": [{
        "code": "EXECUTION_DECISION_UNRESOLVED", "severity": "error", "field_path": path,
        "evidence": "方案 A 或方案 B", "suggested_action": "人工修订",
    }]})[0]
    assert feedback["level"] == "blocking"
    assert feedback["issue_type"] == "required_parameter"
    assert label in feedback["message"] and "候选" in feedback["reason"]
    assert "一个最终值" in feedback["suggestion"]
    assert "EXECUTION_DECISION_UNRESOLVED" not in json.dumps(feedback)


@pytest.mark.parametrize("path,kind", [
    ("director_plan.composition_strategy", "composition_advice"),
    ("director_plan.color_strategy", "color_advice"),
    ("creative_decision.emotional_target", "atmosphere_advice"),
    ("cinematography.lighting", "photography_advice"),
])
def test_advice_explains_impact_and_never_becomes_blocking(path, kind):
    feedback = public_critic_feedback({"findings": [{
        "code": "ARTISTIC_ADVICE", "severity": "warning", "field_path": path,
        "evidence": "当前方案还可以更明确", "suggested_action": "可增强主体与环境关系",
    }]})[0]
    assert feedback["level"] == "warning" and feedback["issue_type"] == kind
    assert "不阻止最终确认" in feedback["impact"]
    assert all(feedback[key] for key in
               ("message", "reason", "suggestion", "affected_field"))


def test_first_advice_is_repaired_by_director_without_replacing_character(app, monkeypatch):
    parts, calls = _responses(), []
    original = parts[SKILLS[1]]["director_plan"]
    reviews = 0

    def model(messages):
        nonlocal reviews
        payload = json.loads(messages[1]["content"])
        for skill, title in zip(SKILLS[:3],
                               ("CreativeDecision", "DirectorPlan", "CinematographyPlan"),
                               strict=True):
            if f'"title": "{title}"' in messages[0]["content"]:
                calls.append(skill)
                result = copy.deepcopy(next(iter(parts[skill].values())))
                if payload["context"].get("generation_feedback") and skill == SKILLS[1]:
                    assert payload["context"]["generation_feedback"][0]["level"] == "warning"
                    result.update(composition_strategy="人物位于右三分线，左侧山谷作为负空间",
                                  character_presence="新的男性角色", style_boundary="卡通")
                return json.dumps(result, ensure_ascii=False)
        reviews += 1
        return json.dumps({"public_summary": "已核验", "confidence": 0.9,
                           "suggested_patches": [], "findings": [] if reviews == 2 else [{
                               "severity": "warning", "code": "COMPOSITION_CAUSALITY",
                               "field_path": "director_plan.composition_strategy",
                               "evidence": original["composition_strategy"],
                               "expected": "突出山谷与人物的关系",
                               "suggested_action": "将人物置于右侧并留出左侧山谷空间"}]},
                          ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", model)
    from test_comic_creation_mode import _execute

    from kantoku.domains.comic.models import ComicProjectInput
    project = app.comic_projects.create(ComicProjectInput(
        title="首次质量优化", brief={"original_request": "东方修仙少女夜晚山巅"}))
    cid = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})["id"]
    result = _execute(app, project.project.project_id, "professional",
                      task="东方修仙少女夜晚山巅", conversation_id=cid)
    spec = app.comic_projects.get_director(project.project.project_id)
    assert result["status"] == "completed" and reviews == 2
    assert calls == [*SKILLS[:3], SKILLS[1], SKILLS[2]]
    assert "右三分线" in spec.director_plan.composition_strategy
    assert spec.director_plan.character_presence == original["character_presence"]
    assert spec.director_plan.style_boundary == original["style_boundary"]
    assert not result["director_execution_summary"]["critic_feedback"]
    assert not result["director_spec"]["user_confirmed"]


def test_single_frame_uses_director_not_an_unrequested_environment_sequence(app, monkeypatch):
    project_id, result, _ = _generate(app, monkeypatch)
    spec = app.comic_projects.get_director(project_id)
    app.confirm_comic_director(project_id, {"expected_project_version": 2, "version": spec.version})
    monkeypatch.setattr(app, "_comic_storyboard_model", lambda _: pytest.fail(
        "单幅已确认构图不得重新调用分镜模型拆镜"))
    board = app.create_comic_storyboard(project_id, {"expected_project_version": 2,
                                                  "generate": True})
    assert len(board["shots"]) == 1
    shot = board["shots"][0]
    assert shot["subject"] == spec.director_plan.character_presence
    assert shot["action"] == spec.director_plan.character_pose
    assert shot["environment"] == spec.director_plan.subject_environment_relation
    assert shot["shot_size"] == spec.cinematography.shot_size
    assert shot["camera_angle"] == spec.cinematography.camera_angle


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_empty_character_prompt_repairs_once_without_resetting_director(
    app, monkeypatch, repair_succeeds,
):
    from test_comic_prompt_production import _sections
    project_id, first, _ = _generate(app, monkeypatch)
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    spec = app.comic_projects.get_director(project_id)
    app.confirm_comic_director(project_id, {"expected_project_version": 2, "version": spec.version})
    calls = []

    def model(messages):
        calls.append(copy.deepcopy(messages))
        result = _sections()
        if len(calls) == 1 or not repair_succeeds:
            result["character_context"]["identity"] = ""
        return json.dumps(result, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_storyboard_model", model)
    request = {"production_project_id": project_id, "conversation_id": cid}
    if repair_succeeds:
        prompt = compile_first(app, request)
        assert "少女" in prompt["character_context"]
        assert prompt["media_preparation"]["status"] == "awaiting_external_image"
        assert len(app.runtime_store.list_artifacts(type=ArtifactType.PROMPT)) == 1
    else:
        with pytest.raises(ToolError, match="结构仍不完整"):
            compile_first(app, request)
        assert not app.runtime_store.list_artifacts(type=ArtifactType.PROMPT)
    assert len(calls) == 2
    assert "character_context.identity" in calls[1][0]["content"]
    assert app.comic_projects.get_director(project_id) == spec
    assert app.comic_projects.human_director_confirmed(spec)
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)


def test_legacy_pronoun_fork_never_silently_restores_an_unreferenced_older_plan(
    app, monkeypatch,
):
    from test_comic_creation_mode import _execute
    from test_comic_prompt_production import _sections
    brief, action = "电影级超兽武装火麟飞站在悬崖上看着远方", "他摆出火云诀的起手式"
    project_id, first, _ = _generate(app, monkeypatch, brief_text=brief, director_fields={
        "character_presence": "火麟飞，少年英雄，红色战斗服，深红短发",
        "subject_environment_relation": "超兽武装世界，暮色悬崖", "character_pose": "站立远望"})
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    app.confirm_comic_director(project_id, {"expected_project_version": 2, "version": 1})
    prompt_output = _sections()
    prompt_output["character_context"].update(identity="火麟飞", appearance="深红短发",
                                             clothing="红色战斗服")
    prompt_output["world_context"].update(location="悬崖", time="暮色",
                                         world_setting="超兽武装世界", environment="岩壁与峡谷")
    monkeypatch.setattr(app, "_comic_storyboard_model", lambda _:
                        json.dumps(prompt_output, ensure_ascii=False))
    first_prompt = compile_first(app, {"production_project_id": project_id, "conversation_id": cid})
    app.comic_projects.fork_brief(project_id, CreativeBriefFork(
        original_request=action,
        expected_version=app.comic_projects.get(project_id).project.current_version,
        parent_brief_id=app.comic_projects.get(project_id).creative_brief.brief_id,
        parent_brief_version=1, reason="explicit new creative direction"))
    # 重现旧错误状态：动作成为 Brief v2，当前导演模型另造三十岁修士。
    parts = _responses()
    parts[SKILLS[1]]["director_plan"].update(
        character_presence="三十岁男性修士，青灰长袍", character_pose=action,
        subject_environment_relation="晨昏云海石台", style_boundary="东方仙侠")
    seen = []

    def model(messages):
        seen.append(copy.deepcopy(messages))
        for skill, title in zip(SKILLS[:3],
                               ("CreativeDecision", "DirectorPlan", "CinematographyPlan"),
                               strict=True):
            if f'"title": "{title}"' in messages[0]["content"]:
                return json.dumps(next(iter(parts[skill].values())), ensure_ascii=False)
        return json.dumps({"public_summary": "可执行", "confidence": 0.9,
                           "findings": [], "suggested_patches": []})

    monkeypatch.setattr(app, "_comic_director_model", model)
    wrong = _execute(app, project_id, "professional", task=action,
                     conversation_id=cid, creative_operation="new")
    assert "修士" in wrong["director_spec"]["director_plan"]["character_presence"]
    before = app.comic_projects.get(project_id)
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context())
    result = app.create_comic_director(project_id, {
        "expected_project_version": before.project.current_version,
        "creation_mode": "professional", "conversation_id": cid,
        "creative_operation": "auto", "task": action,
    })
    current = app.comic_projects.get_director(project_id)
    assert result["status"] == "completed"
    assert "修士" in current.director_plan.character_presence
    assert (app.comic_projects.get(project_id).creative_brief.version
            == before.creative_brief.version)
    assert app._comic_director_result(app.runtime_store.get_run(first["run_id"]))[
        "director_spec"]["version"] == 1
    assert app.comic_prompts.get(first_prompt["shot_id"]).artifact_id == first_prompt["artifact_id"]
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)


def test_prompt_provider_exception_is_not_a_structure_retry(app, monkeypatch):
    project_id, first, _ = _generate(app, monkeypatch)
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    app.confirm_comic_director(project_id, {"expected_project_version": 2, "version": 1})
    calls = []

    def unavailable(messages):
        calls.append(messages)
        raise ToolError("文本供应商连接失败，结果未知", detail="module=llm.transport")

    monkeypatch.setattr(app, "_comic_storyboard_model", unavailable)
    with pytest.raises(ToolError, match="结果未知"):
        compile_first(app, {"production_project_id": project_id, "conversation_id": cid})
    assert len(calls) == 1
    assert not app.runtime_store.list_artifacts(type=ArtifactType.PROMPT)


def test_first_generation_locks_all_anchors_and_hides_internal_validation(app, monkeypatch):
    project_id, result, _ = _generate(app, monkeypatch)
    run = app.runtime_store.get_run(result["run_id"])
    cid = run.state["conversation_id"]
    assert run.state["generation_mode"] == "FIRST_GENERATION"
    assert run.state["review_required"] is False
    assert run.state["stage_roles"]["comic.director_critic"] == "internal_validator"
    assert run.state["critic_result"]["verdict"] == "pass"
    lock = ComicProjectStore(app.comic_projects.path).creative_context_lock(project_id, cid)
    assert all(lock[key] for key in ("character", "world", "visual_style", "camera_style",
                                     "environment", "fixed_attributes"))
    assert run.state["creative_context_lock"] == lock
    assert result["director_spec"]["validation"] == {"can_confirm": True, "can_produce": False}
    for public in (result["director_spec"], app.get_comic_director(project_id),
                   app.list_comic_director_versions(project_id)["versions"][0]):
        assert not public["review_required"]
        assert public["critic_result"] is None and public["critic_feedback"] == []
    summary = result["director_execution_summary"]
    assert summary["stages"][3]["output"] == {} and summary["critic_result"] is None
    assert summary["stages"][4]["output"]["director_spec"]["critic_result"] is None
    names = [event.payload.get("director_event") for event in app.runtime_store.list_events(run.id)]
    assert "internal_validator_started" in names and "director_critic_started" not in names


def test_emperor_action_inherits_character_enemy_environment_and_camera(app, monkeypatch):
    brief = "帝皇侠站在黄昏废墟面对异能兽"
    action = "帝皇侠准备五圣必杀起手式"
    project_id, first, _ = _generate(app, monkeypatch, brief_text=brief, director_fields={
        "character_presence": "帝皇侠，金色铠甲；异能兽，黑色装甲",
        "subject_environment_relation": "黄昏废墟，帝皇侠面对异能兽",
        "style_boundary": "超写实电影风格",
    })
    before = app.comic_projects.get_director(project_id)
    parts, reviews = _responses(), []
    parts[SKILLS[0]]["creative_decision"].update(
        intent_summary=f"{brief}，{action}", emotional_target="战斗前的决心")
    parts[SKILLS[1]]["director_plan"].update(character_presence="男性武士",
                                            style_boundary="卡通", character_pose=action,
                                            visual_focus="帝皇侠的起手式",
                                            composition_strategy="双手起势置于视觉中心")
    parts[SKILLS[2]]["cinematography"].update(camera_language="新的俯视镜头",
                                             shot_size="特写", camera_angle="俯视")

    def model(messages):
        payload = json.loads(messages[1]["content"])
        for skill, title in zip(SKILLS[:3], ("CreativeDecision", "DirectorPlan",
                                            "CinematographyPlan"), strict=True):
            if f'"title": "{title}"' in messages[0]["content"]:
                lock = payload["context"]["creative_context_lock"]
                assert "帝皇侠" in lock["character"] and "异能兽" in lock["world"]
                return json.dumps(next(iter(parts[skill].values())), ensure_ascii=False)
        reviews.append(payload["context_diff"])
        return json.dumps({"public_summary": "动作修改已核验", "confidence": 0.9,
                           "findings": [], "suggested_patches": []}, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", model)
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context())
    result = app.create_comic_director(project_id, {
        "expected_project_version": 2, "creation_mode": "professional", "task": action,
        "conversation_id": app.runtime_store.get_run(first["run_id"]).state["conversation_id"],
    })
    after = app.comic_projects.get_director(project_id)
    assert result["generation_mode"] == "USER_EDIT" and result["review_required"]
    assert after.director_plan.character_pose == action
    for field in ("character_presence", "subject_environment_relation", "style_boundary"):
        assert getattr(after.director_plan, field) == getattr(before.director_plan, field)
    for field in ("camera_language", "shot_size", "camera_angle", "material_language"):
        assert getattr(after.cinematography, field) == parts[SKILLS[2]]["cinematography"][field]
    assert after.creative_decision.narrative_context == (
        parts[SKILLS[0]]["creative_decision"]["narrative_context"])
    assert action in after.creative_decision.intent_summary and reviews
    assert app.comic_projects.get(project_id).creative_brief.original_request == brief
    assert "director_plan.character_pose" in result["context_diff"]["affected_fields"]
    assert "creative_decision.intent_summary" in result["context_diff"]["affected_fields"]


@pytest.mark.parametrize("instruction,scopes", [
    ("把天空改成血红色", {"world_context", "color"}),
    ("换成夜景", {"world_context", "lighting"}),
    ("改变镜头为俯视", {"camera_style"}),
])
def test_user_edit_runs_diff_review_without_resetting_unedited_anchors(
    app, monkeypatch, instruction, scopes,
):
    project_id, first, _ = _generate(app, monkeypatch)
    before = app.comic_projects.get_director(project_id)
    parts, seen = _responses(), []
    parts[SKILLS[0]]["creative_decision"].update(narrative_context="完全不同的科幻世界",
                                                intent_summary="新的主题")
    parts[SKILLS[1]]["director_plan"].update(
        character_presence="新的角色", style_boundary="水墨画",
        subject_environment_relation="同一山巅，血红天空，夜景",
        color_strategy="红色天空与灰白衣服", composition_strategy="完全不同的构图")
    parts[SKILLS[2]]["cinematography"].update(camera_angle="俯视", shot_size="全景",
                                             color_relationship="红色天空与灰白衣服")

    def model(messages):
        payload = json.loads(messages[1]["content"])
        for skill, title in zip(SKILLS[:3], ("CreativeDecision", "DirectorPlan",
                                            "CinematographyPlan"), strict=True):
            if f'"title": "{title}"' in messages[0]["content"]:
                return json.dumps(next(iter(parts[skill].values())), ensure_ascii=False)
        seen.append(payload)
        return json.dumps({"public_summary": "修改影响已核验", "confidence": 0.9,
                           "findings": [], "suggested_patches": []}, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", model)
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context(scopes))
    result = app.create_comic_director(project_id, {
        "expected_project_version": 2, "creation_mode": "professional", "task": instruction,
        "conversation_id": app.runtime_store.get_run(first["run_id"]).state["conversation_id"],
    })
    after = app.comic_projects.get_director(project_id)
    assert result["generation_mode"] == "USER_EDIT" and result["review_required"]
    assert after.creative_brief_version == before.creative_brief_version
    assert after.director_plan.character_presence == before.director_plan.character_presence
    assert after.director_plan.style_boundary == before.director_plan.style_boundary
    assert after.director_plan.character_pose == before.director_plan.character_pose
    assert after.creative_decision.narrative_context == "完全不同的科幻世界"
    assert after.director_plan.composition_strategy == "完全不同的构图"
    assert after.cinematography.camera_angle == "俯视"
    if "camera_style" in scopes:
        assert after.director_plan.subject_environment_relation == (
            before.director_plan.subject_environment_relation)
    assert seen[-1]["context_diff"]["changes"]
    assert result["context_diff"]["changes"] == seen[-1]["context_diff"]["changes"]
    assert not app.runtime_store.get_run(result["run_id"]).state.get("generation_repair_count")


@pytest.mark.parametrize("reset", ["新建对话", "新项目", "重新设计", "换一个完全不同主题"])
def test_explicit_reset_starts_new_context_without_inherited_lock(app, monkeypatch, reset):
    project_id, first, _ = _generate(app, monkeypatch)
    original = app._comic_director_model
    contexts = []

    def model(messages):
        payload = json.loads(messages[1]["content"])
        if "context" in payload:
            contexts.append(payload["context"])
        return original(messages)

    monkeypatch.setattr(app, "_comic_director_model", model)
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context(new=True))
    result = app.create_comic_director(project_id, {
        "expected_project_version": 2, "creation_mode": "professional",
        "task": f"{reset}，未来城市机器人咖啡师",
        "conversation_id": app.runtime_store.get_run(first["run_id"]).state["conversation_id"],
    })
    assert result["generation_mode"] == "NEW_CONTEXT" and not result["review_required"]
    assert result["director_spec"]["critic_result"] is None
    assert contexts and all("creative_context_lock" not in item for item in contexts)
    assert app.comic_projects.get(project_id).creative_brief.version == 2
    assert len(app.comic_projects.director_versions(project_id)) == 2


def test_new_conversation_in_same_project_resets_context(app, monkeypatch):
    project_id, _first, _ = _generate(app, monkeypatch)
    cid = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})["id"]
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context(new=True))
    result = app.create_comic_director(project_id, {
        "expected_project_version": 2, "creation_mode": "professional",
        "conversation_id": cid, "task": "未来城市机器人咖啡师",
    })
    assert result["generation_mode"] == "NEW_CONTEXT" and not result["review_required"]
    assert result["director_spec"]["creative_brief_version"] == 2


def test_confirmed_shot_conflict_returns_persisted_review_without_new_prompt(
    app, creative, monkeypatch,
):
    request, _ = creative
    project_id, cid = request["production_project_id"], request["conversation_id"]
    first = compile_first(app, request)
    original = app.comic_projects.get_director(project_id)
    action = "手持长剑战斗"

    def review(messages):
        payload = json.loads(messages[1]["content"])
        assert payload["context_diff"]["instruction"] == action
        return json.dumps({"public_summary": "本次修改存在冲突", "confidence": 0.9,
                           "suggested_patches": [], "findings": [{
                               "code": "WORLD_CONFLICT", "severity": "error",
                               "field_path": "director_plan.character_pose", "evidence": action,
                               "expected": "动作必须与当前环境相容",
                               "suggested_action": "请同步调整动作与环境的关系",
                           }]}, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", review)
    result = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "creation_mode": "professional", "conversation_id": cid, "task": action,
    })
    # 真实阻断冲突必须拦住本次生成、保留原方案与既有 Prompt。
    # 已有方案的动作修改属于 USER_EDIT，审核绑定本轮候选。
    assert result["status"] == "waiting" and result["review_required"]
    assert not result["ready_for_prompt"] and "shot_continuation" not in result
    assert result["context_diff"]["potential_conflicts"]
    assert result["director_spec"]["critic_feedback"][0]["level"] == "blocking"
    assert app.comic_projects.get_director(project_id) == original
    assert app.comic_prompts.get(first["shot_id"]).artifact_id == first["artifact_id"]
    assert len(app.runtime_store.list_artifacts(type=ArtifactType.PROMPT)) == 1
    assert app.runtime_store.get_run(result["run_id"]).state["review_required"] is True
    persisted = next(item for item in app.list_comic_project_tasks(project_id)["tasks"]
                     if item["run_id"] == result["run_id"])
    assert persisted["context_diff"] == result["context_diff"]


@pytest.mark.parametrize("text", ["不要新建对话", "不要重新设计", "不要换一个完全不同主题",
                                  "不要改变镜头", "天空不要改成红色"])
def test_negated_changes_keep_context(text):
    assert not ComicContextBuilder.starts_new_context(text)
    assert not ComicContextBuilder.edit_scopes(text)


@pytest.mark.parametrize("text,expected", [
    ("但是他被黑暗力量吞噬变成怒吼进行黑化", {
        "creative_decision.emotional_target", "director_plan.character_expression",
        "director_plan.character_pose"}),
    ("掏剑准备五圣必杀起手式", set()),
    ("把天空改成血红色", set()),
])
def test_action_state_change_declares_its_semantic_cluster(text, expected):
    """状态类指令必须牵动同语义字段，否则审核会拿旧情绪判定新姿态矛盾。"""
    assert ComicContextBuilder.action_semantic_cluster(text) == expected


def test_changed_field_does_not_hide_an_active_asset_or_user_constraint():
    from test_comic_director_critic import _brief, _semantic, _spec

    from kantoku.domains.comic.critic import DirectorCriticEngine

    spec = _spec()
    field = "director_plan.visual_focus"
    finding = {"code": "ASSET_CONSTRAINT_CONFLICT", "severity": "error", "field_path": field,
               "evidence": spec.director_plan.visual_focus, "expected": "保留固定资产约束",
               "suggested_action": "核对当前有效来源"}
    result = DirectorCriticEngine(lambda _: _semantic([finding])).review(
        spec, _brief(), context_diff={"changes": [{"field_path": field,
            "before": "旧模型选择", "after": spec.director_plan.visual_focus}]})
    assert any(item.code == "ASSET_CONSTRAINT_CONFLICT" for item in result.findings)
    assert result.verdict != "pass"


def test_first_generation_review_is_never_exposed_as_user_feedback(app, creative):
    """首次系统生成的内部校验不得作为“需要用户修改”的审核建议外显。"""
    project_id, cid = creative[0]["production_project_id"], creative[0]["conversation_id"]
    app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "creation_mode": "professional", "conversation_id": cid,
        "task": "东方修仙少女夜晚山巅",
    })
    for task in app.list_comic_project_tasks(project_id)["tasks"]:
        # 任务列表同时包含 prompt.compile 等非导演任务，它们没有导演摘要。
        if task.get("generation_mode") != "FIRST_GENERATION":
            continue
        assert not task["review_required"]
        # 建议类意见不得外显；只有真正阻断的问题才允许出现。
        assert all(item["level"] == "blocking"
                   for item in task["director_execution_summary"]["critic_feedback"])


def test_legacy_run_without_project_version_does_not_break_task_list(app, creative):
    """旧 Run 缺少 project_version_after 时，历史对话的导演入口仍要可用。"""
    project_id, cid = creative[0]["production_project_id"], creative[0]["conversation_id"]
    created = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "creation_mode": "professional", "conversation_id": cid,
        "task": "东方修仙少女夜晚山巅",
    })
    run = app.runtime_store.get_run(created["run_id"])
    state = {key: value for key, value in run.state.items() if key != "project_version_after"}
    app.runtime_store.update_run(
        run.id, status=run.status, state=state, current_node=run.current_node, force=True,
    )
    # 修复前这里会抛 KeyError 并让整个 /tasks 返回 500，历史对话的导演入口随之不可用。
    tasks = app.list_comic_project_tasks(project_id)["tasks"]
    assert any(item["run_id"] == run.id for item in tasks)


def test_info_only_internal_validator_does_not_restart_generation(app, monkeypatch):
    project_id, result, calls = _generate(app, monkeypatch, critic_findings=[{
        "code": "COMPOSITION_CAUSALITY", "severity": "info",
        "field_path": "director_plan.composition_strategy",
        "evidence": _responses()[SKILLS[1]]["director_plan"]["composition_strategy"],
        "expected": "更清晰", "suggested_action": "可微调构图",
    }])
    assert len(calls) == 4
    assert result["status"] == "completed"
    assert result["director_spec"]["critic_feedback"] == []
    assert app.get_comic_director(project_id)["critic_feedback"] == []


def test_incomplete_generation_keeps_candidate_and_lock(app, monkeypatch):
    project_id, result, _ = _generate(app, monkeypatch,
        ambiguity={"shot_size": "近景或远景"}, resolve=False)
    assert result["status"] == "waiting"
    assert result["director_spec"] is not None
    assert not result["director_spec"]["validation"]["can_confirm"]
    assert result["director_spec"]["critic_feedback"] == []
    run = app.runtime_store.get_run(result["run_id"])
    assert run.state["creative_context_lock"]["character"]
    tasks = app.list_comic_project_tasks(project_id)["tasks"]
    task = next(item for item in tasks if item["run_id"] == run.id)
    assert task["director_spec"]["director_plan"] == result["director_spec"]["director_plan"]


def test_natural_restatement_edits_subject_without_resetting_context(app, monkeypatch):
    project_id, first, _ = _generate(app, monkeypatch)
    run = app.runtime_store.get_run(first["run_id"])
    cid = run.state["conversation_id"]
    baseline = app.comic_projects.get_director(project_id)
    parts = _responses()
    parts[SKILLS[0]]["creative_decision"]["intent_summary"] = "画面中的主角改为山神"
    parts[SKILLS[1]]["director_plan"]["character_presence"] = "山神"
    original = app._comic_director_model
    def model(messages):
        for key, title in zip(SKILLS[:3],
                              ("CreativeDecision", "DirectorPlan", "CinematographyPlan"),
                              strict=True):
            if f'"title": "{title}"' in messages[0]["content"]:
                return json.dumps(next(iter(parts[key].values())))
        return original(messages)
    monkeypatch.setattr(app, "_comic_director_model", model)
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context({"character_context"}))
    second = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "creation_mode": "professional", "creative_operation": "auto", "conversation_id": cid,
        "task": "帮我刻画一个山神",
    })
    assert second["generation_mode"] == "USER_EDIT"
    assert second["director_spec"]["director_plan"]["character_presence"] == "山神"
    assert (second["director_spec"]["director_plan"]["style_boundary"]
            == baseline.director_plan.style_boundary)
    assert (app.comic_projects.get(project_id).creative_brief.version
            == baseline.creative_brief_version)
    history = {item["run_id"]: item for item in app.list_comic_project_tasks(project_id)["tasks"]}
    assert (history[first["run_id"]]["director_spec"]["director_plan"]
            == first["director_spec"]["director_plan"])
    assert history[first["run_id"]]["director_spec"]["critic_feedback"] == []
    assert (history[second["run_id"]]["director_spec"]["version"]
            != first["director_spec"]["version"])


def test_legacy_failed_candidate_is_visible_and_inherited_on_next_request(app, monkeypatch):
    from kantoku.core.runtime.models import ExecutionStatus
    from kantoku.domains.comic.models import ComicProjectInput

    first_project, first, _ = _generate(app, monkeypatch)
    prior = app.runtime_store.get_run(first["run_id"])
    project = app.comic_projects.create(ComicProjectInput(
        title="旧版未保存候选", brief={"original_request": "东方修仙少女夜晚山巅"}))
    project_id = project.project.project_id
    state = {key: value for key, value in prior.state.items() if key not in {
        "director_spec_id", "director_spec_version", "project_version_after",
        "creative_context_lock",
    }}
    state.update(project_id=project_id, generation_incomplete=True,
                 input_brief_id=project.creative_brief.brief_id)
    legacy = app.runtime_store.create_run("comic", "comic.director", state, "director_critic")
    app.runtime_store.update_run(legacy.id, status=ExecutionStatus.FAILED, state=state,
                                 current_node="director_critic", error="old repair failed")
    visible = app.list_comic_project_tasks(project_id)["tasks"][0]
    assert visible["director_spec"]["draft"]
    assert visible["director_spec"]["source_run_id"] == legacy.id
    assert app.comic_projects.get(project_id).project.director_id is None
    result = app.create_comic_director(project_id, {
        "expected_project_version": 1, "creation_mode": "professional",
        "creative_operation": "auto", "conversation_id": prior.state["conversation_id"],
        "task": "准备拔剑起手式",
    })
    assert result["generation_mode"] == "FIRST_GENERATION"
    assert result["director_spec"]["director_plan"]["character_presence"] == (
        first["director_spec"]["director_plan"]["character_presence"])
    assert app.runtime_store.get_run(legacy.id).status is ExecutionStatus.FAILED
    assert app.comic_projects.get(first_project).project.director_version == 1


@pytest.mark.parametrize("text", [
    "帮我刻画一个超写实北欧神话里面的奥丁", "帮我刻画一个北欧神话里面的雷神索尔",
    "她站在山顶，背对镜头", "他摆出火云诀的起手式", "手持长剑战斗",
    "他遇到毁灭博士跟他对峙", "把天空改成血红色", "换成夜景",
])
def test_request_wording_never_drops_a_verified_active_constraint(text):
    from test_comic_director_critic import _brief, _semantic, _spec

    from kantoku.domains.comic.critic import DirectorCriticEngine

    spec = _spec()
    evidence = spec.director_plan.visual_focus
    finding = {"code": "HARD_CONSTRAINT_CONFLICT", "severity": "error",
               "field_path": "director_plan.visual_focus", "evidence": evidence,
               "expected": "与本轮仍有效的约束一致", "suggested_action": "检查明确来源"}
    result = DirectorCriticEngine(lambda _: _semantic([finding])).review(
        spec, _brief(), current_shot_instruction=text)
    assert any(item.code == "HARD_CONSTRAINT_CONFLICT" for item in result.findings)
    assert result.verdict != "pass"


def test_action_followup_in_conversation_skips_intent_boundary(app, monkeypatch):
    """补充动作继续沿用当前 Brief，不得请模型猜测新主题。"""
    project_id, first, _ = _generate(app, monkeypatch, brief_text="东方修仙少女夜晚山巅")
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    monkeypatch.setattr(app, "_comic_intent_model", semantic_context())
    result = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "creation_mode": "professional", "creative_operation": "auto",
        "conversation_id": cid, "task": "她站在山顶，背对镜头",
    })
    assert result["director_spec"]
    assert (
        app.comic_projects.get(project_id).creative_brief.original_request
        != "她站在山顶，背对镜头"
    )


def test_new_opponent_request_regenerates_plan_not_shot_continuation(app, monkeypatch):
    """输入引入新对手时必须重做导演方案，不能只把用户原句写进姿态。

    “他后面遇到毁灭博士然后摆出战斗状态”曾只更新 character_pose，
    导致新对手从未进入方案、审核必然判视觉因果冲突而卡住。
    """
    project_id, first, _ = _generate(app, monkeypatch)
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    parts = _responses()
    parts[SKILLS[1]]["director_plan"]["character_presence"] = "蜘蛛侠与毁灭博士"
    original = app._comic_director_model

    def model(messages):
        for key, title in zip(SKILLS[:3],
                              ("CreativeDecision", "DirectorPlan", "CinematographyPlan"),
                              strict=True):
            if f'"title": "{title}"' in messages[0]["content"]:
                return json.dumps(next(iter(parts[key].values())))
        return original(messages)

    monkeypatch.setattr(app, "_comic_director_model", model)
    monkeypatch.setattr(app, "_comic_intent_model",
                        semantic_context({"action", "character_context"}))
    second = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "creation_mode": "professional", "creative_operation": "auto",
        "conversation_id": cid, "task": "他后面遇到毁灭博士然后摆出战斗状态",
    })
    # 不得退化为“只改姿态”的续接：新对手必须进入导演方案。
    assert "shot_continuation" not in second
    assert (second["director_spec"]["director_plan"]["character_presence"]
            == "蜘蛛侠与毁灭博士")
