"""Validate selected semantic sources in actual director inputs, not answer appearance."""

from __future__ import annotations

import copy
import json

import pytest
from test_comic_director_quality import SKILLS, _generate, _responses
from test_web_studio import app as app_fixture

from kantoku.config import ToolError
from kantoku.core.conversation_context import MemoryFact
from kantoku.domains.comic.models import CreativeConstraint
from kantoku.shells import web_studio

app = app_fixture


@pytest.fixture(autouse=True)
def memory_only_constraint_boundary(monkeypatch):
    """此组只验收读取/继承；语义归一化由模块一独立用例和付费验收覆盖。"""
    def normalized(facts, *, previous, model_call, on_diagnostics=None, previous_evidence=None):
        return [CreativeConstraint(constraint_id=f"offline-{index}", requirement=fact.value,
            target="当前作品", scope="output", source_ref=fact.source, source_quote=fact.value,
            origin="user_request", trust="source_verified", status=fact.status)
            for index, fact in enumerate(facts) if isinstance(fact, MemoryFact)], {"offline": True}
    monkeypatch.setattr(web_studio, "normalize_constraints", normalized)


def test_screenshot_turns_early_reference_and_multiple_sources_reach_actual_stages(
    app, monkeypatch,
):
    project, first, _ = _generate(app, monkeypatch,
        brief_text="给我生成一个jojo风格的大耳朵图图里面的牛爷爷",
        director_fields={"character_presence": "牛爷爷", "style_boundary": "jojo风格"})
    first_run = app.runtime_store.get_run(first["run_id"])
    cid = first_run.state["conversation_id"]
    source = {"id": first_run.id, "kind": "run", "version": first["director_spec"]["version"]}
    observed = []

    def stage(skill, _inputs, context):
        observed.append(copy.deepcopy(context["context"]))
        parts = _responses()[skill]
        if skill == SKILLS[0]:
            parts["creative_decision"]["intent_summary"] = context["context"]["current_task"]
        if skill == SKILLS[1]:
            parts["director_plan"]["character_presence"] = "欧美少女"
            parts["director_plan"]["style_boundary"] = "明亮写实人像"
        if skill == SKILLS[2]:
            parts["cinematography"].update(shot_size="近景", camera_angle="俯视")
        return parts

    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        "public_summary": "离线契约审核", "confidence": .9,
        "findings": [], "suggested_patches": []}))

    def send(request, sources, relation="continue"):
        observed.clear()
        inputs = []

        def resolve(messages):
            payload = json.loads(messages[1]["content"])
            inputs.append(payload)
            required = [source for source in sources if source["id"] not in payload["recalled"]
                        or payload["recalled"][source["id"]].get("read_required")]
            if required:
                return json.dumps({"action": "create", "relation": relation, "goal": request,
                    "reads": [{"kind": "run", "id": source["id"]} for source in required]})
            return json.dumps({"action": "create", "relation": relation, "goal": request,
                               "sources": sources, "tasks": [], "reads": []})

        monkeypatch.setattr(app, "_conversation_context_model", resolve)
        result = app.create_comic_director(project, {
            "expected_project_version": app.comic_projects.get(project).project.current_version,
            "creation_mode": "professional", "conversation_id": cid,
            "task": request, "independent_plan": True})
        assert result["status"] == "completed"
        assert result["context"]["resolution"]["sources"] == sources
        assert inputs[0]["current_request"] == request
        assert len(observed) == 3
        return result

    phone = send("他拿起手机打电话", [{**source, "fields": [
        "character_context", "world_context", "style_context"]}])
    assert phone["director_spec"]["director_plan"]["character_presence"] == "牛爷爷"
    assert phone["director_spec"]["director_plan"]["style_boundary"] == "jojo风格"
    assert phone["director_spec"]["cinematography"]["shot_size"] == "近景"
    assert first["director_spec"]["cinematography"]["shot_size"] != "近景"
    phone_run = app.runtime_store.get_run(phone["run_id"])
    bound = app.runtime_store.list_conversation_messages(cid,
        message_ids=[phone_run.state["creative_context"]["input_message_id"]])[0]
    assert bound.run_id == phone_run.id
    girl = send("我想要一张欧美清纯少女的图片", [], relation="new")
    assert girl["director_spec"]["director_plan"]["character_presence"] == "欧美少女"
    assert girl["director_spec"]["director_plan"]["style_boundary"] == "明亮写实人像"
    assert observed[0]["creative_context_lock"]["bibles"] == {}
    assert "confirmed_director_decision" not in json.dumps(observed[0].get(
        "conversation_context", {}))
    combined = send("人物用第一版，机位用打电话的方案，风格重新选择", [
        {**source, "fields": ["character_context"]},
        {"id": phone["run_id"], "kind": "run", "version": phone["director_spec"]["version"],
         "fields": ["camera_style"]}])
    assert combined["director_spec"]["director_plan"]["character_presence"] == "牛爷爷"
    assert combined["director_spec"]["cinematography"]["shot_size"] == "近景"
    assert combined["director_spec"]["director_plan"]["style_boundary"] == "明亮写实人像"
    assert app.comic_projects.get_director(project).version == first["director_spec"]["version"]
    partial = send("只引用最早方案的机位角度，其他重新设计", [
        {**source, "fields": ["cinematography.camera_angle"]}])
    assert partial["director_spec"]["cinematography"]["camera_angle"] == (
        first["director_spec"]["cinematography"]["camera_angle"])
    assert partial["director_spec"]["cinematography"]["shot_size"] == "近景"
    assert partial["director_spec"]["director_plan"]["character_presence"] == "欧美少女"

    request = {"expected_project_version": app.comic_projects.get(project).project.current_version,
               "creation_mode": "professional", "conversation_id": cid,
               "task": "新的独立方案", "independent_plan": True, "request_id": "bound-request"}
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "new", "goal": request["task"]}))
    original = app.create_comic_director(project, request)
    observed.clear()
    assert app.create_comic_director(project, request)["run_id"] == original["run_id"]
    assert not observed
    with pytest.raises(ToolError, match="原请求"):
        app.create_comic_director(project, {**request, "creation_mode": "fast"})


def test_cross_chat_source_and_malformed_resolution_fail_without_changing_old_plan(
    app, monkeypatch,
):
    project, first, _ = _generate(app, monkeypatch)
    source_run = app.runtime_store.get_run(first["run_id"])
    cid = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    before = app.comic_projects.get(project)
    for response in ("invalid json", json.dumps({"action": "create", "relation": "continue",
        "goal": "沿用", "sources": [{"id": source_run.id, "kind": "run", "version": 1,
                                      "fields": ["character_context"]}]})):
        monkeypatch.setattr(app, "_conversation_context_model", lambda _, raw=response: raw)
        with pytest.raises((ToolError, ValueError)):
            app.create_comic_director(project, {
                "expected_project_version": before.project.current_version,
                "creation_mode": "professional", "conversation_id": cid,
                "task": "沿用", "independent_plan": True})
        assert app.comic_projects.get(project) == before
    assert len(app.runtime_store.list_conversation_messages(cid)) == 2


def test_independent_plan_preserves_explicit_asset_versions(app, monkeypatch):
    from test_comic_assets import _character

    project, first, _ = _generate(app, monkeypatch)
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    original = app.comic_assets.create(project, _character().model_copy(
        update={"fixed_constraints": []}),
        expected_project_version=app.comic_projects.get(project).project.current_version)
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "new", "goal": "用所选资产创作新方案"}))
    result = app.create_comic_director(project, {
        "expected_project_version": app.comic_projects.get(project).project.current_version,
        "creation_mode": "professional", "conversation_id": cid,
        "independent_plan": True, "task": "用所选资产创作新方案",
        "asset_ids": [original.asset_id]})
    run = app.runtime_store.get_run(result["run_id"])
    assert run.state["project_id"] != project
    copied = app.comic_assets.list(run.state["project_id"])
    assert len(copied) == 1 and copied[0].details == original.details
    assert run.state["input_versions"][f"asset:{copied[0].asset_id}"] == copied[0].version
    assert app.comic_assets.get(project, original.asset_id) == original


def test_first_turn_verified_user_constraint_reaches_the_actual_stage(app, monkeypatch):
    conversation = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})
    request = "人物的衣服必须绿色"
    project = app.create_comic_project({"title": request})["project"]["project_id"]
    captured = []
    def resolve(messages):
        payload = json.loads(messages[1]["content"])
        return json.dumps({"action": "create", "relation": "new", "goal": request,
            "current_task_id": "current", "tasks": [{"task_id": "current", "goal": request,
                "status": "active", "facts": [{"value": "绿色", "origin": "user_request",
                    "source": {"id": payload["current_message_id"], "kind": "message"}}]}]})
    def stage(_skill, _inputs, context):
        captured.append(context["context"])
        raise ToolError("离线测试在记录实际输入后停止，不生成方案")
    monkeypatch.setattr(app, "_conversation_context_model", resolve)
    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    with pytest.raises(ToolError, match="记录实际输入"):
        app.create_comic_director(project, {
            "expected_project_version": 1, "creation_mode": "professional",
            "conversation_id": conversation["id"], "independent_plan": True, "task": request})
    assert captured[0]["creative_brief"]["hard_constraints"] == ["绿色"]
    assert app.comic_projects.get(project).creative_brief.original_request == request


def test_explicit_historical_v1_camera_reference_does_not_invent_other_parameters(app, monkeypatch):
    from test_comic_director import _draft

    _initial_project, first, _ = _generate(app, monkeypatch)
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    project = app.create_comic_project({"title": "旧版方案"})["project"]["project_id"]
    legacy = app.comic_projects.save_director(project, _draft().model_copy(update={
        "constraints": []}), expected_project_version=1, source="model")
    legacy_run = app.runtime_store.create_run("comic", "comic.director-spec", {
        "project_id": project, "conversation_id": cid, "task": "旧版方案",
        "director_spec_id": legacy.spec_id, "director_spec_version": legacy.version,
    }, "director_spec")
    source = {"id": legacy_run.id, "kind": "run", "version": legacy.version,
              "fields": ["camera_style"]}
    def resolve(messages):
        payload = json.loads(messages[1]["content"])
        read = payload["recalled"].get(source["id"], {})
        return json.dumps({"action": "create", "relation": "continue", "goal": "使用旧机位语言",
            "reads": [{"kind": "run", "id": source["id"]}]} if not read or read.get("read_required")
            else {"action": "create", "relation": "continue", "goal": "使用旧机位语言",
                  "sources": [source]})
    monkeypatch.setattr(app, "_conversation_context_model", resolve)
    result = app.create_comic_director(project, {
        "expected_project_version": app.comic_projects.get(project).project.current_version,
        "creation_mode": "professional", "conversation_id": cid,
        "independent_plan": True, "task": "使用旧机位语言，景别和光线重新设计"})
    retained = app.runtime_store.get_run(result["run_id"]).state["creative_context"][
        "retained_director_fields"]
    assert retained == {"cinematography.camera_language": _draft().camera_language}


def test_semantic_resource_selection_reads_description_and_excludes_unselected_style(
    app, monkeypatch,
):
    from test_comic_assets import _character, _style

    cid = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})["id"]
    task = "让银发弓手抬头看向天空"
    project = app.create_comic_project({"title": "目录测试", "brief": {
        "original_request": task}})["project"]["project_id"]
    draft = _character().model_copy(update={"name": "档案甲", "aliases": [],
        "fixed_constraints": [], "details": _character().details.model_copy(
            update={"appearance": "银发弓手"})})
    character = app.comic_assets.create(project, draft, expected_project_version=1)
    app.comic_assets.create(project, _style(), expected_project_version=2)
    calls, captured = [], []
    def resolve(messages):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        if not payload["recalled"]:
            candidate = next(item for item in payload["available"] if item.get("label") == "档案甲")
            assert "银发弓手" in candidate["description"]
            assert "档案甲" not in task
            return json.dumps({"action": "create", "relation": "new", "goal": task,
                               "sources": [{"kind": "resource", "id": candidate["id"]}]})
        source = next(iter(payload["recalled"].values()))
        assert source["content"]["details"]["appearance"] == "银发弓手"
        return json.dumps({"action": "create", "relation": "new", "goal": task,
            "sources": [{"kind": "resource", "id": source["id"]}]})
    def stage(_skill, _inputs, context):
        captured.append(context["context"])
        raise ToolError("离线记录实际资产输入后停止")
    monkeypatch.setattr(app, "_conversation_context_model", resolve)
    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    with pytest.raises(ToolError, match="记录实际资产输入"):
        app.create_comic_director(project, {"expected_project_version": 3,
            "conversation_id": cid, "creation_mode": "professional", "independent_plan": True,
            "task": task})
    assert len(calls) == 2
    assert [item["asset_id"] for item in captured[0]["relevant_assets"]] == [character.asset_id]
    assert captured[0]["relevant_assets"][0]["version"] == character.version


def test_resource_reading_is_scoped_and_cannot_read_future_assets(app, monkeypatch):
    from test_comic_assets import _character

    from kantoku.core.conversations import MessageRole, MessageType

    cid = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})["id"]
    incoming = app.runtime_store.add_conversation_message(cid, role=MessageRole.USER,
        type=MessageType.TEXT, content="沿用角色")
    project = app.create_comic_project({"title": "资产范围"})["project"]["project_id"]
    asset = app.comic_assets.create(project, _character(), expected_project_version=1)
    source = {"kind": "resource", "id": f"asset:{project}:{asset.asset_id}:{asset.version}",
              "version": asset.version}
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "continue", "goal": "沿用角色", "sources": [source]}))
    with pytest.raises(ToolError, match="晚于本轮"):
        app._prepare_conversation_context(cid, incoming.content, through_message_id=incoming.id,
                                        project_snapshot=app.comic_projects.get(project))
    other = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})["id"]
    with pytest.raises(ToolError, match="不属于本轮"):
        app._prepare_conversation_context(other, "沿用其他聊天的角色")


def test_verified_resources_remain_readable_in_the_same_chat_after_project_switch(app, monkeypatch):
    from test_comic_assets import _character

    from kantoku.core.conversations import MessageRole, MessageType
    from kantoku.shells.conversation_router import conversation_memory
    from kantoku.shells.web_studio import get_settings

    cid = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})["id"]
    project = app.create_comic_project({"title": "角色来源"})["project"]["project_id"]
    asset = app.comic_assets.create(project, _character(), expected_project_version=1)
    source = {"kind": "resource", "id": f"asset:{project}:{asset.asset_id}:{asset.version}",
              "version": asset.version}
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "chat", "relation": "recall", "goal": "核对之前的角色", "sources": [source]}))
    first = app.runtime_store.add_conversation_message(cid, role=MessageRole.USER,
        type=MessageType.TEXT, content="查看角色")
    prepared = app._prepare_conversation_context(cid, first.content, through_message_id=first.id,
                                               project_snapshot=app.comic_projects.get(project))
    conversation_memory(app.runtime_store, get_settings().runtime).remember(cid, prepared, first.id)
    second = app.runtime_store.add_conversation_message(cid, role=MessageRole.USER,
        type=MessageType.TEXT, content="核对之前的角色")
    prepared = app._prepare_conversation_context(cid, second.content, through_message_id=second.id)
    assert prepared.evidence[source["id"]]["content"]["asset_id"] == asset.asset_id


def test_director_stages_receive_only_current_active_facts_without_history_envelope(
    app, monkeypatch,
):
    from kantoku.core.conversations import MessageRole, MessageType

    project, first, _ = _generate(app, monkeypatch)
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    unrelated = app.runtime_store.add_conversation_message(cid, role=MessageRole.USER,
        type=MessageType.TEXT, content="另一任务设定在火星")
    captured = []
    def resolve(messages):
        payload = json.loads(messages[1]["content"])
        current = {"kind": "message", "id": payload["current_message_id"]}
        return json.dumps({"action": "create", "relation": "new", "goal": "新人物用近景",
            "current_task_id": "current", "tasks": [
                {"task_id": "other", "goal": "火星任务", "status": "pending", "facts": [{
                    "value": "火星", "origin": "user_request",
                    "source": {"kind": "message", "id": unrelated.id}}]},
                {"task_id": "current", "goal": "新人物用近景", "status": "active", "facts": [
                    {"value": "近景", "origin": "user_request", "source": current},
                    {"value": "俯视", "origin": "user_request", "source": current,
                     "status": "withdrawn"}]}]})
    def stage(_skill, _inputs, context):
        captured.append(context["context"])
        raise ToolError("离线记录当前事实后停止")
    monkeypatch.setattr(app, "_conversation_context_model", resolve)
    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    with pytest.raises(ToolError, match="记录当前事实"):
        app.create_comic_director(project, {
            "expected_project_version": app.comic_projects.get(project).project.current_version,
            "conversation_id": cid, "creation_mode": "professional", "independent_plan": True,
            "task": "新人物用近景，撤回俯视要求"})
    memory = captured[0]["conversation_context"]
    assert [fact["value"] for fact in memory["active_facts"]] == ["近景"]
    assert "tasks" not in memory["resolution"]
    assert "request" not in memory
    assert "火星" not in json.dumps(captured, ensure_ascii=False)


def test_director_identity_cannot_change_between_reading_and_inheritance(app, monkeypatch):
    project, first, _ = _generate(app, monkeypatch)
    run = app.runtime_store.get_run(first["run_id"])
    source = {"kind": "run", "id": run.id, "version": first["director_spec"]["version"],
              "fields": ["character_context"]}
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "continue", "goal": "继续同一人物", "sources": [source]}))
    original = app._prepare_conversation_context
    def change_after_read(*args, **kwargs):
        prepared = original(*args, **kwargs)
        app.runtime_store.update_run(run.id, status=run.status,
            state={**run.state, "director_spec_id": "different-spec-with-the-same-version"},
            current_node=run.current_node)
        return prepared
    monkeypatch.setattr(app, "_prepare_conversation_context", change_after_read)
    before = app.comic_projects.get(project)
    with pytest.raises(ToolError, match="身份或修订发生变化"):
        app.create_comic_director(project, {
            "expected_project_version": before.project.current_version,
            "conversation_id": run.state["conversation_id"], "creation_mode": "professional",
            "independent_plan": True, "task": "继续同一人物"})
    assert app.comic_projects.get(project) == before


def test_context_calls_disable_hidden_retries_and_restore_the_calling_policy(app, monkeypatch):
    from kantoku.shells.web_studio import _comic_single_attempt

    original = _comic_single_attempt.get()
    observed = []
    def model(_messages):
        observed.append(_comic_single_attempt.get())
        raise ToolError("模型结果未知")
    monkeypatch.setattr(app, "_comic_director_model", model)
    with pytest.raises(ToolError, match="结果未知"):
        type(app)._conversation_context_model(app, [])
    assert observed == [True]
    assert _comic_single_attempt.get() == original


def test_overlapping_selected_sources_return_clarification_without_changing_plans(app, monkeypatch):
    project, first, _ = _generate(app, monkeypatch)
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    other = app.create_comic_project({"title": "另一人物方案", "brief": {
        "original_request": "另一人物方案"}})["project"]["project_id"]
    second = app.create_comic_director(other, {"expected_project_version": 1,
        "creation_mode": "professional", "conversation_id": cid, "task": "另一人物方案"})
    sources = [{"kind": "run", "id": result["run_id"],
                "version": result["director_spec"]["version"], "fields": fields}
               for result, fields in ((first, ["character_context"]),
                                      (second, ["director_plan.character_presence"]))]
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "continue", "goal": "沿用人物", "sources": sources}))
    before = [app.comic_projects.get(identifier) for identifier in (project, other)]
    response = app.create_comic_director(project, {
        "expected_project_version": before[0].project.current_version,
        "conversation_id": cid, "creation_mode": "professional", "independent_plan": True,
        "task": "沿用两个人物方案的身份"})
    assert response["conversation_response"]["action"] == "clarify"
    assert "哪个方案" in response["conversation_response"]["content"]
    assert [app.comic_projects.get(identifier) for identifier in (project, other)] == before


@pytest.fixture
def bare_leaf_source_pair(app, monkeypatch):
    """Two real versions with different character choices and no production data."""
    project, first, _ = _generate(app, monkeypatch,
        director_fields={"character_presence": "源一人物穿灰色长袍"})
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    captured = []
    phase = {"value": "source"}

    def stage(skill, _inputs, context):
        captured.append(copy.deepcopy(context["context"]))
        parts = _responses()[skill]
        if skill == SKILLS[0]:
            parts["creative_decision"].update(
                intent_summary=context["context"]["current_task"],
                hard_constraints=context["context"]["creative_brief"]["hard_constraints"])
        if skill == SKILLS[1]:
            parts["director_plan"].update(
                character_presence=("源二陶瓷机器人" if phase["value"] == "source"
                                    else "本轮新选人物"),
                style_boundary="本轮选择的视觉风格", character_pose="本轮独立动作")
        return parts

    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    other = app.create_comic_project({"title": "第二个来源", "brief": {
        "original_request": "第二个来源"}})["project"]["project_id"]
    second = app.create_comic_director(other, {"expected_project_version": 1,
        "creation_mode": "professional", "conversation_id": cid, "task": "第二个来源"})
    assert first["director_spec"]["director_plan"]["character_presence"] != (
        second["director_spec"]["director_plan"]["character_presence"])
    phase["value"] = "current"
    captured.clear()
    return project, cid, first, second, captured


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("retain_world", [False, True])
def test_unknown_bare_leaf_overlap_is_projected_before_conflicts_and_public_context(
    app, monkeypatch, bare_leaf_source_pair, reverse, retain_world,
):
    from kantoku.core.runtime.models import ArtifactType
    from kantoku.domains.comic.context_declaration import EDIT_SCOPE_KEYS

    project, cid, first, second, captured = bare_leaf_source_pair
    fields = ["character_presence", "style_boundary", "color_strategy", "lighting"]
    if retain_world:
        fields.append("world_context")
    sources = [{"kind": "run", "id": result["run_id"],
                "version": result["director_spec"]["version"], "fields": selected}
               for result, selected in ((first, fields),
                   (second, ["character_presence", "character_pose"]))]
    if reverse:
        sources.reverse()
    request = "描绘几只箭射向主角的特写，体现速度、杀伤性和突然感"
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "continue", "goal": request, "sources": sources}))
    before = [app.comic_projects.get(identifier) for identifier in
              (project, second["director_spec"]["project_id"])]
    response = app.create_comic_director(project, {
        "expected_project_version": before[0].project.current_version,
        "conversation_id": cid, "creation_mode": "professional", "independent_plan": True,
        "request_id": f"bare-overlap-{reverse}-{retain_world}", "task": request})
    assert response.get("status") == "completed", response.get("conversation_response")
    expected = [{**source, "fields": [field for field in source["fields"]
        if field in {"lighting", "world_context"}]} for source in sources]
    assert response["context"]["resolution"]["sources"] == expected
    run = app.runtime_store.get_run(response["run_id"])
    assert run.state["creative_context"]["resolution"]["sources"] == expected
    remembered = next(item for item in app.runtime_store.list_artifacts(
        conversation_id=cid, type=ArtifactType.JSON, include_internal=True)
        if item.node_id == "conversation.context" and item.metadata.get(
            "through_message_id") == response["context"]["input_message_id"])
    assert remembered.metadata["resolution"]["sources"] == expected
    assert response["context"]["edit_scopes"] == sorted(
        EDIT_SCOPE_KEYS - {field for source in expected for field in source["fields"]})
    assert {(item["source_run_id"], item["field"]) for item in response["context"][
        "dropped_inherited_fields"]} == {(source["id"], field) for source in sources
            for field in source["fields"] if field not in {"lighting", "world_context"}}
    assert len(captured) == 3
    for context in captured:
        assert context["conversation_context"]["resolution"]["sources"] == expected
        bibles = context["creative_context_lock"]["bibles"]
        assert bibles["lighting"] == first["director_spec"]["cinematography"]["lighting"]
        assert ("world_context" in bibles) is retain_world
        assert not any(field in bibles for field in ("character_presence", "style_boundary",
                                                    "color_strategy", "character_pose"))
    assert response["director_spec"]["director_plan"]["character_presence"] == "本轮新选人物"
    assert response["director_spec"]["director_plan"]["character_pose"] == "本轮独立动作"
    assert [app.comic_projects.get(identifier) for identifier in
            (project, second["director_spec"]["project_id"])] == before


def test_unknown_only_source_identity_is_still_checked_after_field_projection(
    app, monkeypatch, bare_leaf_source_pair,
):
    project, cid, first, second, captured = bare_leaf_source_pair
    sources = [{"kind": "run", "id": result["run_id"],
                "version": result["director_spec"]["version"], "fields": fields}
               for result, fields in ((first, ["character_presence", "lighting"]),
                                      (second, ["character_presence", "character_pose"]))]
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "continue", "goal": "继续当前场景", "sources": sources}))
    prepare = app._prepare_conversation_context

    def changed_after_read(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        source = app.runtime_store.get_run(second["run_id"])
        app.runtime_store.update_run(source.id, status=source.status,
            state={**source.state, "director_spec_id": "wrong-identity-same-version"},
            current_node=source.current_node)
        return prepared

    monkeypatch.setattr(app, "_prepare_conversation_context", changed_after_read)
    before = app.comic_projects.get(project)
    with pytest.raises(ToolError, match="身份或修订发生变化"):
        app.create_comic_director(project, {
            "expected_project_version": before.project.current_version,
            "conversation_id": cid, "creation_mode": "professional", "independent_plan": True,
            "task": "继续当前场景"})
    assert app.comic_projects.get(project) == before
    assert not captured


def test_duplicate_fields_from_one_revision_keep_other_fields_without_mixing_versions(
    app, monkeypatch, bare_leaf_source_pair,
):
    project, cid, first, _second, captured = bare_leaf_source_pair
    source = {"kind": "run", "id": first["run_id"],
              "version": first["director_spec"]["version"]}
    sources = [{**source, "fields": ["lighting"]},
               {**source, "fields": ["lighting", "world_context"]}]
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "continue", "goal": "沿用光线和场景", "sources": sources}))
    before = app.comic_projects.get(project)
    body = {"expected_project_version": before.project.current_version,
        "conversation_id": cid, "creation_mode": "professional", "independent_plan": True,
        "task": "只沿用同一方案的光线和场景，其他重新选择"}
    response = app.create_comic_director(project, body)
    assert response.get("status") == "completed", response.get("conversation_response")
    expected = [{**source, "fields": ["lighting"]}, {**source, "fields": ["world_context"]}]
    assert response["context"]["resolution"]["sources"] == expected
    assert len(captured) == 3
    for context in captured:
        assert context["conversation_context"]["resolution"]["sources"] == expected
        assert set(context["creative_context_lock"]["bibles"]) == {"lighting", "world_context"}
    assert response["director_spec"]["cinematography"]["lighting"] == (
        first["director_spec"]["cinematography"]["lighting"])
    assert response["director_spec"]["director_plan"]["subject_environment_relation"] == (
        first["director_spec"]["director_plan"]["subject_environment_relation"])
    sources[1]["version"] += 1
    captured.clear()
    runs_before = {run.id for run in app.runtime_store.list_runs(domain="comic", limit=1000)}
    with pytest.raises(ToolError):
        app.create_comic_director(project, body)
    assert {run.id for run in app.runtime_store.list_runs(
        domain="comic", limit=1000)} == runs_before
    assert app.comic_projects.get(project) == before
    assert not captured


def test_large_verified_source_reaches_director_as_selected_fields_only(app, monkeypatch):
    from kantoku.core.conversation_context import token_size

    unused_background = "离线容量背景说明。" * 380
    project, first, _ = _generate(app, monkeypatch, brief_text="创建机器人画面",
        director_fields={"character_presence": "金属侦察机器人",
                         "visual_strategy": unused_background,
                         "composition_strategy": unused_background})
    run = app.runtime_store.get_run(first["run_id"])
    source = {"kind": "run", "id": run.id, "version": first["director_spec"]["version"],
              "fields": ["character_context"]}
    interpreted, observed = [], []
    def resolve(messages):
        payload = json.loads(messages[1]["content"])
        interpreted.append(payload)
        assert token_size(messages) <= 18000
        if len(interpreted) == 1:
            return json.dumps({"action": "create", "relation": "continue",
                               "goal": "尚未核对人物", "sources": [source]})
        body = payload["recalled"][run.id]
        assert token_size(body) > 6000
        assert body["director"]["director_plan"]["character_presence"] == "金属侦察机器人"
        return json.dumps({"action": "create", "relation": "continue",
                           "goal": "金属侦察机器人整理工具", "sources": [source]})
    def stage(_skill, _inputs, context):
        observed.append(context["context"])
        raise ToolError("离线记录导演输入后停止")
    monkeypatch.setattr(app, "_conversation_context_model", resolve)
    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    with pytest.raises(ToolError, match="记录导演输入"):
        app.create_comic_director(project, {
            "expected_project_version": app.comic_projects.get(project).project.current_version,
            "conversation_id": run.state["conversation_id"], "creation_mode": "professional",
            "independent_plan": True, "task": "沿用之前的角色，动作改为整理工具，其他重新设计"})
    assert len(interpreted) == 2
    assert observed[0]["conversation_context"]["resolution"]["goal"] == "金属侦察机器人整理工具"
    assert observed[0]["creative_context_lock"]["bibles"] == {"character_context": "金属侦察机器人"}
    assert unused_background not in json.dumps(observed, ensure_ascii=False)
