"""模块一：有依据的语义约束、直接导图与不可变参考版本。"""

import base64
import copy
import json
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

import pytest
from test_comic_director_quality import SKILLS, _generate, _responses
from test_web_studio import app as app_fixture
from test_web_studio import server as server_fixture

from kantoku.config import ToolError
from kantoku.config.runtime_paths import atomic_write_bytes, get_runtime_paths
from kantoku.core.conversation_context import MemoryFact, SourceRef
from kantoku.core.runtime.models import ArtifactLifecycle, ArtifactType
from kantoku.core.runtime.store import RuntimeStore
from kantoku.domains.comic.assets import ComicAssetStore
from kantoku.domains.comic.constraints import normalize_constraints, verify_prompt_constraints
from kantoku.domains.comic.models import (
    ComicImageImportRequest,
    ComicProjectInput,
    ComicPromptContexts,
    ComicPromptDraft,
    ComicShotDraft,
    ComicStoryboardDraft,
    CreativeConstraint,
    DirectorSpecDraft,
    ImageVersionRef,
)
from kantoku.domains.comic.projects import ComicProjectStore
from kantoku.domains.comic.prompts import (
    PROMPT_SECTIONS,
    SEMANTIC_BIBLE_CONTRACT,
    ComicPromptStore,
    StructuredImagePromptCompiler,
    compiler_for_model,
    prompt_sections,
)
from kantoku.domains.comic.storyboards import ComicStoryboardStore
from kantoku.shells import web_studio

app = app_fixture
server = server_fixture


def _png(color=0):
    def chunk(name, content):
        return (
            struct.pack(">I", len(content))
            + name
            + content
            + struct.pack(">I", zlib.crc32(name + content))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes([0, color, 0, 0])))
        + chunk(b"IEND", b"")
    )


def _constraint(**changes):
    return CreativeConstraint.model_validate(
        {
            "constraint_id": "adult",
            "requirement": "人物必须为成年女性",
            "target": "主角",
            "scope": "identity",
            "source_ref": {"id": "user-message", "kind": "message"},
            "source_quote": "请做一张成年女性的写实肖像",
            "origin": "user_request",
            "trust": "source_verified",
            **changes,
        }
    )


def test_imported_work_is_not_reused_by_its_first_independent_director(media, monkeypatch):
    app, original, cid = media
    _, image = _upload(app, original, cid)
    monkeypatch.setattr(app, "_conversation_context_model", lambda _: json.dumps({
        "action": "create", "relation": "new", "goal": "新的独立作品"}))
    monkeypatch.setattr(web_studio, "normalize_constraints", lambda *args, **kwargs: ([], {}))
    def stop(*args):
        raise ToolError("离线检查在创建独立作品后停止")
    monkeypatch.setattr(app.comic_director, "stage_executor", stop)
    with pytest.raises(ToolError, match="离线检查"):
        app.create_comic_director(original, {"expected_project_version": image["project_version"],
            "conversation_id": cid, "request_id": "new-work", "creation_mode": "professional",
            "independent_plan": True, "task": "新的独立作品"})
    director = next(run for run in app.runtime_store.list_runs(conversation_id=cid)
                    if run.workflow == "comic.director")
    assert director.state["project_id"] != original
    assert app.comic_projects.get(original).creative_brief.version == 1
    assert app.comic_media.get(image["image_id"]).project_id == original


def test_existing_mixed_project_is_projected_as_separate_works_without_rewriting_media(
    media, monkeypatch, server,
):
    from kantoku.domains.comic.models import CreativeBriefUpdate

    app, pid, cid = media
    _, image = _upload(app, pid, cid)
    # Exercise the old mixed-data shape, including files without the new metadata.
    get_artifact = app.runtime_store.get_artifact
    def old_artifact(identity):
        artifact = get_artifact(identity)
        return artifact.model_copy(update={"metadata": {
            key: value for key, value in artifact.metadata.items()
            if key != "source_brief_version"}})
    monkeypatch.setattr(app.runtime_store, "get_artifact", old_artifact)
    app.comic_projects.replace_brief(pid, CreativeBriefUpdate(
        original_request="新的机器人作品", expected_version=image["project_version"]))
    saved = app.comic_projects.save_director(pid, DirectorSpecDraft(
        storytelling_goal="新的机器人作品", visual_direction="机械与荒原", emotion="平静",
        color_language="暖色", lighting="日光", camera_language="全景",
        composition="居中", character_focus="机器人", constraints=[],
        creative_choices=["观察主体"]),
        expected_project_version=app.comic_projects.get(pid).project.current_version,
        source="model")
    app.runtime_store.create_run("comic", "comic.director", {
        "project_id": pid, "conversation_id": cid, "director_spec_id": saved.spec_id,
        "director_spec_version": saved.version}, "director_assemble")
    before = app.comic_media.get(image["image_id"])
    works = app.comic_media.conversation_works(cid)["works"]
    assert [(item["brief_version"], item["has_director"], item["image_count"])
            for item in works] == [(1, False, 1), (2, True, 0)]
    assert app.list_comic_images(pid, cid, brief_version=2)["images"] == []
    images = app.list_comic_images(pid, cid, brief_version=1)["images"]
    assert images[0]["image_id"] == image["image_id"]
    assert app.comic_media.get(image["image_id"]) == before
    connection = HTTPConnection("127.0.0.1", server)
    try:
        connection.request("GET", f"/api/comic/conversations/{cid}/works",
                           headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["works"] == works
        connection.request("GET", f"/api/comic/projects/{pid}/images"
                           f"?conversation_id={cid}&brief_version=2",
                           headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {"images": []}
    finally:
        connection.close()
    foreign = app.create_conversation({"domain": "comic", "interaction_mode": "guided"})["id"]
    assert app.comic_media.conversation_works(foreign) == {"works": []}
    with pytest.raises(ToolError):
        app.list_comic_images(pid, foreign, brief_version=1)


def _covered(payload, quote):
    return json.dumps(
        {
            "complete": True,
            "scope_valid": True,
            "findings": [
                {"constraint_id": item["constraint_id"], "status": "covered", "quote": quote}
                for item in payload["constraints"]
            ],
        },
        ensure_ascii=False,
    )


@pytest.fixture
def media(app, monkeypatch, tmp_path):
    monkeypatch.setattr(
        web_studio.get_settings().image, "output_dir", tmp_path / "output", raising=False
    )
    monkeypatch.setattr(
        app, "_comic_constraint_model", lambda _: pytest.fail("导图、查询和绑定用途不能调用模型")
    )
    cid = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    pid = app.create_comic_project({"title": "直接导图"})["project"]["project_id"]
    return app, pid, cid


def _upload(app, pid, cid, request_id="upload", **changes):
    data = {
        "request_id": request_id,
        "conversation_id": cid,
        "expected_project_version": app.comic_projects.get(pid).project.current_version,
        "filename": "portrait.png",
        "data_url": "data:image/png;base64," + base64.b64encode(_png()).decode(),
        **changes,
    }
    return data, app.import_comic_image(pid, data)


@pytest.mark.parametrize("lifecycle", ["trashed", "purged"])
def test_media_read_views_keep_history_and_mark_retired_selection_without_switching(
    media, lifecycle,
):
    app, pid, cid = media
    _, first = _upload(app, pid, cid)
    ref = {"image_id": first["image_id"], "version": 1}
    app.select_comic_image(first["image_id"], {
        "request_id": "adopt-first", "conversation_id": cid,
        "expected_project_version": app.comic_projects.get(pid).project.current_version,
        "expected_selection_version": 0, "version": 1})
    _, second = _upload(app, pid, cid, "second", image_id=first["image_id"],
        expected_image_version=1, parent=ref,
        data_url="data:image/png;base64," + base64.b64encode(_png(80)).decode())
    saved = app.save_comic_workbench(pid, {
        "request_id": "workbench", "conversation_id": cid, "expected_version": 0,
        "image": ref, "edit_image": {**ref, "version": 2}, "auto_analyze": True})
    app.change_core_artifact(first["artifact_id"], "trash", {"expected_lifecycle_version": 1})
    if lifecycle == "purged":
        app.change_core_artifact(first["artifact_id"], "purge", {"expected_lifecycle_version": 2})

    family = app.list_comic_images(pid, cid)["images"][0]
    assert family["available"] is True and family["artifact_id"] == second["artifact_id"]
    assert family["selection"]["selected_version"] == 1
    assert family["selection"]["available"] is False
    assert family["selection"]["lifecycle_state"] == lifecycle
    assert family["selection"]["unavailable_reason"]
    history = app.comic_image_versions(first["image_id"], cid)
    assert [(item["version"], item["available"]) for item in history["versions"]] == [
        (2, True), (1, False)]
    workbench = app.comic_workbench_state(pid, cid)
    assert workbench["state"] == saved["state"]
    assert workbench["version"] == saved["version"]
    assert workbench["unavailable_images"][0]["version"] == 1
    with pytest.raises(ToolError):
        app.comic_media.require_image(ImageVersionRef(**ref), pid, cid)
    with pytest.raises(ToolError):
        app.select_comic_image(first["image_id"], {
            "request_id": "do-not-reactivate", "conversation_id": cid,
            "expected_project_version": app.comic_projects.get(pid).project.current_version,
            "expected_selection_version": 1, "version": 1})
    assert app.comic_media.list(pid)[0]["selection"]["selected_version"] == 1


def test_media_list_does_not_hash_every_file_but_operations_reject_changed_or_missing_bytes(
    media, monkeypatch,
):
    app, pid, cid = media
    _, image = _upload(app, pid, cid)
    artifact = app.runtime_store.get_artifact(image["artifact_id"])
    path = Path(artifact.location)
    original_read = Path.read_bytes

    def disallow_projection_read(target):
        assert target != path, "A metadata refresh must not rehash every image"
        return original_read(target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", disallow_projection_read)
        assert app.list_comic_images(pid, cid)["images"][0]["available"] is True
        assert app.comic_image_versions(image["image_id"], cid)["versions"][0]["available"]
    # 列表是身份/文件存在投影，不是内容验真收据；真实读取和操作必须核对字节。
    atomic_write_bytes(path, _png(150), owner="comic.media")
    assert app.comic_media.list(pid)[0]["available"] is True
    with pytest.raises(ToolError, match="改变"):
        app.comic_media.require_image(
            ImageVersionRef(image_id=image["image_id"], version=1), pid, cid)
    with pytest.raises(ToolError, match="内容与保存的版本不一致"):
        app.get_core_artifact_content(image["artifact_id"])
    get_runtime_paths().remove_file(path, file_type="artifact")
    unavailable = app.comic_media.list(pid)[0]
    assert unavailable["available"] is False and unavailable["unavailable_reason"]


@pytest.mark.parametrize("lifecycle", ["trashed", "purged"])
def test_report_list_isolates_inactive_reports_and_keeps_valid_history_for_retired_image(
    media, monkeypatch, lifecycle,
):
    app, pid, cid = media
    _, image = _upload(app, pid, cid)
    ref = {"image_id": image["image_id"], "version": 1}
    output = {"summary": "Offline observation", "findings": [], "protected_features": [],
              "limitations": ["Offline contract fixture"]}
    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(output))
    reports = []
    for number in range(2):
        reports.append(app.comic_image_operation(pid, "analysis", {
            "request_id": f"report-{number}", "conversation_id": cid, "image": ref,
            "expected_project_version": app.comic_projects.get(pid).project.current_version,
            "task": "Inspect this image"}))
    app.change_core_artifact(reports[0]["artifact_id"], "trash",
                             {"expected_lifecycle_version": 1})
    if lifecycle == "purged":
        app.change_core_artifact(reports[0]["artifact_id"], "purge",
                                 {"expected_lifecycle_version": 2})
    app.change_core_artifact(image["artifact_id"], "trash", {"expected_lifecycle_version": 1})
    listed = app.comic_image_reports(pid, cid, "analysis", image=ImageVersionRef(**ref))
    assert [item["artifact_id"] for item in listed["items"]] == [reports[1]["artifact_id"]]
    assert listed["image_available"] is False and listed["unavailable_reason"]
    assert listed["unavailable_items"][0]["artifact_id"] == reports[0]["artifact_id"]
    with pytest.raises(ToolError):
        app.comic_image_reports(pid, cid, "analysis", reports[0]["artifact_id"])


def test_active_child_analysis_reads_purged_parent_lineage_without_reusing_parent_media(
    media, monkeypatch,
):
    app, pid, cid = media
    _, parent = _upload(app, pid, cid)
    parent_ref = {"image_id": parent["image_id"], "version": 1}
    _, child = _upload(app, pid, cid, "child", image_id=parent["image_id"],
        expected_image_version=1, parent=parent_ref,
        data_url="data:image/png;base64," + base64.b64encode(_png(40)).decode())
    app.change_core_artifact(parent["artifact_id"], "trash", {"expected_lifecycle_version": 1})
    app.change_core_artifact(parent["artifact_id"], "purge", {"expected_lifecycle_version": 2})
    seen = []

    def model(messages):
        seen.append(json.loads(messages[-1]["content"][0]["text"]))
        return json.dumps({"summary": "Offline child observation", "findings": [],
                           "protected_features": [], "limitations": ["Offline fixture"]})

    monkeypatch.setattr(app, "_comic_image_vision_model", model)
    report = app.comic_image_operation(pid, "analysis", {
        "request_id": "analyze-child", "conversation_id": cid,
        "expected_project_version": app.comic_projects.get(pid).project.current_version,
        "image": {"image_id": child["image_id"], "version": 2}, "task": "Inspect child only"})
    assert report["status"] == "completed"
    assert {item["artifact_id"] for item in seen[0]["images"]} == {child["artifact_id"]}
    assert "inherited_constraint_replacements" not in seen[0]
    with pytest.raises(ToolError):
        app.comic_media.require_image(ImageVersionRef(**parent_ref), pid, cid)
    assert app.comic_media.require_image(
        ImageVersionRef(image_id=child["image_id"], version=2), pid, cid).artifact_id == \
        child["artifact_id"]
    foreign = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    with pytest.raises(ToolError, match="不属于"):
        app.comic_media.historical_identity(ImageVersionRef(**parent_ref), pid, foreign)


def test_normalization_retains_quotes_and_accepts_paraphrase():
    fact = MemoryFact(
        value="请做一张成年女性的写实肖像",
        source=SourceRef(id="user-message", kind="message"),
        origin="user_request",
    )
    seen = []

    def model(messages):
        payload = json.loads(messages[1]["content"])
        seen.append(payload)
        if isinstance(payload, list):
            return json.dumps(
                {
                    "constraints": [
                        {
                            "source_index": 0,
                            "source_quote": "成年女性",
                            "requirement": "人物必须为成年女性",
                            "target": "主角",
                            "scope": "identity",
                            "verification": "semantic",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        return _covered(payload, "成年女性")

    constraints, evidence = normalize_constraints([fact], previous=[], model_call=model)
    assert len(seen) == 2 and constraints[0].source_ref == fact.source
    assert constraints[0].source_quote == "成年女性"
    assert constraints[0].requirement != fact.value
    assert evidence["review"]["complete"]
    cached, _ = normalize_constraints(
        [fact],
        previous=constraints,
        previous_evidence=[evidence],
        model_call=lambda _: pytest.fail("已核验的相同原文应复用"),
    )
    assert cached == constraints
    receipt = verify_prompt_constraints(
        constraints,
        sections={"character_context": "她是一名成年女性"},
        negative_prompt="",
        model_call=lambda messages: _covered(
            json.loads(messages[1]["content"]), "她是一名成年女性"
        ),
    )
    assert receipt["sha256"]


@pytest.mark.parametrize("case", ["valid", "other_requirement", "uncertain"])
def test_extraction_review_uses_own_frozen_source_and_still_requires_coverage(case):
    fact = MemoryFact(value="边框改为深蓝色，保留周围内容",
                      source=SourceRef(id="manual-edit", kind="run"), origin="user_request")

    def model(messages):
        payload = json.loads(messages[-1]["content"])
        if isinstance(payload, list):
            return json.dumps({"constraints": [{"source_index": 0, "source_quote": quote,
                "target": "已选区域", "scope": "output"}
                for quote in ("边框改为深蓝色", "保留周围内容")]}, ensure_ascii=False)
        ids = [item["constraint_id"] for item in payload["constraints"]]
        paths = list(payload["evidence_fields"])
        return json.dumps({"complete": True, "scope_valid": True, "findings": [
            {"constraint_id": identity, "status": "uncertain" if case == "uncertain" else
             "covered", "quote": "模型另写的文字不作为证据",
             "evidence_path": paths[1 - index] if case == "other_requirement" else paths[index]}
            for index, identity in enumerate(ids)]}, ensure_ascii=False)

    if case != "valid":
        with pytest.raises(ToolError, match="其他要求|有效证据"):
            normalize_constraints([fact], previous=[], model_call=model)
    else:
        constraints, evidence = normalize_constraints([fact], previous=[], model_call=model)
        assert [item["quote"] for item in evidence["review"]["findings"]] == [
            item.source_quote for item in constraints]


def test_unknown_quote_and_false_review_cannot_create_constraints():
    fact = MemoryFact(
        value="成年女性", source=SourceRef(id="user-message", kind="message"), origin="user_request"
    )
    with pytest.raises(ToolError, match="证据不在"):
        normalize_constraints(
            [fact],
            previous=[],
            model_call=lambda _: json.dumps(
                {
                    "constraints": [
                        {
                            "source_index": 0,
                            "source_quote": "银色长发",
                            "requirement": "银发",
                            "target": "主角",
                            "scope": "identity",
                        }
                    ]
                }
            ),
        )
    with pytest.raises(ToolError, match="有效证据"):
        verify_prompt_constraints(
            [_constraint()],
            sections={"character_context": "成年女性"},
            negative_prompt="",
            model_call=lambda messages: _covered(
                json.loads(messages[1]["content"]), "并不存在的证据"
            ),
        )


def test_withdrawal_and_new_task_do_not_resurrect_old_constraints():
    old = _constraint()
    fact = MemoryFact(
        value=old.source_quote, source=old.source_ref, origin="user_request", status="withdrawn"
    )
    retained, _ = normalize_constraints(
        [fact], previous=[old], model_call=lambda _: pytest.fail("已知来源不应重复理解")
    )
    assert retained[0].status == "withdrawn"
    new_task, _ = normalize_constraints([], previous=[old], model_call=lambda _: pytest.fail())
    assert new_task == []
    literal = _constraint(
        requirement="HELLO",
        source_quote="招牌必须原样写 HELLO",
        verification="exact",
        scope="output",
    )
    with pytest.raises(ToolError, match="原样文字"):
        verify_prompt_constraints(
            [literal],
            sections={"shot_context": "招牌上写你好"},
            negative_prompt="",
            model_call=lambda _: pytest.fail("字面错误应先拦截"),
        )


def test_receipt_is_invalid_after_edit_and_negative_changes():
    constraint = _constraint()
    sections = {"character_context": "成年女性"}
    receipt = verify_prompt_constraints(
        [constraint],
        sections=sections,
        negative_prompt="",
        model_call=lambda messages: _covered(json.loads(messages[1]["content"]), "成年女性"),
    )
    assert (
        verify_prompt_constraints(
            [constraint], sections=sections, negative_prompt="", model_call=None, receipt=receipt
        )
        == receipt
    )
    for changed, negative in (({"character_context": "男性"}, ""), (sections, "不要成年女性")):
        with pytest.raises(ToolError, match="重新核验"):
            verify_prompt_constraints(
                [constraint],
                sections=changed,
                negative_prompt=negative,
                model_call=None,
                receipt=receipt,
            )


def test_semantic_empty_sections_are_explicit_absence_not_invented_people():
    from pydantic import ValidationError

    from kantoku.domains.comic.models import CompleteImagePromptOutput

    result = {
        "character_context": {},
        "world_context": {"location": "废弃天文台"},
        "style_context": {},
        "shot_context": {},
    }
    contexts = ComicPromptContexts.model_validate(result, context={"semantic_scope": True})
    sections = contexts.sections()
    text = "\n\n".join(
        heading + "\n" + sections[name] for name, heading in PROMPT_SECTIONS.items()
    ).strip()
    parsed = prompt_sections(text, allow_empty=True)
    assert parsed["character_context"] == parsed["shot_context"] == ""
    assert ComicPromptContexts.from_sections(parsed, semantic_scope=True) == contexts
    with pytest.raises(ValidationError):
        ComicPromptContexts.model_validate(result)
    with pytest.raises(ValidationError, match="空段落"):
        ComicPromptContexts.model_validate(
            {name: {} for name in PROMPT_SECTIONS}, context={"semantic_scope": True}
        )
    assert set(CompleteImagePromptOutput.model_json_schema()["required"]) == {
        *PROMPT_SECTIONS,
        "director_summary",
        "negative_prompt",
    }
    from kantoku.domains.comic.director import execute_director_stage

    plan = _responses()[SKILLS[1]]["director_plan"]
    plan.update(
        characters_present=False,
        character_presence=None,
        character_pose=None,
        character_expression=None,
    )
    calls = []

    def model(messages):
        calls.append(messages)
        return json.dumps(plan, ensure_ascii=False)

    output = execute_director_stage(
        SKILLS[1], {}, {"context": {}, "execution_plan_required": True}, model_call=model
    )
    assert len(calls) == 1
    assert output["director_plan"]["character_pose"] is None
    assert output["director_plan"]["characters_present"] is False


def test_direct_import_retry_restart_preserves_origin_and_no_fake_shot(media):
    app, pid, cid = media
    request, image = _upload(app, pid, cid)
    assert app.import_comic_image(pid, request) == image
    assert ComicProjectStore(app.runtime_store.path).get(pid).project.current_version == 2
    assert app.comic_media.get(image["image_id"]).artifact_id == image["artifact_id"]
    artifact = RuntimeStore(app.runtime_store.path).get_artifact(image["artifact_id"])
    assert Path(artifact.location).read_bytes() == _png()
    assert artifact.conversation_id == cid and artifact.metadata["request_id"] == "upload"
    assert app.comic_projects.get(pid).project.director_id is None
    assert app.comic_storyboards.list(pid) == []
    assert app.comic_media.list(pid)[0]["selection"] is None
    with pytest.raises(ToolError, match="不同内容"):
        app.import_comic_image(pid, {**request, "filename": "other.png"})


def test_upload_new_version_preserves_selection_and_fixed_reference_roles(media):
    app, pid, cid = media
    _, image = _upload(app, pid, cid)
    selected = app.select_comic_image(
        image["image_id"],
        {
            "request_id": "select",
            "conversation_id": cid,
            "expected_project_version": 2,
            "expected_selection_version": 0,
            "version": 1,
        },
    )
    binding = {
        "image": {"image_id": image["image_id"], "version": 1},
        "roles": ["identity", "style"],
        "target": "主角",
    }
    app.bind_comic_references(
        pid,
        {
            "request_id": "bind",
            "conversation_id": cid,
            "expected_project_version": selected["project_version"],
            "expected_reference_version": 0,
            "bindings": [binding],
        },
    )
    _, revised = _upload(
        app,
        pid,
        cid,
        "revision",
        image_id=image["image_id"],
        expected_image_version=1,
        parent=binding["image"],
        data_url="data:image/png;base64," + base64.b64encode(_png(120)).decode(),
    )
    assert revised["version"] == 2 and revised["artifact_id"] != image["artifact_id"]
    assert app.comic_media.list(pid)[0]["selection"]["selected_version"] == 1
    context = app.comic_media.reference_context(pid, cid)
    assert context["bindings"][0]["image"]["version"] == 1
    assert context["bindings"][0]["roles"] == ["identity", "style"]
    assert context["bindings"][0]["observation_status"] == "not_analyzed"
    assert (
        Path(app.runtime_store.get_artifact(image["artifact_id"]).location).read_bytes() == _png()
    )


def test_concurrent_duplicate_and_stale_writes_have_one_effect(media):
    app, pid, cid = media
    request = ComicImageImportRequest(
        request_id="same",
        conversation_id=cid,
        expected_project_version=1,
        filename="portrait.png",
        data_url="data:image/png;base64," + base64.b64encode(_png()).decode(),
    )

    def submit():
        return app.comic_media.import_image(
            pid, request, output_dir=web_studio.get_settings().image.output_dir
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert results[0] == results[1] and len(app.comic_media.list(pid)) == 1
    assert len(app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)) == 1
    stale = request.model_copy(update={"request_id": "different"})
    with pytest.raises(ToolError, match="其他操作更新"):
        app.comic_media.import_image(
            pid, stale, output_dir=web_studio.get_settings().image.output_dir
        )
    assert len(app.comic_media.list(pid)) == 1


def test_cross_chat_missing_content_and_artifact_lifecycle_are_rejected(media):
    app, pid, cid = media
    _, image = _upload(app, pid, cid)
    foreign = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    with pytest.raises(ToolError, match="其他会话"):
        app.list_comic_images(pid, foreign)
    related = app.create_comic_project({"title": "同聊天关联作品"})["project"]["project_id"]
    binding = {
        "image": {"image_id": image["image_id"], "version": 1},
        "roles": ["style"],
        "target": "整体画风",
    }
    app.bind_comic_references(
        related,
        {
            "request_id": "explicit-related",
            "conversation_id": cid,
            "expected_project_version": 1,
            "expected_reference_version": 0,
            "bindings": [binding],
        },
    )
    assert (
        app.comic_media.reference_context(related, cid)["bindings"][0]["source_project_id"] == pid
    )
    isolated = app.create_comic_project({"title": "其他聊天作品"})["project"]["project_id"]
    with pytest.raises(ToolError, match="会话范围"):
        app.bind_comic_references(
            isolated,
            {
                "request_id": "foreign",
                "conversation_id": foreign,
                "expected_project_version": 1,
                "expected_reference_version": 0,
                "bindings": [binding],
            },
        )
    assert app.comic_projects.get(isolated).project.current_version == 1
    ref = ImageVersionRef(image_id=image["image_id"], version=1)
    artifact = app.runtime_store.get_artifact(image["artifact_id"])
    app.runtime_store.change_artifact_lifecycle(
        artifact.id, ArtifactLifecycle.TRASHED, expected_version=artifact.lifecycle_version
    )
    with pytest.raises(ToolError, match="失效"):
        app.comic_media.require_image(ref, pid, cid)
    app.runtime_store.change_artifact_lifecycle(
        artifact.id, ArtifactLifecycle.ACTIVE, expected_version=2
    )
    Path(artifact.location).write_bytes(_png(200))
    with pytest.raises(ToolError, match="已改变"):
        app.comic_media.require_image(ref, pid, cid)


def test_http_media_contract_and_reference_reads_are_model_free(media, server):
    app, pid, cid = media
    connection = HTTPConnection("127.0.0.1", server)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}

    def request(method, path, data=None):
        connection.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status < 300, result
        return result

    try:
        image = request(
            "POST",
            f"/api/comic/projects/{pid}/images",
            {
                "request_id": "http",
                "conversation_id": cid,
                "expected_project_version": 1,
                "filename": "one.png",
                "data_url": "data:image/png;base64," + base64.b64encode(_png()).decode(),
            },
        )
        request(
            "PUT",
            f"/api/comic/images/{image['image_id']}/selection",
            {
                "request_id": "choose",
                "conversation_id": cid,
                "expected_project_version": 2,
                "expected_selection_version": 0,
                "version": 1,
            },
        )
        request(
            "PUT",
            f"/api/comic/projects/{pid}/references",
            {
                "request_id": "reference",
                "conversation_id": cid,
                "expected_project_version": 3,
                "expected_reference_version": 0,
                "bindings": [
                    {
                        "image": {"image_id": image["image_id"], "version": 1},
                        "roles": ["composition"],
                        "target": "画面布局",
                    }
                ],
            },
        )
        assert (
            len(request("GET", f"/api/comic/projects/{pid}/images?conversation_id={cid}")["images"])
            == 1
        )
        assert (
            len(
                request(
                    "GET", f"/api/comic/images/{image['image_id']}/versions?conversation_id={cid}"
                )["versions"]
            )
            == 1
        )
        assert (
            request("GET", f"/api/comic/projects/{pid}/references?conversation_id={cid}")[
                "reference_version"
            ]
            == 1
        )
    finally:
        connection.close()


def test_reference_roles_reach_actual_director_input_and_require_new_revision(
    app, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        web_studio.get_settings().image, "output_dir", tmp_path / "output", raising=False
    )
    pid, first, calls = _generate(app, monkeypatch)
    cid = app.runtime_store.get_run(first["run_id"]).state["conversation_id"]
    _, image = _upload(app, pid, cid)
    binding = {
        "image": {"image_id": image["image_id"], "version": 1},
        "roles": ["style"],
        "target": "整体画风",
    }
    refs = app.bind_comic_references(
        pid,
        {
            "request_id": "style",
            "conversation_id": cid,
            "expected_project_version": app.comic_projects.get(pid).project.current_version,
            "expected_reference_version": 0,
            "bindings": [binding],
        },
    )
    calls.clear()
    result = app.create_comic_director(
        pid,
        {
            "creation_mode": "professional",
            "conversation_id": cid,
            "expected_project_version": refs["project_version"],
            "task": "使用已选风格参考",
        },
    )
    assert result["status"] == "completed"
    actual = json.loads(calls[0][1]["content"])["context"]["image_references"]
    assert actual["bindings"][0]["roles"] == ["style"]
    assert actual["bindings"][0]["artifact_id"] == image["artifact_id"]
    assert actual["bindings"][0]["observation_status"] == "not_analyzed"
    assert result["director_spec"]["reference_context"] == actual
    changed = app.bind_comic_references(
        pid,
        {
            "request_id": "identity",
            "conversation_id": cid,
            "expected_project_version": app.comic_projects.get(pid).project.current_version,
            "expected_reference_version": 1,
            "bindings": [{**binding, "roles": ["identity"]}],
        },
    )
    with pytest.raises(ToolError, match="参考用途已变化"):
        app.create_comic_director(
            pid,
            {
                "creation_mode": "professional",
                "conversation_id": cid,
                "expected_project_version": changed["project_version"],
                "review_current": True,
                "expected_director_version": result["director_spec"]["version"],
            },
        )


def test_actual_semantic_prompt_skips_keyword_relocation_and_rechecks_manual_edit(
    tmp_path, monkeypatch
):
    runtime = RuntimeStore(tmp_path / "runtime.db")
    projects = ComicProjectStore(runtime.path)
    assets = ComicAssetStore(projects, runtime)
    boards = ComicStoryboardStore(projects, assets)
    prompts = ComicPromptStore(projects, boards, assets, runtime)
    constraints = [
        _constraint(),
        _constraint(
            constraint_id="sign",
            requirement="OPEN 24/7",
            target="招牌",
            scope="output",
            verification="exact",
            source_quote="OPEN 24/7",
        ),
    ]
    hard = [item.requirement for item in constraints]
    snapshot = projects.create(
        ComicProjectInput.model_validate(
            {
                "title": "语义肖像",
                "brief": {
                    "original_request": "请做一张成年女性的写实肖像，招牌文字必须为 OPEN 24/7",
                    "hard_constraints": hard,
                    "constraint_contract_version": 2,
                    "semantic_constraints": [item.model_dump(mode="json") for item in constraints],
                },
            }
        )
    )
    pid = snapshot.project.project_id
    director = projects.save_director(
        pid,
        DirectorSpecDraft(
            visual_direction="自然写实",
            storytelling_goal="人物肖像",
            camera_language="近距离肖像",
            composition="人物居中",
            lighting="自然光",
            color_language="柔和色彩",
            emotion="平静",
            character_focus="人物",
            constraints=hard,
            creative_choices=["选择自然光表现肤质"],
        ),
        expected_project_version=1,
    )
    board = boards.create(
        pid,
        ComicStoryboardDraft(title="肖像"),
        expected_project_version=2,
        director_spec_version=director.version,
    )
    shot = boards.add_shot(
        board.storyboard_id,
        ComicShotDraft(purpose="肖像", subject="人物", action="坐着", environment="室内"),
        expected_project_version=3,
        expected_storyboard_version=1,
    )
    snapshot, director, board, shot, selected = prompts.source(shot.shot_id)
    monkeypatch.setattr(
        StructuredImagePromptCompiler,
        "_separate_contexts",
        lambda _: pytest.fail("新动态路径不能调用关键词搬移"),
    )
    result = {
        "director_summary": "人物肖像",
        "character_context": {
            "identity": "她是一名成年女性",
            "fixed_features": "手臂纹身图案为远景山峦",
        },
        "world_context": {"location": "室内"},
        "style_context": {"photographic_style": "自然写实"},
        "shot_context": {"action": "坐着"},
        "negative_prompt": "避免改变成年人物的身份",
    }
    seen = []

    def model(messages):
        payload = json.loads(messages[1]["content"])
        seen.append(payload)
        if "sections" not in payload:
            return json.dumps(result, ensure_ascii=False)
        return json.dumps(
            {
                "complete": True,
                "scope_valid": True,
                "findings": [
                    {
                        "constraint_id": item["constraint_id"],
                        "status": "covered",
                        "evidence_path": "sections.shot_context.literal_text"
                        if item["verification"] == "exact"
                        else "sections.character_context.identity",
                    }
                    for item in payload["constraints"]
                ],
            }
        )

    draft = compiler_for_model("external").compile(
        snapshot=snapshot,
        director=director,
        storyboard=board,
        shot=shot,
        assets=selected,
        model_target="external",
        model_call=model,
        complete_prompt=True,
    )
    assert len(seen) == 2 and "required_constraints" in seen[0]
    assert "手臂纹身图案为远景山峦" in draft.character_context
    assert "请做一张" not in draft.positive_prompt
    assert "指定文字：招牌的文字为“OPEN 24/7”" in draft.shot_context
    assert "literal_text" in draft.context_sources["input_lineage"]
    assert draft.context_sources["bible_contract"] == SEMANTIC_BIBLE_CONTRACT
    run = runtime.create_run("comic", "comic.prompt.compile", {}, "prompt.compile")
    saved = prompts.save(
        shot.shot_id,
        draft,
        expected_project_version=4,
        expected_shot_version=1,
        model_target="external",
        compiler_version="test",
        run_id=run.id,
    )
    edited = ComicPromptDraft.model_validate(
        saved.model_dump(include=set(ComicPromptDraft.model_fields))
    )
    edited.positive_prompt = edited.positive_prompt.replace("成年女性", "年轻男性")
    edited.context_sources = copy.deepcopy(edited.context_sources)
    # 客户端不能提交一个新的 pass 凭据跳过核验。
    edited.context_sources["constraint_review"] = {"sha256": "forged"}
    with pytest.raises(ToolError, match="遗漏"):
        prompts.save(
            shot.shot_id,
            edited,
            expected_project_version=5,
            expected_shot_version=1,
            expected_version=1,
            model_target="external",
            compiler_version="test",
            run_id=run.id,
            source="edited",
            constraint_model_call=lambda _: json.dumps(
                {"complete": False, "scope_valid": True, "findings": []}
            ),
        )
    assert prompts.get(shot.shot_id).artifact_id == saved.artifact_id
    assert all(heading in saved.positive_prompt for heading in PROMPT_SECTIONS.values())
