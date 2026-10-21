"""Work/task organization contracts, using the real stores and no paid model."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_comic_stories import _draft, _select, _upload
from test_comic_stories import story_app as story_app_fixture

from kantoku.config import ToolError

story_app = story_app_fixture


def work(app, title="A work"):
    return app.comic_workspace.create({"request_id": uuid4().hex, "title": title})


def test_screenshot_followups_correct_fact_origin_and_keep_selected_pose(story_app, monkeypatch):
    from test_comic_director_quality import SKILLS, _responses

    from kantoku.shells import web_studio

    app, parent, selected, observed, repairs = story_app, work(story_app), [], [], []
    turns = [
        "给我制作一张水墨中国武侠风 ，周围的背景是树林中间有一个湖泊 "
        "主角就在一个小船上带着斗笠钓鱼 旁边放着他的配件这个是一个远景要包括环境和主角的主体",
        "这个镜头主角保持一样的姿势但是镜头要贴近要让镜头把他的脸看清 "
        "整体主角呈现出悠闲的感觉 但是形象透露出一股江湖侠客的沧桑",
        "镜头稍微后退，沿用同一位侠客和小船，保持刚才的姿势和水墨风格",
        "保持人物和场景不变，只把光线变成傍晚",
    ]

    def understand(messages):
        payload = json.loads(messages[1]["content"])
        if '"FactCorrections"' in messages[0]["content"]:
            repairs.append(payload)
            return json.dumps({"corrections": [{"task_index": 0, "fact_index": 0,
                "fact": {**payload["tasks"][0]["facts"][0], "origin": "model_choice"}}]})
        text = payload["current_request"]
        facts = [{"value": "戴斗笠的垂钓侠客", "source": selected[0],
                  "origin": "user_request"}] if selected else []
        facts.append({"value": text, "source": {"kind": "message",
            "id": payload["current_message_id"]}, "origin": "user_request"})
        return json.dumps({"action": "create", "relation": "continue" if selected else "new",
            "goal": text, "sources": selected, "current_task_id": "fishing",
            "tasks": [{"task_id": "fishing", "goal": text, "status": "active", "facts": facts}]})

    def stage(skill, _inputs, context):
        observed.append(context["context"])
        output = _responses()[skill]
        if skill == SKILLS[0]:
            output["creative_decision"].update(intent_summary=context["context"]["current_task"],
                hard_constraints=context["context"]["creative_brief"]["hard_constraints"])
        if skill == SKILLS[1]:
            output["director_plan"].update(character_presence="戴斗笠的垂钓侠客",
                character_pose="端坐小船持竿垂钓",
                subject_environment_relation="树林环抱湖泊，小船在湖中",
                style_boundary="中国水墨武侠风格")
        return output

    monkeypatch.setattr(app, "_conversation_context_model", understand)
    monkeypatch.setattr(web_studio, "normalize_constraints", lambda *_a, **_kw: ([], {}))
    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        "public_summary": "离线契约检查", "confidence": .9,
        "findings": [], "suggested_patches": []}))
    results = []
    for text in turns:
        observed.clear()
        current = app.comic_workspace.get(parent["work_id"])
        body = {"request_id": uuid4().hex, "expected_project_version": current["version"],
            "conversation_id": current["conversation_id"], "creation_mode": "professional",
            "independent_plan": True, "task": text}
        result = app.comic_work_turn(parent["work_id"], body)
        assert result["status"] == "completed"
        assert app.comic_work_turn_status(
            parent["work_id"], body["request_id"])["phase"] == "completed"
        count = len(repairs)
        assert app.comic_work_turn(parent["work_id"], body)["run_id"] == result["run_id"]
        assert len(repairs) == count
        if selected:
            assert any(context.get("creative_context_lock", {}).get("bibles", {}).get(
                "director_plan.character_pose") == "端坐小船持竿垂钓" for context in observed)
            state = app.runtime_store.get_run(result["run_id"]).state
            resolution = state["creative_context"]["resolution"]
            facts = resolution["tasks"][0]["facts"]
            assert facts[0]["origin"] == "model_choice"
            assert facts[1]["origin"] == "user_request" and facts[1]["value"] == text
        selected[:] = [{"kind": "run", "id": result["run_id"],
            "version": result["director_spec"]["version"],
            "fields": ["character_context", "world_context", "style_context",
                       "director_plan.character_pose"]}]
        results.append(result)
    assert len(repairs) == len(turns) - 1
    assert len({result["task_id"] for result in results}) == len(turns)


def test_pre_run_failure_is_queryable_and_replay_never_calls_the_model(story_app, monkeypatch):
    from kantoku.core.runtime.models import ArtifactType

    app, parent, calls = story_app, work(story_app), []

    def rejected(*_args, **_kwargs):
        calls.append(True)
        raise ToolError("离线来源核验失败")

    monkeypatch.setattr(app, "_prepare_conversation_context", rejected)
    body = {"request_id": uuid4().hex, "expected_project_version": parent["version"],
        "conversation_id": parent["conversation_id"], "creation_mode": "professional",
        "independent_plan": True, "task": "保持原姿势，镜头贴近"}
    with pytest.raises(ToolError) as failure:
        app.comic_work_turn(parent["work_id"], body)
    saved_error = failure.value._kantoku_public_failure
    status = app.comic_work_turn_status(parent["work_id"], body["request_id"])
    assert status == {"phase": "failed", "result": None, "error": saved_error}
    with pytest.raises(ToolError) as repeated:
        app.comic_work_turn(parent["work_id"], body)
    assert repeated.value._kantoku_public_failure == saved_error
    assert calls == [True]
    assert app.comic_workspace.get(parent["work_id"])["tasks"] == []
    assert len(app.runtime_store.list_conversation_messages(parent["conversation_id"])) == 1
    records = app.runtime_store.list_artifacts(conversation_id=parent["conversation_id"],
        type=ArtifactType.JSON, include_internal=True)
    assert len([item for item in records if item.node_id == "conversation.request"]) == 1
    assert app.runtime_store.list_artifacts(conversation_id=parent["conversation_id"]) == []


def task(app, parent, requirement="One image"):
    return app.comic_workspace.create_task(
        parent["work_id"],
        {
            "request_id": uuid4().hex,
            "expected_version": app.comic_workspace.get(parent["work_id"])["version"],
            "title": requirement,
            "requirement": requirement,
        },
    )


def story_request(app, parent):
    state = app.comic_work_story(parent["work_id"])
    return {
        "request_id": uuid4().hex,
        "expected_project_version": app.comic_workspace.get(parent["work_id"])["version"],
        "expected_story_version": state["story_version"],
    }


@pytest.mark.parametrize("identity,location", [
    ("戴护目镜的勘探员", "地下机房"), ("白色陶瓷机器人", "环形温室"),
])
def test_adopted_image_survives_long_dialogue_and_only_explicit_leaf_changes(
    story_app, monkeypatch, identity, location,
):
    """Real adoption, immutable sources, director/Prompt and copy output; models may drift."""
    import base64

    from test_comic_director_quality import _responses
    from test_comic_prompt_production import _sections
    from test_comic_stories import _png

    from kantoku.core.conversations import MessageRole, MessageType
    from kantoku.domains.comic.models import ComicPromptContexts
    from kantoku.domains.comic.workspace import ComicWorkspaceService
    from kantoku.shells import web_studio

    app, parent = story_app, work(story_app)
    state = {"sources": [], "changes": [], "text": "", "reset": False, "failure": None,
             "camera_changed": False, "continuity_task_id": None}
    model_calls, repair_calls, delivered = [], [], []
    output = _sections()
    output["character_context"].update(
        identity=identity, clothing="棕色外套", appearance="银色短发")
    output["world_context"].update(location=location, environment="三排管道与一道圆门")
    monkeypatch.setattr(web_studio, "normalize_constraints", lambda *_a, **_kw: ([], {}))
    def understand(_messages):
        model_calls.append("context")
        return json.dumps({"action": "create", "relation": "continue", "goal": state["text"],
                           "sources": state["sources"]})

    monkeypatch.setattr(app, "_conversation_context_model", understand)
    def stage(skill, *_args):
        model_calls.append(skill)
        result = _responses()[skill]
        if skill == "comic.visual_direction" and any(
            item["field"] == "shot_context.action" for item in state["changes"]
        ):
            result["director_plan"]["character_pose"] = state["text"]
        if skill == "comic.cinematography" and state["camera_changed"]:
            result["cinematography"].update(shot_size="特写", camera_angle="平视",
                camera_distance="人物面前一米", camera_language="平视面部特写，固定镜头")
        return result

    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        "public_summary": "离线契约检查", "confidence": .9,
        "findings": [], "suggested_patches": []}))
    def storyboard(_messages):
        model_calls.append("storyboard")
        return json.dumps({"title": "独立画面", "description": "离线分镜", "shots": [{
            "purpose": "呈现本轮变化", "subject": identity, "action": state["text"],
            "environment": "模型试图换到广场", "shot_size": "特写",
            "camera_angle": "平视" if state["camera_changed"] else "俯拍"}]})

    monkeypatch.setattr(app, "_comic_storyboard_model", storyboard)
    continuity_calls = []

    def model(messages):
        model_calls.append("constraint")
        body = json.loads(messages[1]["content"])
        if body.get("stage") == "confirmed_image_continuity":
            continuity_calls.append(body)
            if "repair" in body:
                repair_calls.append(body)
                assert "offline-private-output" not in json.dumps(body)
            decision = {"candidate_id": next(key for key, candidate in body["candidates"].items()
                if candidate["source_task_id"] == state["continuity_task_id"])
                if not state["reset"] else None,
                "changes": [{**change, "quote": body["current_request"]}
                            for change in state["changes"]],
                "new_context_quote": state["text"] if state["reset"] else None}
            if "repair" not in body and state["failure"]:
                if state["failure"] == "json":
                    return "offline-private-output: {invalid JSON"
                decision["changes"][0]["field"] = "character_context"
            return json.dumps(decision)
        if "constraints" in body and "sections" in body:
            return json.dumps({"complete": True, "scope_valid": True, "findings": []})
        assert "required_constraints" in body
        return json.dumps(output, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_constraint_model", model)

    def send(text, changes, *, failure=None):
        state.update(text=text, changes=[{"field": field, "quote": text} for field in changes],
                     failure=failure)
        request = {"request_id": uuid4().hex,
            "expected_project_version": app.comic_workspace.get(parent["work_id"])["version"],
            "conversation_id": parent["conversation_id"], "creation_mode": "professional",
            "independent_plan": True, "task": text}
        result = app.comic_work_turn(parent["work_id"], request)
        assert result["status"] == "completed"
        assert app.comic_work_turn_status(
            parent["work_id"], request["request_id"])["phase"] == "completed"
        called = len(model_calls)
        assert app.comic_work_turn(parent["work_id"], request)["run_id"] == result["run_id"]
        assert len(model_calls) == called
        pid = result["task_id"]
        spec = app.comic_projects.get_director(pid)
        app.confirm_comic_director(pid, {"expected_project_version": app.comic_projects.get(
            pid).project.current_version, "version": spec.version})
        board = app.create_comic_storyboard(pid, {
            "expected_project_version": app.comic_projects.get(pid).project.current_version,
            "generate": True, "task": text})
        shot = board["shots"][0]
        prompt = app.compile_comic_prompt(shot["shot_id"], {
            "expected_project_version": app.comic_projects.get(pid).project.current_version,
            "expected_shot_version": shot["version"], "image_mode": "external",
            "conversation_id": parent["conversation_id"], "complete_prompt": True})
        state["sources"] = [{"kind": "run", "id": result["run_id"], "version": spec.version,
                             "fields": []}]  # Deliberately omit all stable fields.
        state["continuity_task_id"] = pid
        called = len(model_calls)
        assert app.comic_work_turn(parent["work_id"], request)["run_id"] == result["run_id"]
        assert len(model_calls) == called
        delivered.append((result, prompt))
        return result, prompt

    first, original = send(f"{identity}穿棕色外套，在{location}里安静等待", [])
    uploaded = app.import_comic_external_image(original["shot_id"], {
        "expected_project_version": app.comic_projects.get(
            first["task_id"]).project.current_version,
        "expected_shot_version": original["shot_version"],
        "expected_prompt_version": original["version"],
        "conversation_id": parent["conversation_id"],
        "filename": "approved.png", "data_url": "data:image/png;base64,"
        + base64.b64encode(_png()).decode()})
    image = app.comic_media.get(uploaded["artifact_id"]).model_dump(mode="json")
    _select(app, parent["conversation_id"], image)
    # Neither the last-40 Run list nor the recent chat window may be the continuity authority.
    for index in range(65):
        app.runtime_store.create_run(domain="comic", workflow="offline.noise",
            current_node="offline", state={"conversation_id": parent["conversation_id"],
                "task": f"无关进度 {index}"})
        app.runtime_store.add_conversation_message(parent["conversation_id"], role=MessageRole.USER,
            type=MessageType.TEXT, content=f"无关讨论 {index}")
    output["character_context"].update(identity="陌生人物", clothing="白色雨衣", appearance="红发")
    output["world_context"].update(location="城市广场", environment="六层高楼")
    output["style_context"].update(art_style="卡通", color="红色")
    second, changed = send("只把外套换成白色雨衣", ["character_context.clothing"])
    def sections(prompt):
        return ComicPromptContexts.from_sections({
            key: prompt[key] for key in ComicPromptContexts.model_fields},
            semantic_scope=True).model_dump()
    expected, actual = sections(original), sections(changed)
    expected["character_context"]["clothing"] = "白色雨衣"
    assert actual == expected
    assert changed["reference_attachments"][0]["artifact_id"] == image["artifact_id"]
    assert image["sha256"] in changed["final_prompt"]
    assert "只把外套换成白色雨衣" in changed["final_prompt"]
    assert "没有附上这张参考图" in changed["final_prompt"]
    # Simulate reopening the backend service; all anchors are persisted source references.
    app.comic_workspace = ComicWorkspaceService(
        app.comic_media, app.comic_assets, app.comic_prompts)
    output["character_context"]["clothing"] = "模型试图改回棕色外套"
    # The real interpreter selected two verified plans but misspelled their public field paths.
    # Invalid bare leaves are diagnostics, not a conflicting grant of inheritance authority.
    state["sources"] = [
        {"kind": "run", "id": first["run_id"], "version": original["director_spec_version"],
         "fields": ["character_presence", "style_boundary", "color_strategy", "lighting"]},
        {"kind": "run", "id": second["run_id"], "version": changed["director_spec_version"],
         "fields": ["character_presence", "character_pose"]},
    ]
    third, action = send("保持其余元素，抬起左手", ["shot_context.action", "shot_context.pose"])
    selected_sources = continuity_calls[-1]["resolution"]["sources"]
    assert [source["fields"] for source in selected_sources] == [["lighting"], []]
    assert len(continuity_calls[-1]["candidates"]) == 2
    expected["shot_context"]["action"] = "保持其余元素，抬起左手"
    actual = sections(action)
    assert output["shot_context"]["pose"] in actual["shot_context"]["pose"]
    actual["shot_context"].pop("pose")
    expected["shot_context"].pop("pose")
    assert actual == expected
    assert action["reference_attachments"][0]["sha256"] == image["sha256"]
    assert app.comic_projects.get_director(second["task_id"]).reference_context["continuity"]
    assert len(continuity_calls) == 2
    # Continue past the formerly tested two image decisions, accumulating independent changes.
    expected = sections(action)
    for text, field, value, failure in [
        ("只把白色雨衣换成深蓝色外套", "character_context.clothing", "深蓝色外套", "field"),
        ("只把头发改成金属银色短发", "character_context.appearance", "金属银色短发", None),
        ("只把管道减少为两排，保留一道圆门", "world_context.environment",
         "两排管道与一道圆门", "json"),
    ]:
        section, leaf = field.split(".")
        output[section][leaf] = value
        latest, prompt = send(text, [field], failure=failure)
        expected[section][leaf] = value
        assert sections(prompt) == expected
        assert prompt["reference_attachments"][0]["sha256"] == image["sha256"]
        assert identity in prompt["final_prompt"] and location in prompt["final_prompt"]
        assert "深蓝色外套" in prompt["final_prompt"]
        app.comic_workspace = ComicWorkspaceService(
            app.comic_media, app.comic_assets, app.comic_prompts)
        assert app.comic_prompts.get(prompt["shot_id"]).artifact_id == prompt["artifact_id"]
    state["camera_changed"] = True
    output["shot_context"]["camera"] = "平视面部特写"
    _seventh, closeup = send("镜头改为平视面部特写", ["shot_context.camera"])
    camera = sections(closeup)["shot_context"]["camera"]
    assert "平视" in camera and "特写" in camera
    expected["shot_context"]["camera"] = camera
    assert sections(closeup) == expected
    output["shot_context"]["camera"] = "模型试图改回俯拍远景"
    latest, unchanged = send("保留现在的机位、人物及场景，继续这张画面", [])
    assert sections(unchanged) == expected
    assert unchanged["reference_attachments"][0]["artifact_id"] == image["artifact_id"]
    assert len(delivered) == 8 and len(repair_calls) == 2
    assert len({result["task_id"] for result, _prompt in delivered}) == 8
    assert len(continuity_calls) == 9  # Seven decisions and two bounded structure corrections.
    assert app.comic_projects.get_director(latest["task_id"]).reference_context["continuity"]
    # Both natural-language revision and direct user edits update the release permission.
    state["changes"] = [{"field": "character_context.clothing", "quote": "把雨衣换成黑色"}]
    state["continuity_task_id"] = third["task_id"]
    current = app.comic_projects.get_director(third["task_id"])
    edit = {"expected_project_version": app.comic_projects.get(
        third["task_id"]).project.current_version, "expected_director_version": current.version,
        "conversation_id": parent["conversation_id"]}
    if identity == "戴护目镜的勘探员":
        monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
            "changes": {"director_plan.character_presence": identity + "穿黑色雨衣"}}))
        edit["revision_instruction"] = "把雨衣换成黑色"
    else:
        from kantoku.domains.comic.models import DirectorSpecDraft
        draft = current.model_dump(mode="json", include=set(DirectorSpecDraft.model_fields))
        draft["director_plan"]["character_presence"] = identity + "穿黑色雨衣"
        edit["draft"] = draft
    revised = app.create_comic_director(third["task_id"], edit)
    permissions = revised["reference_context"]["continuity"]
    assert permissions["image"]["sha256"] == image["sha256"]
    fields = app.comic_workspace.continuity_fields(parent["work_id"], permissions)
    assert "character_context.clothing" not in fields
    assert fields["character_context.identity"] == identity
    assert not revised["user_confirmed"]
    # A genuinely separate idea clears the old source even if the memory model selected it.
    state["reset"] = True
    state["changes"] = []
    _new, reset = send("另做一张完全独立的火星城市航拍", [])
    assert not reset["reference_attachments"]
    assert "continuity" not in reset["context_sources"].get("image_references", {})
    assert app.comic_prompts.get(original["shot_id"]).artifact_id == original["artifact_id"]
    assert app.comic_projects.get_director(third["task_id"]).reference_context["continuity"]


@pytest.mark.parametrize("failure", ["quote", "field", "unknown", "ambiguous", "transport"])
def test_image_continuity_rejects_unproved_release_and_never_guesses(
    story_app, failure,
):
    from kantoku.core.conversation_context import TurnUnderstanding
    from kantoku.core.runtime.models import utc_now

    app, parent = story_app, work(story_app)
    owner = task(app, parent)
    image = _upload(app, owner["task_id"], owner["conversation_id"])
    _select(app, owner["conversation_id"], image)
    request = "保持人物，只抬起左手"

    def resolve(messages):
        if failure == "transport":
            raise TimeoutError("Offline unknown response")
        body = json.loads(messages[1]["content"])
        return json.dumps({"candidate_id": "invented" if failure == "unknown" else
            next(iter(body["candidates"])),
            "changes": [{"field": "character_context" if failure == "field" else
                "shot_context.pose", "quote": "换掉全部人物" if failure == "quote" else request}],
            "question": "请明确要沿用哪张图片" if failure == "ambiguous" else None})

    with pytest.raises((ToolError, TimeoutError)):
        app.comic_workspace.image_continuity(parent["work_id"], request,
            TurnUnderstanding(action="create", relation="continue", goal=request),
            model_call=resolve, cutoff=utc_now(), budget=18000)
    assert len(app.comic_workspace.get(parent["work_id"])["tasks"]) == 1


def test_image_continuity_schema_names_every_allowed_leaf():
    from pydantic import ValidationError

    from kantoku.domains.comic.models import ComicPromptContexts, ImageContinuityChange

    paths = ImageContinuityChange.model_json_schema()["properties"]["field"]["enum"]
    expected = {f"{section}.{leaf}" for section, body in ComicPromptContexts.model_fields.items()
                for leaf in body.annotation.model_fields}
    assert set(paths) == expected
    for path in paths:
        assert ImageContinuityChange(field=path, quote="本轮用户原话").field == path
    with pytest.raises(ValidationError):
        ImageContinuityChange(field="character_context", quote="本轮用户原话")


@pytest.mark.parametrize("quote", [" ", "\n"])
@pytest.mark.parametrize("kind", ["change", "new_context"])
def test_image_continuity_whitespace_cannot_authorize_changes_or_clear_the_image(
    story_app, quote, kind,
):
    from kantoku.core.conversation_context import TurnUnderstanding
    from kantoku.core.runtime.models import utc_now

    app, parent, calls = story_app, work(story_app), []
    owner = task(app, parent)
    image = _upload(app, owner["task_id"], owner["conversation_id"])
    _select(app, owner["conversation_id"], image)
    request = "请 只抬起左手\n其他元素全部保留"

    def resolve(messages):
        body = json.loads(messages[1]["content"])
        calls.append(body)
        candidate = next(iter(body["candidates"]))
        return json.dumps({"candidate_id": candidate if kind == "change" else None,
            "changes": [{"field": "character_context.clothing", "quote": quote}]
                if kind == "change" else [],
            "new_context_quote": quote if kind == "new_context" else None})

    with pytest.raises(ToolError):
        app.comic_workspace.image_continuity(parent["work_id"], request,
            TurnUnderstanding(action="create", relation="continue", goal=request),
            model_call=resolve, cutoff=utc_now(), budget=18000)
    assert len(calls) == 2
    assert app.comic_media.get(image["image_id"]).sha256 == image["sha256"]


@pytest.mark.parametrize("failure", [
    "persistent", "repair_transport", "candidate_rewrite", "valid_change_rewrite",
    "valid_change_drop", "change_added", "repair_quote", "repair_unknown",
    "partial_field_rewrite", "partial_quote_rewrite",
])
def test_image_continuity_correction_is_bounded_and_cannot_rewrite_verified_decisions(
    story_app, failure,
):
    from kantoku.core.conversation_context import TurnUnderstanding
    from kantoku.core.runtime.models import utc_now

    app, parent, calls = story_app, work(story_app), []
    owner = task(app, parent)
    image = _upload(app, owner["task_id"], owner["conversation_id"])
    _select(app, owner["conversation_id"], image)
    request = "只抬起左手，其他元素全部保留"
    valid = {"field": "shot_context.pose", "quote": "只抬起左手"}

    def resolve(messages):
        body = json.loads(messages[1]["content"])
        calls.append(body)
        candidate = next(iter(body["candidates"]))
        if "repair" not in body:
            first = ({**valid, "offline-private-key": "offline-private-output"}
                     if failure == "partial_field_rewrite" else
                     {"field": "shot_context", "quote": "只抬起左手"}
                     if failure == "partial_quote_rewrite" else valid)
            return json.dumps({"candidate_id": candidate, "changes": [first,
                {"field": "shot_context", "quote": request}],
                "offline-private-key": "offline-private-output"})
        assert "offline-private" not in json.dumps(messages)
        assert body["repair"]["fixed"]["candidate_id"] == candidate
        fixed = body["repair"]["fixed"]["changes"]
        assert fixed[0] == {"index": 0, "change": {"quote": "只抬起左手"}
                            if failure == "partial_quote_rewrite" else valid}
        assert fixed[1] == {"index": 1, "change": {"quote": request}}
        if failure == "repair_transport":
            raise TimeoutError("Offline repair transport unknown")
        if failure == "persistent":
            return "offline-private-output: invalid JSON"
        decision = {"candidate_id": candidate, "changes": [valid,
            {"field": "shot_context.action", "quote": request}]}
        if failure == "candidate_rewrite":
            decision["candidate_id"] = "invented-candidate"
        elif failure in {"valid_change_rewrite", "partial_field_rewrite"}:
            decision["changes"][0] = {"field": "shot_context.camera", "quote": request}
        elif failure == "partial_quote_rewrite":
            decision["changes"][0]["quote"] = request
        elif failure == "valid_change_drop":
            decision["changes"] = decision["changes"][1:]
        elif failure == "change_added":
            decision["changes"].append({"field": "character_context.clothing", "quote": request})
        elif failure == "repair_quote":
            decision["changes"][1]["quote"] = "用户没有说过的修改要求"
        elif failure == "repair_unknown":
            decision["offline-private-key"] = "offline-private-output"
        return json.dumps(decision)

    with pytest.raises((ToolError, TimeoutError)) as caught:
        app.comic_workspace.image_continuity(parent["work_id"], request,
            TurnUnderstanding(action="create", relation="continue", goal=request),
            model_call=resolve, cutoff=utc_now(), budget=18000)
    assert len(calls) == 2
    assert "offline-private" not in str(caught.value)
    assert "offline-private" not in str(getattr(caught.value, "detail", ""))
    assert len(app.comic_workspace.get(parent["work_id"])["tasks"]) == 1
    assert app.comic_media.get(image["image_id"]).sha256 == image["sha256"]


def test_image_continuity_correction_rechecks_capacity_before_resubmitting(story_app, monkeypatch):
    from kantoku.core.conversation_context import TurnUnderstanding
    from kantoku.core.runtime.models import utc_now
    from kantoku.domains.comic import workspace

    app, parent, calls = story_app, work(story_app), []
    owner = task(app, parent)
    image = _upload(app, owner["task_id"], owner["conversation_id"])
    _select(app, owner["conversation_id"], image)
    request = "保持人物，只抬起左手"
    monkeypatch.setattr(workspace, "token_size", lambda messages:
        18001 if "repair" in json.loads(messages[1]["content"]) else 1)

    def resolve(messages):
        calls.append(messages)
        return "offline-private-output: invalid JSON"

    with pytest.raises(ToolError, match="容量"):
        app.comic_workspace.image_continuity(parent["work_id"], request,
            TurnUnderstanding(action="create", relation="continue", goal=request),
            model_call=resolve, cutoff=utc_now(), budget=18000)
    assert len(calls) == 1


@pytest.mark.parametrize("initial", ["empty", "changes_only", "blank_question"])
def test_image_continuity_missing_decision_is_corrected_in_the_same_context(story_app, initial):
    from kantoku.core.conversation_context import TurnUnderstanding
    from kantoku.core.runtime.models import utc_now

    app, parent, calls = story_app, work(story_app), []
    owner = task(app, parent)
    image = _upload(app, owner["task_id"], owner["conversation_id"])
    _select(app, owner["conversation_id"], image)
    request = "沿用当前图片，只抬起左手"
    change = {"field": "shot_context.pose", "quote": "只抬起左手"}

    def resolve(messages):
        body = json.loads(messages[1]["content"])
        calls.append(body)
        if "repair" not in body:
            return json.dumps({"changes": [change]} if initial == "changes_only" else
                              {"question": " \n"} if initial == "blank_question" else {})
        assert body["current_request"] == calls[0]["current_request"]
        assert body["resolution"] == calls[0]["resolution"]
        assert body["candidates"] == calls[0]["candidates"]
        if initial == "changes_only":
            assert body["repair"]["fixed"]["changes"] == [{"index": 0, "change": change}]
        return json.dumps({"candidate_id": next(iter(body["candidates"])), "changes": [change]})

    result = app.comic_workspace.image_continuity(parent["work_id"], request,
        TurnUnderstanding(action="create", relation="continue", goal=request),
        model_call=resolve, cutoff=utc_now(), budget=18000)
    assert len(calls) == 2 and result["image"]["artifact_id"] == image["artifact_id"]
    assert result["changes"] == [change]


@pytest.mark.parametrize("identity", ["candidate_id", "image_id", "artifact_id"])
def test_image_continuity_correction_keeps_explicit_external_edit_among_multiple_candidates(
    story_app, monkeypatch, identity,
):
    from test_comic_image_review import analyze, common, edit, png

    from kantoku.core.conversation_context import TurnUnderstanding
    from kantoku.core.runtime.models import utc_now

    app, parent, calls = story_app, work(story_app), []
    first, second = task(app, parent, "保留首图"), task(app, parent, "另一张外部修改图")
    original = _upload(app, first["task_id"], first["conversation_id"])
    _select(app, first["conversation_id"], original)
    cid = second["conversation_id"]
    base = _upload(app, second["task_id"], cid, content=png(10))
    ref = {"image_id": base["image_id"], "version": base["version"]}
    report, _, _ = analyze(app, second["task_id"], cid, ref, monkeypatch)
    instruction, _, _ = edit(app, second["task_id"], cid, ref, report, monkeypatch)
    app.comic_image_operation(second["task_id"], "confirm", {
        **common(app, second["task_id"], cid, "confirm-external-edit"),
        "instruction_artifact_id": instruction["artifact_id"]})
    edited = _upload(app, second["task_id"], cid, content=png(20), image_id=base["image_id"],
        expected_image_version=1, parent=ref, instruction_artifact_id=instruction["artifact_id"])
    _select(app, cid, edited)
    metadata = app.runtime_store.get_artifact(instruction["artifact_id"]).metadata
    assert "prompt_id" not in metadata and "shot_id" not in metadata
    request = "沿用第二张修改后的图，只抬起左手"

    def resolve(messages):
        body = json.loads(messages[1]["content"])
        calls.append(body)
        assert len(body["candidates"]) == 2
        chosen = next(key for key, item in body["candidates"].items()
                      if item["image"]["artifact_id"] == edited["artifact_id"])
        assert body["candidates"][chosen]["prompt_source"] is None
        assert body["candidates"][chosen]["prompt_sections"] == {}
        if "repair" in body:
            assert body["repair"]["fixed"]["candidate_id"] == chosen
        identifier = chosen if identity == "candidate_id" else edited[identity]
        return json.dumps({"candidate_id": identifier, "changes": [{
            "field": "shot_context.pose" if "repair" in body else "shot_context",
            "quote": "只抬起左手"}]})

    result = app.comic_workspace.image_continuity(parent["work_id"], request,
        TurnUnderstanding(action="create", relation="continue", goal=request),
        model_call=resolve, cutoff=utc_now(), budget=18000)
    assert len(calls) == 2 and result["image"]["artifact_id"] == edited["artifact_id"]
    assert result["image"]["version"] == 2 and result["image"]["sha256"] == edited["sha256"]
    assert result["prompt_source"] is None
    assert app.comic_workspace.continuity_fields(parent["work_id"], result) == {}


def test_continuity_rechecks_selection_content_and_work_ownership(story_app):
    from kantoku.core.artifacts import ArtifactService

    app, parent = story_app, work(story_app)
    owner = task(app, parent)
    image = _upload(app, owner["task_id"], owner["conversation_id"])
    _select(app, owner["conversation_id"], image)
    context = {"image": image, "selection_version": 1}
    app.comic_workspace.require_continuity(parent["work_id"], context)
    other = work(app, "Other work")
    with pytest.raises(ToolError, match="归属"):
        app.comic_workspace.require_continuity(other["work_id"], context)
    _select(app, owner["conversation_id"], image, expected=1)
    with pytest.raises(ToolError, match="版本已变化"):
        app.comic_workspace.require_continuity(parent["work_id"], context)
    context["selection_version"] = 2
    ArtifactService(app.runtime_store).trash(image["artifact_id"], expected_version=1)
    with pytest.raises(ToolError):
        app.comic_workspace.require_continuity(parent["work_id"], context)


@pytest.mark.parametrize("identity", ["image_id", "artifact_id"])
def test_unique_image_identity_selects_the_exact_earlier_candidate_without_another_call(
    story_app, identity,
):
    from kantoku.core.conversation_context import TurnUnderstanding
    from kantoku.core.runtime.models import utc_now

    app, parent, calls = story_app, work(story_app), []
    earlier, later = task(app, parent, "原始画面"), task(app, parent, "另一个画面")
    chosen = _upload(app, earlier["task_id"], earlier["conversation_id"])
    other = _upload(app, later["task_id"], later["conversation_id"])
    for image in (chosen, other):
        _select(app, image["conversation_id"], image)
    request = "沿用原始画面，只抬起左手"

    def resolve(messages):
        body = json.loads(messages[1]["content"])
        calls.append(body)
        assert len(body["candidates"]) == 2 and "repair" not in body
        for key, candidate in body["candidates"].items():
            assert candidate["candidate_id"] == key
        return json.dumps({"candidate_id": chosen[identity], "changes": [{
            "field": "shot_context.pose", "quote": "只抬起左手"}]})

    result = app.comic_workspace.image_continuity(parent["work_id"], request,
        TurnUnderstanding(action="create", relation="continue", goal=request),
        model_call=resolve, cutoff=utc_now(), budget=18000)
    assert len(calls) == 1
    assert result["image"]["artifact_id"] == chosen["artifact_id"]
    assert result["source_task_id"] == earlier["task_id"]


@pytest.mark.parametrize("identity", ["image_id", "artifact_id", "unknown", "candidate_id"])
def test_shared_image_alias_cannot_choose_between_distinct_text_states(story_app, identity):
    app, parent, calls = story_app, work(story_app), []
    owner = task(app, parent)
    image = _upload(app, owner["task_id"], owner["conversation_id"])
    keys = ["original:" + image["artifact_id"], "successor:" + image["artifact_id"]]
    candidates = {key: {"image": image} for key in keys}
    request = "只修改姿态，沿用明确指定的方案"
    messages = [{"role": "system", "content": "Offline continuity contract"},
        {"role": "user", "content": json.dumps({"stage": "confirmed_image_continuity",
            "current_request": request, "candidates": candidates, "resolution": {}})}]

    def resolve(received):
        calls.append(received)
        return json.dumps({"candidate_id": keys[1] if identity == "candidate_id" else
                           "unrelated-artifact" if identity == "unknown" else image[identity]})

    if identity == "candidate_id":
        result = app.comic_workspace._continuity_decision(parent["work_id"], messages,
            request, candidates, model_call=resolve, budget=18000)
        assert result.candidate_id == keys[1]
    else:
        with pytest.raises(ToolError, match="来源无效|多个方案"):
            app.comic_workspace._continuity_decision(parent["work_id"], messages,
                request, candidates, model_call=resolve, budget=18000)
    assert len(calls) == 1


@pytest.mark.parametrize("resolution", [
    "keep", "independent", "other_candidate", "rewrite_quote", "rewrite_change",
    "add_change", "transport", "persistent",
])
def test_conflicting_continuity_branches_are_corrected_once_without_new_authority(
    story_app, resolution,
):
    from kantoku.core.conversation_context import TurnUnderstanding
    from kantoku.core.runtime.models import utc_now

    app, parent, calls = story_app, work(story_app), []
    first, second = task(app, parent, "第一份方案"), task(app, parent, "第二份方案")
    chosen = _upload(app, first["task_id"], first["conversation_id"])
    other = _upload(app, second["task_id"], second["conversation_id"])
    for image in (chosen, other):
        _select(app, image["conversation_id"], image)
    quote = "重新开始独立画面"
    request = "沿用第一张，只抬起左手；另一个备选是" + quote
    change = {"field": "shot_context.pose", "quote": "只抬起左手"}

    def resolve(messages):
        body = json.loads(messages[1]["content"])
        calls.append(body)
        candidate = next(key for key, item in body["candidates"].items()
                         if item["image"]["artifact_id"] == chosen["artifact_id"])
        if "repair" not in body:
            return json.dumps({"candidate_id": chosen["artifact_id"], "changes": [change],
                               "new_context_quote": quote,
                               "offline-private-key": "offline-private-output"})
        assert "offline-private" not in json.dumps(messages)
        assert body["repair"]["alternatives"] == {
            "candidate_id": candidate, "new_context_quote": quote}
        assert body["repair"]["fixed"]["changes"] == [{"index": 0, "change": change}]
        assert body["repair"]["change_count"] == 1
        if resolution == "transport":
            raise TimeoutError("Offline response unknown")
        if resolution == "persistent":
            return json.dumps({"candidate_id": candidate, "changes": [change],
                               "new_context_quote": quote})
        result = ({"candidate_id": None, "changes": [], "new_context_quote": quote}
                  if resolution in {"independent", "rewrite_quote"} else
                  {"candidate_id": candidate, "changes": [dict(change)]})
        if resolution == "other_candidate":
            result["candidate_id"] = next(key for key in body["candidates"] if key != candidate)
        elif resolution == "rewrite_quote":
            result["new_context_quote"] = "另一个备选"
        elif resolution == "rewrite_change":
            result["changes"][0]["field"] = "character_context.clothing"
        elif resolution == "add_change":
            result["changes"].append({"field": "shot_context.camera", "quote": request})
        return json.dumps(result)

    def execute():
        return app.comic_workspace.image_continuity(parent["work_id"], request,
            TurnUnderstanding(action="create", relation="continue", goal=request),
            model_call=resolve, cutoff=utc_now(), budget=18000)

    if resolution in {"keep", "independent"}:
        result = execute()
        if resolution == "independent":
            assert result == {"new_context": True}
        else:
            assert result["image"]["artifact_id"] == chosen["artifact_id"]
            assert result["changes"] == [change]
    else:
        with pytest.raises((ToolError, TimeoutError)):
            execute()
    assert len(calls) == 2
    assert len(app.comic_workspace.get(parent["work_id"])["tasks"]) == 2
    assert app.comic_media.get(chosen["image_id"]).sha256 == chosen["sha256"]


@pytest.mark.parametrize("invalid_candidate", [
    {"offline-private-key": "offline-private-output"}, ["offline-private-output"],
])
def test_non_string_candidate_is_never_forwarded_as_a_verified_alternative(
    story_app, invalid_candidate,
):
    from kantoku.core.conversation_context import TurnUnderstanding
    from kantoku.core.runtime.models import utc_now

    app, parent, calls = story_app, work(story_app), []
    owner = task(app, parent)
    image = _upload(app, owner["task_id"], owner["conversation_id"])
    _select(app, image["conversation_id"], image)
    request = "重新开始一个完全独立的画面"

    def resolve(messages):
        body = json.loads(messages[1]["content"])
        calls.append(body)
        if "repair" not in body:
            return json.dumps({"candidate_id": invalid_candidate, "new_context_quote": request})
        assert "offline-private" not in json.dumps(messages)
        assert "alternatives" not in body["repair"]
        assert body["repair"]["fixed"]["candidate_id"] is None
        return json.dumps({"candidate_id": None, "new_context_quote": request, "changes": []})

    result = app.comic_workspace.image_continuity(parent["work_id"], request,
        TurnUnderstanding(action="create", relation="new", goal=request),
        model_call=resolve, cutoff=utc_now(), budget=18000)
    assert result == {"new_context": True} and len(calls) == 2


def test_work_dialogue_creates_independent_plans_and_replays_original(story_app, monkeypatch):
    from test_comic_director_quality import SKILLS, _responses

    from kantoku.shells import web_studio

    app, selected_sources = story_app, []
    parent = work(app)
    monkeypatch.setattr(web_studio, "normalize_constraints", lambda *_args, **_kw: ([], {}))
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "continue", "goal": "樱花少女画面",
        "sources": selected_sources, "tasks": [], "reads": []}))
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        "public_summary": "离线契约检查", "confidence": .9,
        "findings": [], "suggested_patches": []}))

    def stage(skill, _inputs, context):
        output = _responses()[skill]
        text = context["context"]["current_task"]
        if skill == SKILLS[0]:
            output["creative_decision"].update(intent_summary=text, narrative_context=text)
        if skill == SKILLS[1]:
            output["director_plan"].update(
                character_presence="日式少女，身穿水手服",
                subject_environment_relation="晴天盛开的樱花树下",
                character_pose="惊喜回眸朝我看过来" if "叫" in text else "背对镜头抬头看樱花")
        return output

    monkeypatch.setattr(app.comic_director, "stage_executor", stage)

    def send(text):
        current = app.comic_workspace.get(parent["work_id"])
        request = {"request_id": uuid4().hex, "expected_project_version": current["version"],
                   "conversation_id": current["conversation_id"], "creation_mode": "professional",
                   "independent_plan": True, "task": text}
        return request, app.comic_work_turn(parent["work_id"], request)

    body, first = send("日式少女穿水手服，晴天樱花树下背对我抬头看樱花")
    selected_sources.append({"kind": "run", "id": first["run_id"], "version": 1,
                             "fields": ["character_context", "world_context", "style_context"]})
    _, second = send("然后我叫了一声，她充满惊喜地回眸朝我看过来")
    _, third = send("她拿起手机拍下樱花")
    assert len({item["task_id"] for item in (first, second, third)}) == 3
    for item in (first, second, third):
        assert item["status"] == "completed"
        assert item["generation_mode"] == "FIRST_GENERATION" and not item["review_required"]
        assert item["director_spec"]["version"] == 1
        assert item["director_spec"]["validation"]["can_confirm"]
    assert first["director_spec"]["director_plan"]["character_pose"] == "背对镜头抬头看樱花"
    assert second["director_spec"]["director_plan"]["character_pose"] == "惊喜回眸朝我看过来"
    assert app.comic_projects.get_director(first["task_id"]).version == 1
    assert app.comic_work_turn(parent["work_id"], body)["run_id"] == first["run_id"]
    replay = app.comic_work_turn_status(parent["work_id"], body["request_id"])
    assert replay["result"]["run_id"] == first["run_id"]
    timeline = app.comic_workspace.conversation(parent["work_id"])
    assert len([item for item in timeline["messages"] if item["role"] == "user"]) == 3
    assert set(timeline["related_run_ids"]) == {first["run_id"], second["run_id"], third["run_id"]}
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        "changes": {"director_plan.character_expression": "安静微笑"}}))
    snapshot = app.comic_projects.get(first["task_id"])
    edit = {"request_id": uuid4().hex,
            "expected_project_version": app.comic_workspace.get(parent["work_id"])["version"],
            "conversation_id": parent["conversation_id"], "creation_mode": "professional",
            "independent_plan": True, "task": "把第一个方案的表情改成安静微笑",
            "turn_operation": "revise", "target_task_id": first["task_id"],
            "expected_target_project_version": snapshot.project.current_version,
            "expected_director_version": 1}
    revised = app.comic_work_turn(parent["work_id"], edit)
    assert revised["task_id"] == first["task_id"]
    assert revised["director_spec"]["version"] == 2 and revised["review_required"]
    assert app.comic_projects.get_director(second["task_id"]).version == 1
    assert app.comic_work_turn(parent["work_id"], edit)["run_id"] == revised["run_id"]
    assert len(app.comic_workspace.get(parent["work_id"])["tasks"]) == 3


@pytest.mark.parametrize("failure", [None, "repair", "persistent", "unresolvable", "timeout"])
def test_three_creative_turns_keep_independent_directors_and_first_complete_prompts(
    story_app, monkeypatch, failure,
):
    """Real normalization, confirmations, stores and HTTP reads; only models are substitutes."""
    from http.client import HTTPConnection
    from threading import Thread

    from test_comic_director_quality import _responses

    from kantoku.shells import web_studio

    app, parent = story_app, work(story_app)
    turns = [
        ("少女穿深蓝水手服，晴天校园樱花树下，背对镜头抬头看樱花，神态安静，日系胶片风格",
         "背对镜头抬头看樱花", "神态安静"),
        ("她回眸看向镜头，一脸充满爱意，双手背在身后，人物位于画面右侧",
         "回眸看向镜头，双手背在身后", "一脸充满爱意"),
        ("她拿起手机拍下樱花，好奇地微笑", "拿起手机拍下樱花", "好奇地微笑"),
    ]
    clauses = [
        [("少女穿深蓝水手服", "identity", ["少女", "深蓝水手服"]),
         ("晴天校园樱花树下", "world", ["晴天校园樱花树下"]),
         ("背对镜头抬头看樱花", "shot", ["背对镜头抬头看樱花"]),
         ("神态安静", "shot", ["神态安静"]),
         ("日系胶片风格", "style", ["日系胶片风格"])],
        [("回眸看向镜头", "shot", ["回眸看向镜头"]),
         ("一脸充满爱意", "shot", ["一脸充满爱意"]),
         ("双手背在身后", "shot", ["双手背在身后"]),
         ("人物位于画面右侧", "shot", ["人物位于画面右侧"])],
        [("拿起手机拍下樱花", "shot", ["拿起手机拍下樱花"]),
         ("好奇地微笑", "shot", ["好奇地微笑"])],
    ]
    current, selected_sources, saved = 0, [], []
    calls = {"compile": [0, 0, 0], "review": [0, 0, 0], "normalize": [0, 0, 0]}
    sections_by_scope = {"identity": "character_context", "world": "world_context",
                         "style": "style_context", "shot": "shot_context"}

    def stage(skill, _inputs, context):
        output = _responses()[skill]
        if skill == "comic.creative_understanding":
            output["creative_decision"].update(
                intent_summary=turns[current][0], narrative_context=turns[current][0],
                emotional_target=turns[current][2],
                hard_constraints=context["context"]["creative_brief"]["hard_constraints"])
        if skill == "comic.visual_direction":
            output["director_plan"].update(
                visual_strategy="校园里用人物动作连接樱花场景", visual_focus="水手服少女与樱花树",
                character_presence="黑色短发少女，深蓝水手服", characters_present=True,
                subject_environment_relation="晴天校园樱花树下，完整树冠与人物相互衬托",
                composition_strategy="人物位于画面右侧" if current == 1 else "人物位于画面左侧",
                color_strategy="深蓝服饰与淡粉樱花组成柔和色彩", style_boundary="日系胶片风格",
                character_pose=turns[current][1], character_expression=turns[current][2])
        if skill == "comic.cinematography":
            output["cinematography"].update(
                shot_size="全景", camera_angle="平视", light_source="日光作为主光源",
                lighting="柔和日光照亮少女与樱花", light_direction="左前方",
                color_relationship="深蓝服饰与淡粉樱花组成柔和色彩")
        return output

    def semantic_model(messages):
        body = json.loads(messages[1]["content"])
        if isinstance(body, list):
            calls["normalize"][current] += 1
            proposals = [{"source_index": index, "source_quote": quote,
                          "requirement": quote, "target": "当前少女与场景", "scope": scope}
                         for index, source in enumerate(body)
                         for quote, scope, _tokens in clauses[current] if quote in source["quote"]]
            assert len(proposals) == len(clauses[current])
            return json.dumps({"constraints": proposals}, ensure_ascii=False)
        if "required_constraints" in body:
            calls["compile"][current] += 1
            if current == 1 and failure == "timeout":
                raise TimeoutError("Offline provider response and billing are unknown")
            broken = current == 1 and failure in {"repair", "persistent"} and (
                calls["compile"][current] == 1 or failure == "persistent")
            if body.get("content_repair"):
                assert failure in {"repair", "persistent", "unresolvable"}
                assert body["content_repair"]["issues"]
            # Deliberately omit every emotion: final coverage must inspect server-projected text.
            return json.dumps({
                "director_summary": "离线模型替身：当前校园画面",
                "character_context": {"identity": "少女", "appearance": "黑色短发",
                                      "clothing": "深蓝水手服"},
                "world_context": {"location": "校园", "time": "晴天",
                                  "environment": "晴天校园樱花树下"},
                "style_context": {"art_style": "日系胶片风格", "color": "深蓝与淡粉"},
                "shot_context": {"action": turns[current][1], "pose": "双肩自然放松",
                                 "composition": "人物位于画面左侧" if broken or current != 1
                                 else "人物位于画面右侧", "camera": "全景，平视"},
                "negative_prompt": "禁止额外人物与人物身份漂移",
            }, ensure_ascii=False)
        extraction = body.get("stage") == "constraint_extraction_review"
        if not extraction:
            calls["review"][current] += 1
            assert "content_repair" not in body
            assert "上一次返回已收到但输出校验失败" not in messages[0]["content"]
        findings = []
        for item in body["constraints"]:
            if extraction:
                path = f"constraints.{item['constraint_id']}.source_quote"
                covered = any(item["source_quote"] in source["quote"]
                              for source in body["sources"])
            else:
                path = "sections." + sections_by_scope[item["scope"]]
                evidence = body["evidence_fields"][path]
                tokens = next(tokens for quote, _scope, tokens in clauses[current]
                              if quote == item["requirement"])
                covered = all(token in evidence for token in tokens)
            if not extraction and current == 1 and failure == "unresolvable":
                # 模拟复核在正文补齐后仍不确认：交付必须如实标注 advisory，且不能中断创作。
                findings.append({"constraint_id": item["constraint_id"], "status": "uncertain",
                                 "evidence_path": None, "reason": "离线复核保持存疑"})
                continue
            findings.append({"constraint_id": item["constraint_id"],
                             "status": "covered" if covered else "missing",
                             "evidence_path": path if covered else None,
                             "reason": "离线逐项核验真实正文" if covered else "正文遗漏要求"})
        return json.dumps({"complete": True, "scope_valid": True, "findings": findings},
                          ensure_ascii=False)

    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "new" if current == 0 else "continue",
        "goal": turns[current][0], "sources": selected_sources}))
    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        "public_summary": "离线导演检查", "confidence": .9,
        "findings": [], "suggested_patches": []}))
    monkeypatch.setattr(app, "_comic_constraint_model", semantic_model)
    monkeypatch.setattr(app, "_comic_storyboard_model", lambda _: json.dumps({
        "title": "离线单图分镜", "description": "当前新创作独立分镜", "shots": [{
            "purpose": "呈现当前动作和神态", "subject": "少女", "action": turns[current][1],
            "environment": "晴天校园樱花树下", "emotion": turns[current][2],
            "shot_size": "全景", "camera_angle": "平视"}]}))
    server = web_studio.make_server(app)
    thread = Thread(target=server.serve_forever)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        for current in range(3):
            request = {"request_id": uuid4().hex,
                "expected_project_version": app.comic_workspace.get(parent["work_id"])["version"],
                "conversation_id": parent["conversation_id"], "creation_mode": "professional",
                "independent_plan": True, "task": turns[current][0]}
            result = app.comic_work_turn(parent["work_id"], request)
            project_id = result["task_id"]
            director = app.comic_projects.get_director(project_id)
            assert result["status"] == "completed"
            assert result["generation_mode"] == "FIRST_GENERATION"
            assert not result["review_required"] and not result["director_spec"]["user_confirmed"]
            assert not app.comic_projects.human_director_confirmed(director)
            assert app.comic_work_turn(parent["work_id"], request)["run_id"] == result["run_id"]
            brief = app.comic_projects.get(project_id).creative_brief
            assert brief.constraint_contract_version == 2 and brief.semantic_constraints
            assert {item.requirement for item in brief.semantic_constraints} == {
                quote for quote, _scope, _tokens in clauses[current]}
            app.confirm_comic_director(project_id, {
                "expected_project_version": app.comic_projects.get(
                    project_id).project.current_version,
                "version": director.version})
            board = app.create_comic_storyboard(project_id, {
                "expected_project_version": app.comic_projects.get(
                    project_id).project.current_version,
                "generate": True, "task": turns[current][0]})
            shot = board["shots"][0]
            compile_request = {
                "expected_project_version": app.comic_projects.get(
                    project_id).project.current_version,
                "expected_shot_version": shot["version"], "image_mode": "external",
                "conversation_id": parent["conversation_id"], "complete_prompt": True}
            if current == 1 and failure == "timeout":
                with pytest.raises(TimeoutError):
                    app.compile_comic_prompt(shot["shot_id"], compile_request)
                # 供应商结果未知时不能伪造指令；超时必须如实暴露。
                assert not app.comic_prompts.versions(shot["shot_id"])
                assert calls["compile"][current] == 1
                assert calls["review"][current] == 0
                break
            prompt = app.compile_comic_prompt(shot["shot_id"], compile_request)
            assert prompt["version"] == 1 and prompt["context_sources"]["constraint_review"]
            assert turns[current][2] in prompt["shot_context"]
            assert turns[current][2] not in prompt["character_context"]
            attempted = 2 if current == 1 and failure in {
                "repair", "persistent", "unresolvable"} else 1
            assert calls["compile"][current] == attempted
            # 首次遗漏 → 带核验结论重编一次；仍遗漏则由服务端补齐正文并再独立复核一次。
            assert calls["review"][current] in {attempted, attempted + 1}
            if current == 1 and failure in {"persistent", "unresolvable"}:
                assert calls["review"][current] == attempted + 1
            # 只有“正文已承载全部要求”时才允许以 advisory 交付，绝不伪造通过。
            expected_delivery = ("advisory" if current == 1 and failure == "unresolvable"
                                 else "verified")
            assert (prompt["context_sources"]["constraint_review"]["delivery"]
                    == expected_delivery)
            assert calls["normalize"][current] == 1
            if saved:
                for field in ("character_context", "world_context", "style_context"):
                    assert prompt[field] == saved[0][field]
                assert prompt["shot_context"] != saved[0]["shot_context"]
                assert project_id != saved[0]["project_id"]
            saved.append(prompt)
            if current == 0:
                selected_sources.append({"kind": "run", "id": result["run_id"], "version": 1,
                    "fields": ["character_context", "world_context", "style_context"]})
        for prompt in saved:
            connection.request("GET", f"/api/comic/shots/{prompt['shot_id']}/prompt?version=1",
                               headers={"X-Studio-Token": app.token})
            response = connection.getresponse()
            restored = json.loads(response.read())
            assert response.status == 200
            assert restored["artifact_id"] == prompt["artifact_id"]
            assert restored["positive_prompt"] == prompt["positive_prompt"]
            assert len(app.comic_prompts.versions(prompt["shot_id"])) == 1
        # 只有供应商结果未知会中断；内容遗漏由编译器重编与补齐消化，不再中断后续创作。
        assert len(saved) == (1 if failure == "timeout" else 3)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_work_delete_is_persistent_and_keeps_sources(story_app):
    app = story_app
    parent = work(app)
    child = task(app, parent)
    current = app.comic_workspace.get(parent["work_id"])
    body = {"request_id": uuid4().hex, "expected_version": current["version"]}
    assert app.comic_workspace.delete(parent["work_id"], body)["deleted"]
    assert app.comic_workspace.delete(parent["work_id"], body)["deleted"]
    assert not app.comic_workspace.list()["works"]
    assert app.comic_projects.get(child["project_id"])
    assert app.runtime_store.get_conversation(child["conversation_id"])
    with pytest.raises(ToolError, match="找不到"):
        app.comic_workspace.get(parent["work_id"])


@pytest.mark.parametrize("issue", ["pose", "character", "photography", "composition"])
@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_first_work_delivery_repairs_content_or_returns_marked_local_draft(
    story_app, monkeypatch, issue, repair_succeeds,
):
    from test_comic_director_quality import SKILLS, _responses

    from kantoku.domains.comic.models import DirectorSpecDraft
    from kantoku.shells import web_studio

    app, parent = story_app, work(story_app)
    calls, candidate, selected_sources = [], {}, []
    testing = False
    monkeypatch.setattr(web_studio, "normalize_constraints", lambda *_args, **_kw: ([], {}))
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "continue", "goal": "少女惊喜回眸",
        "sources": selected_sources}))

    def stage(skill, _inputs, context):
        repairing = bool(context["context"].get("generation_feedback"))
        calls.append((skill, repairing))
        output = _responses()[skill]
        if skill == SKILLS[1]:
            output["director_plan"].update(
                character_presence="水手服少女", character_pose="惊喜回眸",
                composition_strategy="人物回眸看向镜头，与背景樱花形成景深")
            if testing and not (repairing and repair_succeeds):
                if issue == "pose":
                    output["director_plan"]["character_pose"] = "背对镜头，完全看不到脸"
                if issue == "character":
                    output["director_plan"]["character_presence"] = "穿铠甲的陌生男性"
                if issue == "composition":
                    output["director_plan"]["composition_strategy"] = "人物背面轮廓，完全遮住正脸"
            candidate.update(output["director_plan"])
        if (testing and skill == SKILLS[2] and issue == "photography"
                and not (repairing and repair_succeeds)):
            output["cinematography"].update(status="needs_revision", shot_size=None)
        return output

    def critic(_messages):
        field = ("character_pose" if issue == "pose" else "composition_strategy"
                 if issue == "composition" else "character_presence")
        wrong = candidate.get(field, "")
        findings = [{"code": "CURRENT_REQUEST_CONFLICT",
                     "severity": "warning" if issue == "composition" else "error",
                     "field_path": "director_plan." + field, "evidence": wrong,
                     "expected": "水手服少女惊喜回眸", "suggested_action": "按本轮要求修正"}]
        return json.dumps({"public_summary": "离线内容检查", "confidence": .9,
                           "findings": findings if "完全看不到" in wrong
                           or "陌生男性" in wrong or "遮住正脸" in wrong else [],
                           "suggested_patches": []})

    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    monkeypatch.setattr(app, "_comic_director_model", critic)
    seed = app.comic_work_turn(parent["work_id"], {
        "request_id": uuid4().hex, "expected_project_version": parent["version"],
        "conversation_id": parent["conversation_id"], "creation_mode": "professional",
        "independent_plan": True, "task": "水手服少女站在樱花树下"})
    original = app.comic_projects.get_director(seed["task_id"])
    draft = {name: value for name, value in original.model_dump(mode="json").items()
             if name in DirectorSpecDraft.model_fields}
    draft["director_plan"]["style_boundary"] = "人工选定的柔和胶片质感"
    edited = app.create_comic_director(seed["task_id"], {
        "expected_project_version": app.comic_projects.get(seed["task_id"]).project.current_version,
        "expected_director_version": 1, "conversation_id": parent["conversation_id"],
        "draft": draft})
    owner = app._comic_director_owner(seed["task_id"], parent["conversation_id"], edited["version"])
    selected_sources.append({"kind": "run", "id": owner.id, "version": edited["version"],
                             "fields": ["director_plan.style_boundary"]})
    testing = True
    calls.clear()
    result = app.comic_work_turn(parent["work_id"], {
        "request_id": uuid4().hex,
        "expected_project_version": app.comic_workspace.get(parent["work_id"])["version"],
        "conversation_id": parent["conversation_id"], "creation_mode": "professional",
        "independent_plan": True, "task": "水手服少女惊喜回眸朝我看过来"})
    run = app.runtime_store.get_run(result["run_id"])
    assert any(repairing for _skill, repairing in calls)
    assert run.state["generation_repair_count"] == 1
    assert result["review_required"] is False
    assert result["generation_mode"] == "FIRST_GENERATION"
    assert result["director_spec"]["director_plan"]["style_boundary"] == "人工选定的柔和胶片质感"
    assert app.comic_projects.get_director(seed["task_id"]).version == edited["version"]
    assert result["director_spec"]["validation"]["can_confirm"]
    assert not result["generation_incomplete"]
    assert not result["director_spec"]["generation_incomplete"]
    assert result["status"] == "completed"
    assert not result["director_spec"]["user_confirmed"]
    if repair_succeeds:
        assert result["status"] == "completed"
        assert result["director_spec"]["director_plan"]["character_pose"] == "惊喜回眸"
        assert result["director_spec"]["director_plan"]["character_presence"] == "水手服少女"
        assert result["director_spec"]["cinematography"]["shot_size"]
        assert run.state["critic_result"]["verdict"] == "pass"
    else:
        assert run.state["local_fallback_stages"]
        assert app.comic_projects.get(result["task_id"]).project.director_id
        assert result["director_spec"]["critic_status"] == "unavailable"
        assert result["director_spec"]["critic_result"] is None
        assert "本地预案" in result["director_execution_summary"]["status_label"]
        assert result["director_spec"]["creative_decision"]["intent_summary"] == (
            "水手服少女惊喜回眸朝我看过来")
        assert run.state["generation_fallback_previous_candidate"]
        assert run.state["generation_fallback_previous_review"]
        assert "本地" in result["director_spec"]["director_plan"]["risk_flags"][0]
        assert len(app.comic_workspace.get(parent["work_id"])["tasks"]) == 2


def test_first_work_delivery_succeeds_with_advisory_only_findings(story_app, monkeypatch):
    """首次生成只返回“优化建议”级结论时，必须交付可用方案，不能判为生成失败。

    回归根因：作品新方案原先要求 Critic verdict 严格为 pass，任何 warning 级
    建议（如 VISUAL_CAUSALITY_MISMATCH）都会把整轮方案标为 generation_incomplete，
    用户看到的即“方案检查未完成 / 生成失败”。
    """
    from test_comic_director_quality import _responses

    from kantoku.shells import web_studio

    app, parent = story_app, work(story_app)
    monkeypatch.setattr(web_studio, "normalize_constraints", lambda *_a, **_k: ([], {}))
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "new", "goal": "少女站在樱花树下",
        "sources": []}))
    candidate: dict = {}

    def stage(skill, _inputs, _context):
        assert not _context["context"].get("generation_feedback"), (
            "An optional advisory must not add a model repair call or replace valid content")
        output = _responses()[skill]
        if skill == "comic.visual_direction":
            candidate.update(output["director_plan"])
        return output

    monkeypatch.setattr(app.comic_director, "stage_executor", stage)

    def advisory_critic(_messages):
        # 证据必须逐字来自方案，因此直接引用真实构图值；结论为纯“优化建议”。
        return json.dumps({
            "public_summary": "视觉因果可优化", "confidence": 0.9,
            "findings": [{
                "code": "VISUAL_CAUSALITY_MISMATCH", "severity": "warning",
                "field_path": "director_plan.composition_strategy",
                "evidence": candidate.get("composition_strategy", ""),
                "expected": "补足构图与故事的关系说明",
                "suggested_action": "补充构图理由",
            }],
            "suggested_patches": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", advisory_critic)
    result = app.comic_work_turn(parent["work_id"], {
        "request_id": uuid4().hex, "expected_project_version": parent["version"],
        "conversation_id": parent["conversation_id"], "creation_mode": "professional",
        "independent_plan": True, "task": "水手服少女站在樱花树下抬头看樱花"})
    run = app.runtime_store.get_run(result["run_id"])
    assert result["status"] == "completed"
    assert result["generation_incomplete"] is False
    assert run.state.get("generation_incomplete") is not True
    assert result["director_spec"]["validation"]["can_confirm"] is True
    # 真实审核结论被保留，没有被伪造成 pass。
    assert run.state["critic_result"]["verdict"] == "needs_revision"
    assert app.comic_projects.get(result["task_id"]).project.director_id


@pytest.mark.parametrize("failed_stage", [
    "comic.creative_understanding", "comic.visual_direction", "comic.cinematography",
])
@pytest.mark.parametrize("failure", ["timeout", "empty", "invalid_schema"])
def test_first_work_generation_recovers_failed_stage_without_unknown_resubmission(
    story_app, monkeypatch, failed_stage, failure,
):
    from test_comic_director_quality import _responses

    from kantoku.shells import web_studio

    app, parent, calls = story_app, work(story_app), []
    text = "少女在樱花树下惊喜回眸，全景，平视"
    monkeypatch.setattr(web_studio, "normalize_constraints", lambda *_args, **_kw: ([], {}))
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "new", "goal": text}))

    def stage(skill, *_args):
        calls.append(skill)
        if skill == failed_stage:
            if failure == "timeout":
                raise TimeoutError("Offline transport timed out; response unknown")
            return {} if failure == "empty" else {"unexpected": "Invalid public schema"}
        return _responses()[skill]

    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    monkeypatch.setattr(app, "_comic_director_model", lambda _: pytest.fail(
        "Local recovery must not make another model/review call"))
    body = {"request_id": uuid4().hex, "expected_project_version": parent["version"],
            "conversation_id": parent["conversation_id"], "creation_mode": "professional",
            "independent_plan": True, "task": text}
    result = app.comic_work_turn(parent["work_id"], body)
    run = app.runtime_store.get_run(result["run_id"])
    spec = result["director_spec"]
    assert result["status"] == "completed" and spec["schema_version"] == 2
    assert spec["validation"]["can_confirm"] and not spec["user_confirmed"]
    assert spec["cinematography"]["status"] == "complete"
    assert spec["critic_status"] == "unavailable" and spec["critic_result"] is None
    assert app.comic_projects.get(result["task_id"]).creative_brief.original_request == text
    assert failed_stage in run.state["local_fallback_stages"]
    if failure == "timeout":
        assert calls.count(failed_stage) == 1
    for skill in run.state["local_fallback_stages"]:
        debug = run.state["director_debug"]["stages"][skill]
        assert debug["source"] == "local_director_fallback"
        assert not debug.get("model_request_ids")
    count = len(calls)
    assert app.comic_work_turn(parent["work_id"], body)["run_id"] == result["run_id"]
    assert len(calls) == count


@pytest.mark.parametrize("failure", ["context_timeout", "context_empty", "constraints"])
def test_first_work_preparation_failure_keeps_request_and_replay_is_idempotent(
    story_app, monkeypatch, failure,
):
    from test_comic_director_quality import _responses

    from kantoku.shells import web_studio

    app, parent, context_calls = story_app, work(story_app), []
    text = "创作少女穿水手服站在樱花树下，保留整棵树与人物全身"

    def understand(_messages):
        context_calls.append(True)
        if failure == "context_timeout":
            raise TimeoutError("Offline context service unavailable")
        return "" if failure == "context_empty" else json.dumps({
            "action": "create", "relation": "new", "goal": text})

    def constraints(*_args, **_kwargs):
        if failure == "constraints":
            raise TimeoutError("Offline constraint service unavailable")
        return [], {}

    def stage(skill, _inputs, context):
        output = _responses()[skill]
        if skill == "comic.creative_understanding":
            output["creative_decision"].update(
                intent_summary=text, narrative_context=text,
                hard_constraints=context["context"]["creative_brief"]["hard_constraints"])
        return output

    monkeypatch.setattr(app, "_conversation_context_model", understand)
    monkeypatch.setattr(web_studio, "normalize_constraints", constraints)
    monkeypatch.setattr(app.comic_director, "stage_executor", stage)
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        "public_summary": "离线约束检查", "confidence": .9,
        "findings": [], "suggested_patches": []}))
    body = {"request_id": uuid4().hex, "expected_project_version": parent["version"],
            "conversation_id": parent["conversation_id"], "creation_mode": "professional",
            "independent_plan": True, "task": text}
    assert web_studio._comic_generation_deadline.get() is None
    result = app.comic_work_turn(parent["work_id"], body)
    brief = app.comic_projects.get(result["task_id"]).creative_brief
    assert result["status"] == "completed" and result["director_spec"]["schema_version"] == 2
    assert brief.original_request == text
    if failure == "constraints":
        assert "".join(brief.hard_constraints) == text
        assert brief.constraint_contract_version is None
        assert brief.constraint_evidence["fallback"] == "verbatim_user_sources"
    count = len(context_calls)
    assert app.comic_work_turn(parent["work_id"], body)["run_id"] == result["run_id"]
    assert len(context_calls) == count
    assert web_studio._comic_generation_deadline.get() is None


def test_local_director_draft_keeps_complete_request_and_selected_bibles():
    from kantoku.domains.comic.director import local_director_stage

    original = "水手服少女站在樱花树下抬头看花，远景，仰拍"
    bibles = {"character_context": "同一水手服少女，黑色短发与蓝色裙装",
              "world_context": "晴天的校园，完整盛开的樱花树",
              "style_context": "日系自然胶片质感，柔和自然色彩"}
    context = {"creative_brief": {"original_request": original,
                                  "hard_constraints": ["完整保留樱花树"]},
               "current_task": original, "creative_context_lock": {"bibles": bibles}}
    decision = local_director_stage("comic.creative_understanding", {}, context)[
        "creative_decision"]
    plan = local_director_stage("comic.visual_direction", {}, context)["director_plan"]
    camera = local_director_stage("comic.cinematography", {"director_plan": plan}, context)[
        "cinematography"]
    assert decision["intent_summary"] == original == decision["narrative_context"]
    assert decision["hard_constraints"] == ["完整保留樱花树"]
    assert plan["character_presence"] == bibles["character_context"]
    assert plan["subject_environment_relation"] == bibles["world_context"]
    assert plan["style_boundary"] == bibles["style_context"]
    assert camera["shot_size"] == "远景" and camera["camera_angle"] == "仰拍"
    assert camera["status"] == "complete" and camera["movement"] is None


def test_first_work_delivery_survives_unavailable_internal_check(story_app, monkeypatch):
    """内部检查暂不可用时，作品首次生成仍必须返回一份可编辑、可确认的方案草稿。"""
    from test_comic_director_quality import _responses

    from kantoku.shells import web_studio

    app, parent = story_app, work(story_app)
    monkeypatch.setattr(web_studio, "normalize_constraints", lambda *_a, **_k: ([], {}))
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "new", "goal": "少女站在樱花树下", "sources": []}))
    monkeypatch.setattr(app.comic_director, "stage_executor",
                        lambda skill, *_a: _responses()[skill])
    # 审核结构缺少 confidence：一次结构修复后仍不可用，不得因此丢掉首轮方案。
    monkeypatch.setattr(app, "_comic_director_model", lambda _messages: json.dumps(
        {"public_summary": "缺少 confidence", "findings": []}, ensure_ascii=False))

    result = app.comic_work_turn(parent["work_id"], {
        "request_id": uuid4().hex, "expected_project_version": parent["version"],
        "conversation_id": parent["conversation_id"], "creation_mode": "professional",
        "independent_plan": True, "task": "水手服少女站在樱花树下抬头看樱花"})
    assert result["status"] == "completed"
    assert result["generation_incomplete"] is False
    assert result["director_spec"]["schema_version"] == 2
    assert result["director_spec"]["critic_status"] == "unavailable"
    assert result["director_spec"]["critic_result"] is None
    # 生产仍须显式人工确认：方案可编辑可确认，但不等于已获制作授权。
    assert result["director_spec"]["validation"]["can_confirm"] is True
    assert result["director_spec"]["user_confirmed"] is False
    assert app.comic_projects.get(result["task_id"]).project.director_id


@pytest.mark.parametrize("interrupted", [False, True])
def test_recover_exact_message_plans_keeps_originals_and_resumes(
    story_app, monkeypatch, interrupted,
):
    from test_comic_director_quality import _responses

    app, parent = story_app, work(story_app)
    child = task(app, parent, "A girl under a cherry tree")
    action = "create"
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": action, "relation": "continue", "goal": "Cherry tree scene"}))
    monkeypatch.setattr(app.comic_director, "stage_executor",
                        lambda skill, *_args: _responses()[skill])
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        "public_summary": "Offline validation", "confidence": .9,
        "findings": [], "suggested_patches": []}))

    def turn(text):
        current = app.comic_projects.get(child["task_id"])
        return app.comic_task_turn(parent["work_id"], child["task_id"], {
            "request_id": uuid4().hex, "conversation_id": child["conversation_id"],
            "expected_project_version": current.project.current_version,
            "expected_director_version": current.project.director_version, "text": text})

    first = turn("Girl looks at the cherry blossoms")
    action = "director.revise"
    monkeypatch.setattr(app, "_comic_director_model", lambda _: json.dumps({
        "changes": {"director_plan.character_expression": "Surprised smile"}}))
    second = turn("Then she turns back with a surprised smile")
    original = app.comic_projects.get_director(child["task_id"]).model_dump(mode="json")
    messages = [item.model_dump(mode="json") for item in
                app.runtime_store.list_conversation_messages(child["conversation_id"])]
    request = {"request_id": uuid4().hex,
               "expected_version": app.comic_workspace.get(parent["work_id"])["version"],
               "source_run_groups": [[first["run_id"]], [second["run_id"]]]}
    if interrupted:
        append = app.comic_workspace._append
        failed = False

        def interrupt(connection, project_id, kind, payload, entity_id=None):
            nonlocal failed
            if kind == "workspace_plan_source" and not failed:
                failed = True
                raise ToolError("offline interruption")
            return append(connection, project_id, kind, payload, entity_id)

        monkeypatch.setattr(app.comic_workspace, "_append", interrupt)
        with pytest.raises(ToolError, match="interruption"):
            app.comic_workspace.recover_plans(parent["work_id"], request)
    recovered = app.comic_workspace.recover_plans(parent["work_id"], request)
    assert len([item for item in recovered["tasks"] if not item.get("history_source_only")]) == 2
    assert len(recovered["plan_recoveries"]) == 2
    assert recovered == app.comic_workspace.recover_plans(parent["work_id"], request)
    assert app.comic_projects.get_director(child["task_id"]).model_dump(mode="json") == original
    assert [item.model_dump(mode="json") for item in
            app.runtime_store.list_conversation_messages(child["conversation_id"])] == messages
    timeline = app.comic_workspace.conversation(parent["work_id"])
    assert len(timeline["messages"]) == len(messages)
    assert {item["source_run_id"] for item in timeline["messages"]} == {
        first["run_id"], second["run_id"]}
    for mapping in recovered["plan_recoveries"]:
        result = app._comic_director_result(app.runtime_store.get_run(mapping["run_id"]))
        assert result["director_spec"]["validation"]["can_confirm"] is False
        assert result["status"] == "waiting"
    assert len(timeline["related_run_ids"]) == 2


def test_parent_child_identity_conversation_binding_and_replay(story_app):
    app = story_app
    request = {"request_id": uuid4().hex, "title": "West"}
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: app.comic_workspace.create(request), range(2)))
    assert results[0] == results[1]
    parent = results[0]
    a, b = task(app, parent), task(app, parent, "Closeup")
    assert a["task_id"] == a["project_id"] != b["task_id"]
    assert a["conversation_id"] != b["conversation_id"]
    with pytest.raises(ToolError, match="归属"):
        app.comic_workspace.require_task(parent["work_id"], a["task_id"], b["conversation_id"])
    with pytest.raises(ToolError, match="固定绑定"):
        app.comic_media.require_owner(a["task_id"], b["conversation_id"])
    foreign = work(app, "Rain")
    with pytest.raises(ToolError, match="归属"):
        app.comic_workspace.require_task(foreign["work_id"], a["task_id"])
    with pytest.raises(ToolError, match="更新"):
        app.comic_workspace.create_task(
            parent["work_id"], {"request_id": uuid4().hex, "expected_version": 1, "title": "Stale"}
        )


def test_work_story_aggregates_only_member_tasks_and_pins_bytes(story_app):
    app = story_app
    west, rain = work(app, "West"), work(app, "Rain")
    a, b, foreign = task(app, west), task(app, west, "Closeup"), task(app, rain)
    images = [_upload(app, t["task_id"], t["conversation_id"]) for t in (a, b, foreign)]
    for t, image in zip((a, b, foreign), images, strict=True):
        _select(app, t["conversation_id"], image)
    candidates = app.comic_work_story(west["work_id"], "images")["images"]
    assert not {west["story_conversation_id"], a["conversation_id"]} & {
        item["id"] for item in app.list_conversations(domain="comic")
    }
    assert {i["image_id"] for i in candidates} == {i["image_id"] for i in images[:2]}
    saved = app.comic_work_story(
        west["work_id"], "save", {**story_request(app, west), "draft": _draft(*images[:2])}
    )
    assert saved["story"]["story_id"] != app.comic_stories.story_id(a["conversation_id"])
    assert not app.comic_work_story(rain["work_id"])["story"]
    assert len(saved["story"]["fixed_images"]) == 2
    assert not saved["missing_images"]
    with pytest.raises(ToolError, match="归属"):
        app.comic_work_story(
            west["work_id"], "save", {**story_request(app, west), "draft": _draft(images[2])}
        )
    assert app.comic_work_story(west["work_id"])["story"] == saved["story"]


def test_settings_are_fixed_copies_with_explicit_publication_and_sync(story_app):
    app = story_app
    parent = work(app)

    def publish(description, asset_id=None, version=None):
        return app.comic_workspace.publish_setting(
            parent["work_id"],
            {
                "request_id": uuid4().hex,
                "expected_version": app.comic_workspace.get(parent["work_id"])["version"],
                "asset": {
                    "name": "Robot",
                    "details": {"kind": "character", "appearance": description},
                },
                **({"asset_id": asset_id, "expected_asset_version": version} if asset_id else {}),
            },
        )

    public = publish("Copper shell")
    a, b = task(app, parent), task(app, parent)
    assert a["settings_refs"][0]["source_version"] == 1
    assert a["settings_refs"][0]["local_asset_id"] != b["settings_refs"][0]["local_asset_id"]
    publish("Blue shell", public["asset_id"], 1)
    before = app.comic_workspace.get_task(parent["work_id"], a["task_id"])
    assert before["settings_updates"] == [{"asset_id": public["asset_id"], "version": 2}]
    assert (
        app.comic_assets.get(
            a["task_id"], a["settings_refs"][0]["local_asset_id"]
        ).details.appearance
        == "Copper shell"
    )
    app.comic_workspace.sync_settings(
        parent["work_id"],
        a["task_id"],
        {
            "request_id": uuid4().hex,
            "expected_project_version": before["project_version"],
            "refs": [{"asset_id": public["asset_id"], "version": 2}],
        },
    )
    after = app.comic_workspace.get_task(parent["work_id"], a["task_id"])
    assert after["settings_refs"][0]["source_version"] == 2
    added = publish("New shared character")
    updates = app.comic_workspace.get_task(parent["work_id"], a["task_id"])["settings_updates"]
    assert updates == [{"asset_id": added["asset_id"], "version": 1}]
    assert (
        app.comic_workspace.get_task(parent["work_id"], b["task_id"])["settings_refs"][0][
            "source_version"
        ]
        == 1
    )
    foreign = work(app)
    with pytest.raises(ToolError):
        app.comic_workspace.sync_settings(
            foreign["work_id"],
            task(app, foreign)["task_id"],
            {
                "request_id": uuid4().hex,
                "expected_project_version": 1,
                "refs": [{"asset_id": public["asset_id"], "version": 1}],
            },
        )


def test_history_preview_import_replay_and_original_data_survive(story_app):
    from test_comic_stories import _setup

    app = story_app
    pid, cid, image = _setup(app)
    original = app.comic_media.get(image["image_id"])
    preview = app.comic_workspace.legacy_preview(cid, app.comic_stories)
    assert len(preview["groups"]) == 1
    request = {
        "request_id": uuid4().hex,
        "title": "Imported",
        "conversation_id": cid,
        "preview_sha256": preview["preview_sha256"],
        "source_ids": [preview["groups"][0]["work_id"]],
    }
    imported = app.comic_workspace.import_legacy(request, app.comic_stories)
    assert imported["migration"]["completed"]
    assert app.comic_workspace.import_legacy(request, app.comic_stories) == imported
    child = imported["tasks"][0]
    assert child["task_id"] != pid and child["conversation_id"] != cid
    copies = app.comic_media.list(child["task_id"])
    assert len(copies) == 1 and copies[0]["sha256"] == original.sha256
    assert app.comic_media.get(image["image_id"]) == original
    assert app.runtime_store.get_conversation(cid)


def test_multiple_turns_chat_revision_replay_and_failure_keep_fixed_task(story_app, monkeypatch):
    from test_comic_director_quality import _responses

    app = story_app
    parent = work(app)
    child = task(app, parent, "A single character portrait")
    action = "create"
    calls = []

    def understand(_messages):
        calls.append(action)
        return json.dumps(
            {
                "action": action,
                "relation": "continue",
                "goal": "Current frame",
                "sources": [],
                "tasks": [],
                "reads": [],
            }
        )

    monkeypatch.setattr(app, "_conversation_context_model", understand)
    monkeypatch.setattr(
        app.comic_director, "stage_executor", lambda skill, _inputs, _context: _responses()[skill]
    )
    monkeypatch.setattr(
        app,
        "_comic_director_model",
        lambda _messages: json.dumps(
            {
                "public_summary": "Offline contract only",
                "confidence": 0.9,
                "findings": [],
                "suggested_patches": [],
            }
        ),
    )
    monkeypatch.setattr(
        app, "_comic_constraint_model", lambda _messages: "Discuss this frame only."
    )

    def request(text):
        project = app.comic_projects.get(child["task_id"]).project
        return {
            "request_id": uuid4().hex,
            "conversation_id": child["conversation_id"],
            "expected_project_version": project.current_version,
            "expected_director_version": project.director_version,
            "text": text,
        }

    first_body = request("Create this image")
    first = app.comic_task_turn(parent["work_id"], child["task_id"], first_body)
    assert first["director_spec"]["project_id"] == child["task_id"]
    users = [
        message
        for message in app.runtime_store.list_conversation_messages(child["conversation_id"])
        if message.role.value == "user"
    ]
    assert len(users) == 1
    assert users[0].event_id == "director-request:" + first_body["request_id"]
    assert app.comic_task_turn(parent["work_id"], child["task_id"], first_body) == first
    assert calls == ["create"]
    action = "chat"
    before = app.comic_projects.get(child["task_id"])
    app.comic_task_turn(parent["work_id"], child["task_id"], request("Explain the composition"))
    assert app.comic_projects.get(child["task_id"]) == before
    action = "director.revise"
    monkeypatch.setattr(
        app,
        "_comic_director_model",
        lambda _messages: json.dumps(
            {"changes": {"director_plan.character_expression": "Calm and confident"}}
        ),
    )
    revised = app.comic_task_turn(parent["work_id"], child["task_id"], request("Change expression"))
    assert revised["director_spec"]["spec_id"] == first["director_spec"]["spec_id"]
    assert revised["director_spec"]["version"] == first["director_spec"]["version"] + 1
    assert len(app.comic_workspace.get(parent["work_id"])["tasks"]) == 1
    assert not app.comic_projects.human_director_confirmed(
        app.comic_projects.get_director(child["task_id"])
    )
    failed_body = request("Another change")

    def failed(_messages):
        calls.append("failed-model")
        raise ToolError("offline unavailable")

    monkeypatch.setattr(app, "_comic_director_model", failed)
    with pytest.raises(ToolError, match="offline"):
        app.comic_task_turn(parent["work_id"], child["task_id"], failed_body)
    count = len(calls)
    with pytest.raises(ToolError, match="不会自动重发"):
        app.comic_task_turn(parent["work_id"], child["task_id"], failed_body)
    assert len(calls) == count
    assert (
        app.comic_projects.get_director(child["task_id"]).version
        == revised["director_spec"]["version"]
    )
    public = app.comic_workspace.publish_setting(
        parent["work_id"],
        {
            "request_id": uuid4().hex,
            "expected_version": app.comic_workspace.get(parent["work_id"])["version"],
            "asset": {
                "name": "Shared role",
                "details": {"kind": "character", "appearance": "Copper"},
            },
        },
    )
    binding = app.comic_workspace.get_task(parent["work_id"], child["task_id"])
    synced = app.comic_workspace.sync_settings(
        parent["work_id"],
        child["task_id"],
        {
            "request_id": uuid4().hex,
            "expected_project_version": binding["project_version"],
            "refs": [{"asset_id": public["asset_id"], "version": 1}],
        },
    )
    action = "create"
    monkeypatch.setattr(
        app,
        "_comic_director_model",
        lambda _: json.dumps(
            {
                "public_summary": "Offline public setting refresh",
                "confidence": 0.9,
                "findings": [],
                "suggested_patches": [],
            }
        ),
    )
    updated = app.comic_task_turn(
        parent["work_id"], child["task_id"], request("Use selected setting")
    )
    assert updated["director_spec"]["spec_id"] == first["director_spec"]["spec_id"]
    assert updated["director_spec"]["asset_versions"] == {
        "asset:" + synced["settings_refs"][0]["local_asset_id"]: 1
    }
    assert len(app.comic_workspace.get(parent["work_id"])["tasks"]) == 1


def test_work_zip_keeps_three_formats_original_bytes_and_repeat_order(story_app, monkeypatch):
    from pathlib import Path
    from zipfile import ZipFile

    from test_comic_stories import _jpeg, _png, _webp

    app = story_app
    parent = work(app)
    images, contents = [], [_png(), _jpeg(), _webp()]
    for content, mime, suffix in zip(
        contents, ("image/png", "image/jpeg", "image/webp"), ("png", "jpg", "webp"), strict=True
    ):
        child = task(app, parent)
        image = _upload(
            app,
            child["task_id"],
            child["conversation_id"],
            content=content,
            mime=mime,
            filename="source." + suffix,
        )
        _select(app, child["conversation_id"], image)
        images.append(image)
    app.comic_work_story(
        parent["work_id"],
        "save",
        {**story_request(app, parent), "draft": _draft(*images, images[0])},
    )
    monkeypatch.setattr(
        app, "_comic_constraint_model", lambda _: json.dumps({"status": "pass", "findings": []})
    )
    app.comic_work_story(parent["work_id"], "review", story_request(app, parent))
    app.comic_work_story(parent["work_id"], "confirm", story_request(app, parent))
    exported = app.comic_work_story(parent["work_id"], "exports", story_request(app, parent))
    artifact = app.runtime_store.get_artifact(exported["export_artifact_id"])
    with ZipFile(Path(artifact.location)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["shots"]) == 4 and len(manifest["images"]) == 3
        assert manifest["shots"][0]["filename"] == manifest["shots"][3]["filename"]
        assert {entry["filename"].rsplit(".", 1)[1] for entry in manifest["images"]} == {
            "png",
            "jpg",
            "webp",
        }
        assert {archive.read(entry["filename"]) for entry in manifest["images"]} == set(contents)
        assert "story.md" in archive.namelist()


def test_http_work_task_story_routes_use_authoritative_membership(story_app):
    from http.client import HTTPConnection
    from threading import Thread

    from kantoku.shells import web_studio

    app = story_app
    server = web_studio.make_server(app)
    thread = Thread(target=server.serve_forever)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}

    def call(method, path, data=None):
        connection.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    try:
        status, parent = call(
            "POST", "/api/comic/works", {"request_id": uuid4().hex, "title": "HTTP work"}
        )
        assert status == 201
        root = "/api/comic/works/" + parent["work_id"]
        status, child = call(
            "POST",
            root + "/image-tasks",
            {"request_id": uuid4().hex, "expected_version": parent["version"]},
        )
        assert status == 201 and child["project_id"] == child["task_id"]
        for path in (
            root,
            root + "/image-tasks",
            root + "/image-tasks/" + child["task_id"],
            root + "/settings",
            root + "/story",
            root + "/story/images",
            root + "/story/versions",
            root + "/story/requests",
            root + "/conversation",
            root + "/requests/unknown-request",
        ):
            assert call("GET", path)[0] == 200
        image = _upload(app, child["task_id"], child["conversation_id"])
        _select(app, child["conversation_id"], image)
        status, saved = call(
            "POST", root + "/story/save", {**story_request(app, parent), "draft": _draft(image)}
        )
        assert status == 200 and saved["story"]["anchor_project_id"] == parent["work_id"]
        status, failure = call(
            "POST",
            root + "/story/confirm",
            {**story_request(app, parent), "anchor_project_id": child["task_id"]},
        )
        assert status == 400 and failure["error_id"]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_history_import_preserves_old_fixed_version_and_resumes_after_committed_file(
    story_app, monkeypatch
):
    from test_comic_stories import _png, _request, _setup

    app = story_app
    pid, cid, first = _setup(app)
    app.comic_story_operation(
        cid, "save", {**_request(app, pid, cid, uuid4().hex), "draft": _draft(first)}
    )
    second = _upload(
        app,
        pid,
        cid,
        content=_png(140),
        image_id=first["image_id"],
        expected_image_version=1,
        parent={"image_id": first["image_id"], "version": 1},
    )
    _select(app, cid, second, expected=1)
    preview = app.comic_workspace.legacy_preview(cid, app.comic_stories)
    request = {
        "request_id": uuid4().hex,
        "title": "Recovered work",
        "conversation_id": cid,
        "preview_sha256": preview["preview_sha256"],
        "source_ids": [preview["groups"][0]["work_id"]],
        "include_story": True,
    }
    original_append = app.comic_workspace._append
    interrupted = False

    def append(connection, project_id, kind, payload, entity_id=None):
        nonlocal interrupted
        if kind == "legacy_step" and payload.get("result", {}).get("image_id") and not interrupted:
            interrupted = True
            raise OSError("offline interruption after file commit")
        return original_append(connection, project_id, kind, payload, entity_id)

    monkeypatch.setattr(app.comic_workspace, "_append", append)
    with pytest.raises(ToolError, match="数据库操作失败"):
        app.comic_workspace.import_legacy(request, app.comic_stories)
    recovered = app.comic_workspace.import_legacy(request, app.comic_stories)
    assert len(recovered["tasks"]) == 1
    child = recovered["tasks"][0]
    images = app.comic_media.list(child["task_id"])
    assert len(images) == 1 and images[0]["version"] == 2
    assert len(app.comic_media.versions(images[0]["image_id"])) == 2
    imported_story = app.comic_work_story(recovered["work_id"])
    assert imported_story["story"]["shots"][0]["image"]["version"] == 1
    assert imported_story["review"] is None and imported_story["confirmation"] is None
    assert not imported_story["missing_images"]
    assert app.comic_media.get(first["image_id"], version=1).sha256 == first["sha256"]
