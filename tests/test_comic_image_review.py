"""少量契约回归：实际媒体/HTTP/编译输入，模型全部为明确离线替身。"""

import base64
import json
import struct
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from kantoku.config import ToolError, get_settings
from kantoku.core import llm
from kantoku.core.runtime.models import ArtifactLifecycle, ArtifactType
from kantoku.domains.comic.director_provenance import value_hash
from kantoku.domains.comic.models import ImageVersionRef
from kantoku.shells import web_studio
from kantoku.tools.image_gen import LocalFakeImageProvider


def png(color=0):
    def chunk(name, data):
        return (struct.pack(">I", len(data)) + name + data
                + struct.pack(">I", zlib.crc32(name + data)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, color, 0, 0]))) + chunk(b"IEND", b""))


@pytest.fixture
def review_app(monkeypatch, tmp_path):
    settings = get_settings().model_copy(deep=True)
    settings.storage.sqlite_path = tmp_path / "review.db"
    settings.image.output_dir = tmp_path / "images"
    monkeypatch.setattr(web_studio, "get_settings", lambda: settings)
    # Match the production topology: FileCatalog and domain writes share one DB.
    from kantoku.core.runtime import storage
    monkeypatch.setattr(storage, "get_settings", lambda: settings)
    monkeypatch.setattr(web_studio, "_provider", lambda: LocalFakeImageProvider(
        tmp_path / "fake", model_id="offline-test", actual_fen=0))
    def forbidden(*_args, **_kwargs):
        pytest.fail("离线验收禁止任何未替换的付费调用")
    monkeypatch.setattr(web_studio, "vision_chat", forbidden)
    monkeypatch.setattr(web_studio, "chat", forbidden)
    application = web_studio.StudioApplication()
    monkeypatch.setattr(application, "_comic_image_vision_model", forbidden)
    monkeypatch.setattr(application, "_comic_constraint_model", forbidden)
    try:
        yield application
    finally:
        application.runner.close()


def setup_image(app, *, constraints=None):
    cid = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    pid = app.create_comic_project({"title": "QA 素材", "brief": {
        "original_request": "制作独立画面", "hard_constraints": constraints or []}})["project"][
            "project_id"]
    image = upload(app, pid, cid)
    return pid, cid, {"image_id": image["image_id"], "version": 1}


def common(app, pid, cid, request_id):
    return {"conversation_id": cid, "request_id": request_id,
            "expected_project_version": app.comic_projects.get(pid).project.current_version}


def upload(app, pid, cid, *, color=0, **fields):
    return app.import_comic_image(pid, {**common(app, pid, cid, f"upload-{color}"),
        "filename": "offline-fixture.png", "data_url": "data:image/png;base64,"
        + base64.b64encode(png(color)).decode(), **fields})


def observation(topic="主体"):
    return {"summary": f"离线观察 {topic}", "findings": [
        {"finding_id": "selected", "observation": f"{topic}边缘模糊",
         "suggested_change": f"调整{topic}边缘", "confidence": 0.7,
         "uncertainty": "小尺寸限制", "region": [0.1, 0.1, 0.8, 0.8]},
        {"finding_id": "unselected", "observation": "未选观察",
         "suggested_change": "换成绿色天空", "confidence": 0.5}],
        "protected_features": [f"保留{topic}轮廓"], "limitations": ["离线替身不证明视觉质量"]}


def analyze(app, pid, cid, image, monkeypatch, task="检查主体", output=None, request_id="analyze"):
    seen = []
    def model(messages):
        context = json.loads(messages[-1]["content"][0]["text"])
        if context.get("stage") == "constraint_extraction_review":
            return app._comic_constraint_model([messages[0],
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)}])
        seen.append(messages)
        return json.dumps(output if output is not None else observation(), ensure_ascii=False)
    monkeypatch.setattr(app, "_comic_image_vision_model", model)
    request = {**common(app, pid, cid, request_id), "image": image, "task": task}
    return app.comic_image_operation(pid, "analysis", request), request, seen


def edit(app, pid, cid, image, report, monkeypatch, *, task="检查主体", approved=True,
         request_id="edit", request_fields=None):
    seen = []
    def model(messages):
        payload = json.loads(messages[-1]["content"])
        seen.append(payload)
        if isinstance(payload, list):
            return json.dumps({"constraints": [{"source_index": index,
                "source_quote": item["quote"], "target": "本轮授权内容", "scope": "output"}
                for index, item in enumerate(payload)]}, ensure_ascii=False)
        if payload.get("stage") == "constraint_extraction_review":
            return json.dumps({"complete": True, "scope_valid": approved,
                "constraint_replacements": [], "findings": [
                {"constraint_id": item["constraint_id"], "status": "covered",
                 "evidence_path": f"constraints.{item['constraint_id']}.source_quote"}
                for item in payload["constraints"]]})
        if "sections" in payload:
            return json.dumps({"complete": True, "scope_valid": approved, "findings": [
                {"constraint_id": item["constraint_id"], "status": "covered",
                 "evidence_path": "sections.positive_prompt"} for item in payload["constraints"]],
                "edit_findings": [{"constraint_id": item["requirement_id"], "status": "covered",
                    "evidence_path": "sections.positive_prompt"}
                    for item in payload.get("authorized_requirements", [])]})
        return json.dumps({"positive_prompt": "只修改选中的边缘，保留其余轮廓与当前约束。",
                           "negative_prompt": "", "summary": "离线修改指令"}, ensure_ascii=False)
    monkeypatch.setattr(app, "_comic_constraint_model", model)
    request = {**common(app, pid, cid, request_id), "image": image, "task": task,
               "analysis_artifact_id": report["artifact_id"],
               "selected_finding_ids": ["selected"], **(request_fields or {})}
    return app.comic_image_operation(pid, "edit", request), request, seen


def test_legacy_shot_upload_is_read_only_and_retains_original_when_editing(review_app, monkeypatch):
    app = review_app
    pid, cid, imported = setup_image(app)
    original = app.comic_media.get(imported["image_id"])
    artifact = app.runtime_store.get_artifact(original.artifact_id).model_copy(update={
        "id": "artifact-legacy-shot", "source": "comic.external_image",
        "metadata": {"origin": "real", "creation_mode": "external_upload", "project_id": pid,
                     "shot_id": "legacy-shot", "prompt_artifact_id": "legacy-prompt",
                     "sha256": original.sha256, "filename": "original.png"}})
    app.runtime_store.save_artifact(artifact)
    image = {"image_id": artifact.id, "version": 1}
    project_version = app.comic_projects.get(pid).project.current_version
    before = app.comic_media.list(pid)
    projected = app.comic_media.require_image(
        ImageVersionRef.model_validate(image), pid, cid)
    assert projected.artifact_id == artifact.id and projected.sha256 == original.sha256
    assert app.comic_media.list(pid) == before
    assert app.comic_projects.get(pid).project.current_version == project_version
    assert app.comic_media.versions(artifact.id) == [projected]
    report, _, seen = analyze(app, pid, cid, image, monkeypatch)
    assert seen[0][1]["content"][2]["image_url"]["url"].endswith(base64.b64encode(png()).decode())
    instruction, _, _ = edit(app, pid, cid, image, report, monkeypatch)
    app.comic_image_operation(pid, "confirm", {**common(app, pid, cid, "legacy-confirm"),
        "instruction_artifact_id": instruction["artifact_id"]})
    newer = upload(app, pid, cid, color=21, image_id=artifact.id, expected_image_version=1,
                   parent=image, instruction_artifact_id=instruction["artifact_id"])
    assert newer["version"] == 2 and newer["artifact_id"] != artifact.id
    assert [item.version for item in app.comic_media.versions(artifact.id)] == [2, 1]
    assert app.runtime_store.get_artifact(artifact.id) == artifact
    other = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    with pytest.raises(ToolError, match="范围"):
        app.comic_media.require_image(ImageVersionRef.model_validate(image), pid, other)
    fake = artifact.model_copy(update={"id": "artifact-not-real", "metadata": {
        **artifact.metadata, "origin": "mock"}})
    app.runtime_store.save_artifact(fake)
    with pytest.raises(ToolError, match="找不到指定图片版本"):
        app.comic_media.get(fake.id)


@pytest.mark.parametrize("topic", ["机械鱼", "山间拱桥"])
def test_full_loop_selected_scope_actual_images_and_manual_selection(
    review_app, monkeypatch, topic,
):
    app = review_app
    pid, cid, image = setup_image(app)
    report, request, seen = analyze(app, pid, cid, image, monkeypatch,
                                    task=f"检查{topic}", output=observation(topic))
    assert seen[0][1]["content"][2]["image_url"]["url"].endswith(base64.b64encode(png()).decode())
    result, _, calls = edit(app, pid, cid, image, report, monkeypatch, task=request["task"])
    assert calls[0]["selected_findings"][0]["finding_id"] == "selected"
    assert "unselected" not in json.dumps(calls)
    assert "绿色天空" not in json.dumps(calls, ensure_ascii=False)
    assert app.runtime_store.get_artifact(result["artifact_id"]).type == ArtifactType.PROMPT
    app.comic_image_operation(pid, "confirm", {**common(app, pid, cid, "confirm"),
                             "instruction_artifact_id": result["artifact_id"]})
    restored = app.comic_image_reports(pid, cid, "edit", result["artifact_id"])
    assert restored["requires_confirmation"] is False and restored["confirmation"]["confirmed"]
    newer = upload(app, pid, cid, color=20, image_id=image["image_id"], expected_image_version=1,
                   parent=image, instruction_artifact_id=result["artifact_id"])
    assert newer["version"] == 2
    assert app.comic_media.list(pid)[0]["selection"] is None
    compared = []
    def model(messages):
        compared.append(messages)
        payload = json.loads(messages[1]["content"][0]["text"])
        return json.dumps({"summary": "两图公开对比", "changes": [{
            "check_id": item["check_id"], "status": "uncertain", "evidence": "尺寸过小",
            "confidence": 0.2} for item in payload["changes"]], "preservation": [{
            "check_id": item["check_id"], "status": "satisfied", "evidence": "轮廓保持",
            "confidence": 0.5} for item in payload["preservation"]],
            "other_observations": [], "limitations": ["人工判断"]})
    monkeypatch.setattr(app, "_comic_image_vision_model", model)
    request = {**common(app, pid, cid, "compare"), "before": image,
               "after": {"image_id": image["image_id"], "version": 2},
               "instruction_artifact_id": result["artifact_id"]}
    comparison = app.comic_image_operation(pid, "comparison", request)
    urls = [part["image_url"]["url"] for part in compared[0][1]["content"]
            if part["type"] == "image_url"]
    assert len(urls) == 2 and urls[0] != urls[1]
    assert comparison["advisory"] is True
    assert app.comic_media.list(pid)[0]["selection"] is None
    app.select_comic_image(image["image_id"], {**common(app, pid, cid, "select-old"),
                          "version": 1, "expected_selection_version": 0})
    assert app.comic_media.list(pid)[0]["selection"]["selected_version"] == 1
    assert len(app.comic_media.versions(image["image_id"])) == 2


def test_concurrent_idempotency_restart_and_unknown_never_resend(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app)
    report, request, seen = analyze(app, pid, cid, image, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: app.comic_image_operation(pid, "analysis", request),
                                    range(2)))
    assert all(item["artifact_id"] == report["artifact_id"] for item in results) and len(seen) == 1
    restarted = web_studio.StudioApplication()
    try:
        assert restarted.comic_image_operation(pid, "analysis", request) == report
        with pytest.raises(ToolError, match="不同内容"):
            restarted.comic_image_operation(pid, "analysis", {**request, "task": "不同任务"})
    finally:
        restarted.runner.close()
    attempts = []
    def unavailable(_):
        attempts.append(True)
        raise TimeoutError("offline unknown")
    monkeypatch.setattr(app, "_comic_image_vision_model", unavailable)
    failed = {**common(app, pid, cid, "unknown"), "image": image, "task": "新检查"}
    with pytest.raises(TimeoutError):
        app.comic_image_operation(pid, "analysis", failed)
    with pytest.raises(ToolError, match="不会自动重发"):
        app.comic_image_operation(pid, "analysis", failed)
    assert len(attempts) == 1


def test_valid_empty_report_and_invalid_response_are_distinct(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app)
    clean = {**observation(), "findings": []}
    report, _, _ = analyze(app, pid, cid, image, monkeypatch, output=clean)
    assert report["output"]["findings"] == [] and report["advisory"]
    with pytest.raises(ToolError, match="有效公开结构"):
        analyze(app, pid, cid, image, monkeypatch, output={"private_reasoning": "never save"},
                request_id="bad-output")
    assert len(app.comic_image_reports(pid, cid, "analysis")["items"]) == 1


@pytest.mark.parametrize("change", ["task", "constraints", "references", "content", "conversation"])
def test_report_rejects_changed_basis_before_edit_call(review_app, monkeypatch, change):
    app = review_app
    pid, cid, image = setup_image(app)
    report, _, _ = analyze(app, pid, cid, image, monkeypatch)
    request = {**common(app, pid, cid, "edit-stale"), "image": image, "task": "检查主体",
               "analysis_artifact_id": report["artifact_id"], "selected_finding_ids": ["selected"]}
    if change == "task":
        request["task"] = "另一任务"
    elif change == "constraints":
        app.update_comic_brief(pid, {"expected_version": request["expected_project_version"],
            "original_request": "制作独立画面", "hard_constraints": ["保留所有背景"]})
        request.update(common(app, pid, cid, "edit-stale"))
    elif change == "references":
        app.bind_comic_references(pid, {**common(app, pid, cid, "bind"),
            "expected_reference_version": 0, "bindings": [
                {"image": image, "roles": ["composition"], "target": "布局"}]})
        request.update(common(app, pid, cid, "edit-stale"))
    elif change == "content":
        artifact = app.runtime_store.get_artifact(
            app.comic_media.get(image["image_id"]).artifact_id)
        Path(artifact.location).write_bytes(png(30))
        with pytest.raises(ToolError, match="内容"):
            app.get_core_artifact_content(artifact.id)
    else:
        request["conversation_id"] = app.create_conversation({
            "interaction_mode": "guided", "domain": "comic"})["id"]
    with pytest.raises(ToolError):
        app.comic_image_operation(pid, "edit", request)


def test_unconfirmed_import_and_scope_conflict_are_blocked(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app, constraints=["主体轮廓不能改变"])
    report, _, _ = analyze(app, pid, cid, image, monkeypatch)
    with pytest.raises(ToolError, match="作用范围"):
        edit(app, pid, cid, image, report, monkeypatch, approved=False)
    assert app.comic_image_reports(pid, cid, "edit")["items"] == []
    # The rejected request remains unavailable; a new explicit request can make another attempt.
    report2, _, _ = analyze(app, pid, cid, image, monkeypatch, request_id="new-analysis")
    instruction, _, _ = edit(app, pid, cid, image, report2, monkeypatch, request_id="edit-new")
    with pytest.raises(ToolError, match="先确认"):
        upload(app, pid, cid, color=60, parent=image,
               instruction_artifact_id=instruction["artifact_id"])
    assert len(app.comic_media.list(pid)) == 1


def test_http_persisted_report_queries_are_read_only(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app)
    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(observation()))
    server = web_studio.make_server(app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}
    try:
        path = f"/api/comic/projects/{pid}/image-analyses"
        connection.request("POST", path, json.dumps({**common(app, pid, cid, "http-analysis"),
            "image": image, "task": "检查主体"}), headers)
        response = connection.getresponse()
        assert response.status == 201
        result = json.loads(response.read())
        monkeypatch.setattr(app, "_comic_image_vision_model",
                            lambda _: pytest.fail("查询不能调用模型"))
        for suffix in ("", "/" + result["artifact_id"]):
            connection.request("GET", path + suffix + "?conversation_id=" + cid, headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        def blocked_edit(messages):
            payload = json.loads(messages[-1]["content"])
            if "sections" in payload:
                return json.dumps({"complete": True, "scope_valid": True, "findings": [],
                    "edit_findings": []})
            return json.dumps({"positive_prompt": "离线待核验正文", "negative_prompt": "",
                               "summary": "测试失败投影"})
        monkeypatch.setattr(app, "_comic_constraint_model", blocked_edit)
        connection.request("POST", f"/api/comic/projects/{pid}/edit-instructions", json.dumps({
            **common(app, pid, cid, "http-edit"), "image": image, "task": "检查主体",
            "analysis_artifact_id": result["artifact_id"], "selected_finding_ids": ["selected"]}),
            headers)
        response = connection.getresponse()
        assert response.status == 400
        failure = json.loads(response.read())
        assert failure["error_id"] and failure["trace_id"]
        assert failure["review_diagnostics"]["issues"][0]["finding_ids"] == ["selected"]
        assert failure["review_diagnostics"]["diagnostic_artifact_id"]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_edit_review_uses_frozen_targets_and_persists_missing_and_scope_failures(
    review_app, monkeypatch,
):
    app = review_app
    pid, cid, image = setup_image(app)
    report, _, _ = analyze(app, pid, cid, image, monkeypatch)
    scope = {"mode": "regions", "regions": [{"region_id": "selected-region",
        "label": "区域1", "bounds": [0.1, 0.1, 0.8, 0.8], "finding_id": "selected",
        "instruction": ""}], "whole_finding_ids": [],
        "instruction_target": {"mode": "selected_regions"}}
    for verdict in ("missing", "scope", "fabricated", "normalize"):
        calls = []
        def model(messages, verdict=verdict, calls=calls):
            payload = json.loads(messages[-1]["content"])
            calls.append(payload)
            if isinstance(payload, list):
                return json.dumps({"constraints": [{"source_index": 0,
                    "source_quote": payload[0]["quote"], "target": "本轮区域", "scope": "output"}]})
            if payload.get("stage") == "constraint_extraction_review":
                identity = payload["constraints"][0]["constraint_id"]
                return json.dumps({"complete": False, "scope_valid": False,
                    "constraint_replacements": [], "findings": [{
                    "constraint_id": identity, "status": "conflict",
                    "reason": "离线输入冲突投影", "evidence_path":
                    "constraints." + identity + ".source_quote"}]})
            if "sections" not in payload:
                return json.dumps({"positive_prompt": "只修改指定区域，保留其他内容。",
                                   "negative_prompt": "", "summary": "离线指令"})
            assert payload["edit_scope"] == scope
            assert [item["requirement_id"] for item in payload["authorized_requirements"]] == [
                "finding:selected"]
            assert payload["authorized_requirements"][0]["target"] == {
                "mode": "regions", "region_ids": ["selected-region"]}
            assert "unselected" not in json.dumps(payload["edit_context"])
            finding = {"constraint_id": "finding:selected", "status": "covered",
                       "evidence_path": "sections.positive_prompt"}
            if verdict == "fabricated":
                finding.update(evidence_path=None, quote="不存在于正文的证据")
            return json.dumps({"complete": True, "scope_valid": verdict != "scope",
                "findings": [], "edit_findings": [] if verdict == "missing" else [finding],
                "scope_issues": [{"requirement_ids": ["finding:selected"],
                    "reason": "局部要求被扩展到区域外"}] if verdict == "scope" else []})
        monkeypatch.setattr(app, "_comic_constraint_model", model)
        with pytest.raises(ToolError) as raised:
            app.comic_image_operation(pid, "edit", {**common(app, pid, cid, verdict),
                "image": image, "task": "检查主体", "analysis_artifact_id": report["artifact_id"],
                "selected_finding_ids": ["selected"], "edit_scope": scope, "round_id": verdict,
                "instruction": "区域颜色改为蓝色，同时必须保持红色"
                if verdict == "normalize" else ""})
        assert len(calls) == 2
        failure = raised.value._kantoku_public_failure
        diagnostic = failure["review_diagnostics"]
        assert diagnostic["stage"] == ("normalize" if verdict == "normalize" else "final_review")
        assert diagnostic["issues"][0]["finding_ids"] == (
            [] if verdict == "normalize" else ["selected"])
        if verdict == "normalize":
            assert "user-instruction" in diagnostic["issues"][0]["requirement_ids"]
        assert diagnostic["issues"][0]["region_ids"] == ["selected-region"]
        audit = app.runtime_store.get_artifact(diagnostic["diagnostic_artifact_id"])
        assert audit.type == ArtifactType.JSON and audit.metadata["internal"] is True
        assert audit.conversation_id == cid and audit.metadata["source_image"] == image
        assert audit.metadata["round_id"] == verdict and audit.metadata["trace_id"]
        run = app.runtime_store.get_run(diagnostic["run_id"])
        assert run.state["review_diagnostics"] == diagnostic
        assert any(item["id"] == run.id for item in app.list_comic_project_tasks(pid)["tasks"])
    assert app.comic_image_reports(pid, cid, "edit")["items"] == []


def test_vision_single_attempt_preserves_old_default_and_usage(monkeypatch):
    settings = get_settings().model_copy(deep=True)
    settings.llm.retry = 2
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    monkeypatch.setattr(type(settings.llm), "vision_api_key", lambda _: "offline-key")
    calls = []
    def provider(**kwargs):
        calls.append(kwargs)
        if kwargs["on_usage"]:
            kwargs["on_usage"](3, 4, True)
        return SimpleNamespace(content="{}")
    monkeypatch.setattr(llm, "_call_provider", provider)
    usage = []
    llm.vision_chat([], single_attempt=True, on_usage=lambda *args: usage.append(args))
    llm.vision_chat([])
    assert [call["retry"] for call in calls] == [0, 2] and usage == [(3, 4, True)]


def test_reference_pixels_and_literal_edit_are_verified(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app)
    reference = upload(app, pid, cid, color=80)
    app.bind_comic_references(pid, {**common(app, pid, cid, "bind-ref"),
        "expected_reference_version": 0, "bindings": [{"image": {
            "image_id": reference["image_id"], "version": 1}, "roles": ["style"],
            "target": "当前画面"}]})
    report, _, seen = analyze(app, pid, cid, image, monkeypatch)
    assert len([part for part in seen[0][1]["content"] if part["type"] == "image_url"]) == 2
    instruction = "把铭牌上的文字原样改成 UNIT 37"
    calls = []
    def model(messages):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        if isinstance(payload, list):
            return json.dumps({"constraints": [{"source_index": 0,
                "source_quote": instruction, "requirement": "铭牌文字为 UNIT 37",
                "target": "铭牌", "scope": "output", "verification": "exact",
                "literal_value": "UNIT 37"}]}, ensure_ascii=False)
        if payload.get("stage") == "constraint_extraction_review":
            return json.dumps({"complete": True, "scope_valid": True,
                "constraint_replacements": [], "findings": [{
                "constraint_id": payload["constraints"][0]["constraint_id"],
                "status": "covered", "quote": instruction}]}, ensure_ascii=False)
        if "sections" in payload:
            return json.dumps({"complete": True, "scope_valid": True, "findings": [{
                "constraint_id": payload["constraints"][0]["constraint_id"],
                "status": "covered", "evidence_path": "sections.positive_prompt"}],
                "edit_findings": [{"constraint_id": item["requirement_id"], "status": "covered",
                    "evidence_path": "sections.positive_prompt"}
                    for item in payload["authorized_requirements"]]})
        return json.dumps({"positive_prompt": "将铭牌改成 UNIT 37；保留其余内容。",
                           "negative_prompt": "", "summary": "只修改指定文字"}, ensure_ascii=False)
    monkeypatch.setattr(app, "_comic_constraint_model", model)
    result = app.comic_image_operation(pid, "edit", {**common(app, pid, cid, "literal-edit"),
        "image": image, "task": "检查主体", "analysis_artifact_id": report["artifact_id"],
        "instruction": instruction})
    assert result["edit_constraints"][0]["literal_value"] == "UNIT 37"
    assert len(calls) == 4
    state = app.runtime_store.get_run(result["run_id"]).state
    assert state["media_input"]["instruction"] == instruction


@pytest.mark.parametrize("ambiguous", [False, True])
def test_manual_region_review_receives_actual_pixels_and_keeps_scope_gate(
    review_app, monkeypatch, ambiguous,
):
    app = review_app
    pid, cid, image = setup_image(app, constraints=["右侧铜鱼", "保留左侧铭牌"])
    instruction = "把这个换成陶瓷飞鸟"
    scope = {"mode": "regions", "regions": [{"region_id": "right", "label": "手动1",
        "bounds": [0.55, 0.2, 0.95, 0.85], "finding_id": None,
        "instruction": instruction}]}
    text_calls, vision_calls = [], []

    def text_model(messages):
        payload = json.loads(messages[-1]["content"])
        text_calls.append(payload)
        if isinstance(payload, list):
            assert payload[0]["quote"] == instruction
            return json.dumps({"constraints": [{"source_index": 0,
                "source_quote": instruction, "requirement": "换成陶瓷飞鸟",
                "target": "这个主体", "scope": "identity"}]})
        assert payload.get("stage") != "constraint_extraction_review"
        if "sections" not in payload:
            assert payload["constraints"][0]["enforcement"] == "scoped_replacement"
            return json.dumps({"positive_prompt": "仅在右侧选区换成陶瓷飞鸟，"
                "保留左侧铭牌及框外内容。",
                "negative_prompt": "不扩大范围", "summary": "离线范围契约"})
        return json.dumps({"complete": True, "scope_valid": True, "findings": [{
            "constraint_id": item["constraint_id"], "status": "covered",
            "evidence_path": "sections.positive_prompt"} for item in payload["constraints"]],
            "edit_findings": [{"constraint_id": "region:right", "status": "covered",
                "evidence_path": "sections.positive_prompt"}]})

    def vision_model(messages):
        vision_calls.append(messages)
        parts = messages[1]["content"]
        context = json.loads(parts[0]["text"])
        pixels = [part for part in parts if part["type"] == "image_url"]
        assert len(pixels) == 1
        assert base64.b64decode(pixels[0]["image_url"]["url"].split(",", 1)[1]) == png()
        assert context["edit_basis"]["edit_scope"] == scope
        requirement = context["edit_basis"]["authorized_requirements"][0]
        assert requirement["requirement"] == instruction
        assert requirement["target"] == {"mode": "regions", "region_ids": ["right"]}
        old = context["edit_basis"]["historical_constraints"][0]
        return json.dumps({"complete": not ambiguous, "scope_valid": not ambiguous,
            "findings": [{"constraint_id": item["constraint_id"], "status": "covered",
                "evidence_path": f"constraints.{item['constraint_id']}.source_quote"}
                for item in context["constraints"]],
            "constraint_replacements": [] if ambiguous else [{
                "constraint_id": old["constraint_id"], "requirement_id": "region:right",
                "source_quote": instruction, "reason": "离线视觉替身：仅替换框内对象"}],
            "scope_issues": [{"requirement_ids": ["region:right"],
                "reason": "框内存在多个可能对象"}] if ambiguous else []})

    monkeypatch.setattr(app, "_comic_constraint_model", text_model)
    monkeypatch.setattr(app, "_comic_image_vision_model", vision_model)
    request = {**common(app, pid, cid, "visual-edit"), "image": image,
               "task": "检查当前画面", "edit_scope": scope, "round_id": "visual-round"}
    if ambiguous:
        with pytest.raises(ToolError) as raised:
            app.comic_image_operation(pid, "edit", request)
        assert raised.value.review_diagnostics["issues"][0]["region_ids"] == ["right"]
        assert len(text_calls) == 1
        assert app.comic_image_reports(pid, cid, "edit")["items"] == []
    else:
        result = app.comic_image_operation(pid, "edit", request)
        assert len(text_calls) == 3
        visual_basis = result["scope"]["constraint_resolution"]["visual_basis"]
        assert visual_basis["source_image"] == image
        assert visual_basis["sha256"] == result["basis"]["images"][0]["sha256"]
        assert app.comic_image_operation(pid, "edit", request) == result
        assert len(text_calls) == 3
    assert len(vision_calls) == 1


@pytest.mark.parametrize("old,new,is_text", [
    ("铜制机械鱼", "陶瓷飞鸟", False), ("OPEN 24/7", "UNIT 37", True)])
def test_manual_revision_supersedes_only_its_old_target_and_comparison_keeps_exceptions(
    review_app, monkeypatch, old, new, is_text,
):
    """明确离线替身：重复来源、主体/文字替换及后续真实两图接口的范围契约。"""
    from kantoku.domains.comic.models import ComicProjectInput, CreativeConstraint

    app = review_app
    cid = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    historical = [CreativeConstraint.model_validate({
        "constraint_id": identity, "requirement": text, "target": target,
        "scope": "output" if is_text or identity == "keep-label" else "identity",
        "source_ref": {"id": "message-" + identity, "kind": "message"},
        "source_quote": text, "origin": "user_request", "trust": "source_verified",
        "verification": "exact" if is_text or identity == "keep-label" else "semantic",
        "literal_value": text if is_text or identity == "keep-label" else None,
    }) for identity, text, target in (
        ("old-a", old, "右侧框内主体"), ("old-b", old, "右侧框内主体"),
        ("keep-label", "ARCHIVE 42", "左侧铭牌"))]
    pid = app.comic_projects.create(ComicProjectInput.model_validate({
        "title": "离线迭代契约", "brief": {
        "original_request": old, "hard_constraints": list(dict.fromkeys(
            item.requirement for item in historical)), "constraint_contract_version": 2,
        "semantic_constraints": [item.model_dump(mode="json") for item in historical],
    }})).project.project_id
    imported = upload(app, pid, cid)
    image = {"image_id": imported["image_id"], "version": 1}
    original_brief = app.comic_projects.get(pid).creative_brief
    report, _, _ = analyze(app, pid, cid, image, monkeypatch, task="检查当前画面",
        output={**observation(old), "protected_features": [old + "的位置与形象", "保留左侧铭牌"]},
        request_id="analysis-revision")
    instruction = "将框内的主体替换为" + new
    scope = {"mode": "regions", "regions": [{"region_id": "right",
        "label": "手动1", "bounds": [0.55, 0.2, 0.95, 0.85], "finding_id": None,
        "instruction": instruction}], "whole_finding_ids": [],
        "instruction_target": {"mode": "selected_regions"}}
    calls = []
    def model(messages):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        if isinstance(payload, list):
            return json.dumps({"constraints": [{"source_index": 0,
                "source_quote": instruction, "requirement": new, "target": "右侧框内主体",
                "scope": "output" if is_text else "identity",
                "verification": "exact" if is_text else "semantic",
                "literal_value": new if is_text else None}]})
        if payload.get("stage") == "constraint_extraction_review":
            return json.dumps({"complete": True, "scope_valid": True, "findings": [{
                "constraint_id": item["constraint_id"], "status": "covered",
                "evidence_path": "constraints." + item["constraint_id"] + ".source_quote"}
                for item in payload["constraints"]], "constraint_replacements": [{
                    "constraint_id": identity, "requirement_id": "region:right",
                    "source_quote": instruction, "reason": "人工明确替换同一框内对象；框外仍保留"}
                    for identity in ("old-a", "old-b")]})
        if "sections" not in payload:
            assert payload["historical_constraint_ids"] == [
                item.constraint_id for item in historical]
            assert payload["authorized_requirements"][0]["target"] == {
                "mode": "regions", "region_ids": ["right"]}
            assert "protected_features" not in payload and "task" not in payload
            assert payload["constraints"][0]["enforcement"] == "scoped_replacement"
            assert payload["constraints"][0]["effective_requirement"]["requirement"] == instruction
            assert payload["constraints"][0]["original_requirement"]["requirement"] == old
            return json.dumps({"positive_prompt": "仅在手动1坐标范围内替换为" + new
                + "，保留区域外内容和左侧文字 ARCHIVE 42。", "negative_prompt": "不扩大修改范围",
                "summary": "离线局部替代，不证明模型质量"})
        assert payload["constraints"] == calls[-2]["constraints"]
        assert "protected_features" not in payload["edit_context"]
        return json.dumps({"complete": True, "scope_valid": True, "findings": [{
            "constraint_id": item["constraint_id"], "status": "covered",
            "evidence_path": "sections.positive_prompt"} for item in payload["constraints"]],
            "edit_findings": [{"constraint_id": "region:right", "status": "covered",
                "evidence_path": "sections.positive_prompt"}], "constraint_replacements": []})
    monkeypatch.setattr(app, "_comic_constraint_model", model)
    result = app.comic_image_operation(pid, "edit", {**common(app, pid, cid, "revision"),
        "image": image, "task": "检查当前画面", "edit_scope": scope, "round_id": "revision",
        "analysis_artifact_id": report["artifact_id"]})
    assert len(calls) == 4  # Original normalization, compilation and independent review only.
    assert result["compiler_version"].endswith(":edit-4")
    assert app.comic_projects.get(pid).creative_brief == original_brief
    assert len(result["constraint_receipt"]["review"]["constraint_replacements"]) == 2
    assert result["scope"]["constraint_resolution"]["constraint_replacements"] == result[
        "constraint_receipt"]["review"]["constraint_replacements"]
    def contradictory_model(messages):
        payload = json.loads(messages[-1]["content"])
        output = json.loads(model(messages))
        if "sections" in payload:
            for item in output["findings"]:
                if item["constraint_id"] in {"old-a", "old-b"}:
                    item.update(status="conflict", reason="负向无条件保留旧主体，与框内替换矛盾")
        elif isinstance(payload, dict) and payload.get("mode") == "edit":
            output["negative_prompt"] = "不能改变" + old
        return json.dumps(output)
    monkeypatch.setattr(app, "_comic_constraint_model", contradictory_model)
    with pytest.raises(ToolError) as failed:
        app.comic_image_operation(pid, "edit", {**common(app, pid, cid, "contradictory-edit"),
            "image": image, "task": "检查当前画面", "edit_scope": scope, "round_id": "failed"})
    assert failed.value.review_diagnostics["issues"][0]["region_ids"] == ["right"]
    app.comic_image_operation(pid, "confirm", {**common(app, pid, cid, "confirm-revision"),
        "instruction_artifact_id": result["artifact_id"]})
    newer = upload(app, pid, cid, color=17, image_id=image["image_id"], expected_image_version=1,
                   parent=image, instruction_artifact_id=result["artifact_id"])
    def compare_model(messages):
        context = json.loads(messages[1]["content"][0]["text"])
        checks = {item["check_id"]: item for item in context["preservation"]}
        assert checks["preserve-constraint-old-a"]["authorized_exceptions"][0]["target"] == {
            "mode": "regions", "region_ids": ["right"]}
        assert "authorized_exceptions" not in checks["preserve-constraint-keep-label"]
        assert context["changes"][0]["requirement"] == instruction
        return json.dumps({"summary": "离线两图契约", "changes": [{"check_id": item["check_id"],
            "status": "uncertain", "evidence": "离线图像不证明修改完成", "confidence": 0.1}
            for item in context["changes"]], "preservation": [{"check_id": item["check_id"],
            "status": "uncertain", "evidence": "尺寸过小", "confidence": 0.1}
            for item in context["preservation"]], "other_observations": [], "limitations": []})
    monkeypatch.setattr(app, "_comic_image_vision_model", compare_model)
    app.comic_image_operation(pid, "comparison", {**common(app, pid, cid, "compare-revision"),
        "before": image, "after": {"image_id": image["image_id"], "version": newer["version"]},
        "instruction_artifact_id": result["artifact_id"]})
    assert app.comic_projects.get(pid).creative_brief == original_brief
    next_image = {"image_id": image["image_id"], "version": newer["version"]}
    report, _, vision_calls = analyze(app, pid, cid, next_image, monkeypatch,
                                      task="检查当前画面", request_id="next-analysis")
    context = json.loads(vision_calls[0][1]["content"][0]["text"])
    assert [item["constraint_id"] for item in context["inherited_constraint_replacements"]] == [
        "old-a", "old-b"]
    next_calls = []
    def next_model(messages):
        payload = json.loads(messages[-1]["content"])
        next_calls.append(payload)
        if "sections" not in payload:
            assert len(payload["inherited_constraint_replacements"]) == 2
            return json.dumps({"positive_prompt": "仅修改右侧边缘，保留" + new + "及 ARCHIVE 42",
                "negative_prompt": "不恢复旧主体，不改变其他内容", "summary": "离线第二轮"})
        assert payload["constraints"] == next_calls[-2]["constraints"]
        assert payload["constraints"][0]["enforcement"] == "scoped_replacement"
        assert new in payload["constraints"][0]["effective_requirement"]["requirement"]
        return json.dumps({"complete": True, "scope_valid": True, "findings": [{
            "constraint_id": item["constraint_id"], "status": "covered"
            if item["constraint_id"] == "keep-label" else "superseded",
            "evidence_path": "sections.positive_prompt"} for item in payload["constraints"]],
            "edit_findings": [{"constraint_id": "finding:selected", "status": "covered",
                "evidence_path": "sections.positive_prompt"}]})
    monkeypatch.setattr(app, "_comic_constraint_model", next_model)
    next_result = app.comic_image_operation(pid, "edit", {**common(app, pid, cid, "next-edit"),
        "image": next_image, "task": "检查当前画面", "analysis_artifact_id": report["artifact_id"],
        "selected_finding_ids": ["selected"], "edit_scope": {**scope, "regions": [{
            **scope["regions"][0], "finding_id": "selected", "instruction": ""}]},
        "round_id": "next-round"})
    assert len(next_calls) == 2
    assert next_result["constraint_receipt"]["review"]["constraint_replacements"] == []
    app.comic_image_operation(pid, "confirm", {**common(app, pid, cid, "confirm-next"),
        "instruction_artifact_id": next_result["artifact_id"]})
    third = upload(app, pid, cid, color=18, image_id=image["image_id"], expected_image_version=2,
                   parent=next_image, instruction_artifact_id=next_result["artifact_id"])
    basis, _ = app.comic_image_review._basis(pid, cid, ImageVersionRef(
        image_id=image["image_id"], version=third["version"]), "检查当前画面")
    assert len(basis["inherited_constraint_replacements"]) == 2
    source = app.comic_stories._source_contexts([app.comic_media.get(
        image["image_id"], version=third["version"]).model_dump(mode="json")])[0]
    assert source["source_kind"] == "confirmed_edit"
    protected = {item["constraint_id"]: item for item in source["constraints"]}
    for identity in ("old-a", "old-b"):
        assert protected[identity]["enforcement"] == "scoped_replacement"
        assert new in json.dumps(protected[identity]["effective_requirement"], ensure_ascii=False)
    assert protected["keep-label"]["enforcement"] == "preserve"
    branch = upload(app, pid, cid, color=19, image_id=image["image_id"], expected_image_version=3,
                    parent=image)
    original_basis, _ = app.comic_image_review._basis(pid, cid, ImageVersionRef(
        image_id=image["image_id"], version=branch["version"]), "检查当前画面")
    assert "inherited_constraint_replacements" not in original_basis
    assert app.comic_projects.get(pid).creative_brief == original_brief


def test_edit_replacement_cannot_release_assets_current_demands_or_unknown_sources():
    from kantoku.domains.comic.constraints import verify_prompt_constraints
    from kantoku.domains.comic.models import CreativeConstraint

    for case in ("asset", "current", "analysis", "quote", "unknown", "unrecorded", "late", "scope"):
        constraint = CreativeConstraint.model_validate({"constraint_id": "protected",
            "requirement": "固定身份", "target": "主体", "scope": "identity",
            "source_ref": {"id": "original", "kind": "message"},
            "source_quote": "固定身份", "origin": "asset_fact" if case == "asset"
            else "user_request", "trust": "source_verified"})
        requirement = {"requirement_id": "region:right", "requirement": "将框内主体换成飞鸟",
            "source": {"kind": "selected_analysis" if case == "analysis" else "manual_region"},
            "target": {"mode": "regions", "region_ids": ["right"]}}
        scope = {"edit_scope": {"mode": "regions", "regions": [{"region_id": "right",
                    "bounds": [0.5, 0.1, 0.9, 0.9]}]},
            "authorized_requirements": [requirement],
            "historical_constraint_ids": [] if case == "current" else ["protected"]}
        review = {"complete": True, "scope_valid": case != "scope", "findings": [{
            "constraint_id": "protected", "status": "superseded",
            "evidence_path": "sections.positive_prompt"}], "edit_findings": [{
            "constraint_id": "region:right", "status": "covered",
            "evidence_path": "sections.positive_prompt"}], "constraint_replacements": []
            if case == "unrecorded" else [{"constraint_id": "protected",
                "requirement_id": "unknown" if case == "unknown" else "region:right",
                "source_quote": "不在原文的伪造授权" if case == "quote"
                else requirement["requirement"],
                "reason": "离线尝试解除保护"}]}
        scope["constraint_resolution"] = {"constraint_replacements": review[
            "constraint_replacements"] if case == "scope" else []}
        with pytest.raises(ToolError) as raised:
            verify_prompt_constraints([constraint],
                sections={"positive_prompt": "只在框内更换飞鸟"},
                negative_prompt="", edit_scope=scope,
                model_call=lambda _, review=review: json.dumps(review))
        if case != "scope":
            assert raised.value.review_issues[0]["code"] == "replacement_invalid"


def test_cross_instance_claim_prevents_concurrent_paid_submission(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app)
    request = {**common(app, pid, cid, "claim-once"), "image": image, "task": "检查主体"}
    entered, release, calls = threading.Event(), threading.Event(), []
    def model(_messages):
        calls.append(True)
        entered.set()
        assert release.wait(10)
        return json.dumps(observation())
    monkeypatch.setattr(app, "_comic_image_vision_model", model)
    second = web_studio.StudioApplication()
    monkeypatch.setattr(second, "_comic_image_vision_model", lambda _: pytest.fail("重复付费"))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(app.comic_image_operation, pid, "analysis", request)
            try:
                assert entered.wait(10)
                with pytest.raises(ToolError, match="不会自动重发"):
                    second.comic_image_operation(pid, "analysis", request)
            finally:
                release.set()
            result = future.result()
        assert len(calls) == 1
        assert second.comic_image_operation(pid, "analysis", request) == result
    finally:
        second.runner.close()


def test_paid_acceptance_rejects_missing_ledger_and_removes_its_session(monkeypatch, tmp_path):
    import runpy
    import sys

    from kantoku.config.runtime_paths import get_test_runtime_paths
    from kantoku.config.settings import ROOT

    path = tmp_path / "qa.png"
    path.write_bytes(png())
    manager = get_test_runtime_paths()
    before = set((manager.tmp / "sessions").glob("kantoku-paid-acceptance-*"))
    monkeypatch.setattr(sys, "argv", ["verify", "--allow-paid", "--suite", "image-review",
        "--budget-id", "existing-multi-module-budget", "--image", str(path),
        "--task", "检查画面", "--edit-request", "只调整边缘"])
    script = runpy.run_path(str(ROOT / "scripts" / "verify_director_pipeline.py"))
    with pytest.raises(SystemExit) as failure:
        script["run"]()
    assert failure.value.code == 2
    assert set((manager.tmp / "sessions").glob("kantoku-paid-acceptance-*")) == before

def test_workbench_drafts_are_versioned_without_changing_project(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app)
    report, _, _ = analyze(app, pid, cid, image, monkeypatch)
    original_version = app.comic_projects.get(pid).project.current_version
    state = {"conversation_id": cid, "request_id": "workspace", "expected_version": 0,
             "image": image, "auto_analyze": False}
    saved = app.save_comic_workbench(pid, state)
    assert saved["version"] == 1
    assert app.save_comic_workbench(pid, state) == saved
    draft = {"conversation_id": cid, "request_id": "draft", "expected_version": 0,
             "image": image, "round_id": "round-1", "task": "检查主体",
             "analysis_artifact_id": report["artifact_id"], "selected_finding_ids": ["selected"],
             "instruction": "仅修改框内", "edit_scope": {"mode": "regions", "regions": [
                 {"region_id": "adjusted", "label": "区域1", "bounds": [0.2, 0.3, 0.6, 0.7],
                  "finding_id": "selected", "instruction": ""}]}}
    stored = app.save_comic_review_draft(image["image_id"], draft)
    assert app.save_comic_review_draft(image["image_id"], draft) == stored
    assert app.comic_review_draft(image["image_id"], 1, cid)["state"]["edit_scope"] == draft[
        "edit_scope"]
    assert app.comic_projects.get(pid).project.current_version == original_version
    with pytest.raises(ToolError, match="其他页面"):
        app.save_comic_review_draft(image["image_id"], {**draft, "request_id": "other"})
    other = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]
    with pytest.raises(ToolError):
        app.comic_review_draft(image["image_id"], 1, other)


def test_manual_scope_reaches_compiler_and_excludes_unselected_candidates(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app)
    report, _, _ = analyze(app, pid, cid, image, monkeypatch)
    seen = []
    def model(messages):
        payload = json.loads(messages[-1]["content"])
        seen.append(payload)
        if isinstance(payload, list):
            return json.dumps({"constraints": [{"source_index": index,
                "source_quote": item["quote"], "target": "选中区域", "scope": "output"}
                for index, item in enumerate(payload)]}, ensure_ascii=False)
        if payload.get("stage") == "constraint_extraction_review":
            return json.dumps({"complete": True, "scope_valid": True,
                "constraint_replacements": [], "findings": [
                {"constraint_id": item["constraint_id"], "status": "covered",
                 "evidence_path": f"constraints.{item['constraint_id']}.source_quote"}
                for item in payload["constraints"]]})
        if "sections" in payload:
            return json.dumps({"complete": True, "scope_valid": True, "findings": [
                {"constraint_id": item["constraint_id"], "status": "covered",
                 "evidence_path": "sections.positive_prompt"}
                for item in payload["constraints"]],
                "edit_findings": [{"constraint_id": item["requirement_id"], "status": "covered",
                    "evidence_path": "sections.positive_prompt"}
                    for item in payload["authorized_requirements"]]})
        return json.dumps({"positive_prompt": "修改明确框选区域，保留其余内容。",
                           "negative_prompt": "", "summary": "离线区域指令"})

    monkeypatch.setattr(app, "_comic_constraint_model", model)
    scope = {"mode": "regions", "regions": [
        {"region_id": "user-box", "label": "手动1", "bounds": [0.25, 0.2, 0.75, 0.6],
         "finding_id": None, "instruction": "调整框内边缘"}]}
    request = {**common(app, pid, cid, "manual-edit"), "image": image, "task": "检查主体",
               "round_id": "manual-round", "edit_scope": scope, "instruction": "只改此区域"}
    manual = app.comic_image_operation(pid, "edit", request)
    assert manual["analysis_artifact_id"] is None
    assert manual["scope"]["protected_features"] == []
    compiled = next(item for item in seen if isinstance(item, dict) and "edit_scope" in item)
    assert compiled["edit_scope"] == scope
    assert compiled["selected_findings"] == []
    assert len(manual["edit_constraints"]) == 2
    selected_scope = {"mode": "regions", "regions": [
        {**scope["regions"][0], "finding_id": "selected"}]}
    request.update(common(app, pid, cid, "selected-edit"))
    selected = app.comic_image_operation(pid, "edit", {**request,
        "analysis_artifact_id": report["artifact_id"], "selected_finding_ids": ["selected"],
        "edit_scope": selected_scope})
    assert [item["finding_id"] for item in selected["scope"]["selected_findings"]] == ["selected"]
    assert "unselected" not in json.dumps(selected["scope"])
    with pytest.raises(ToolError, match="关联"):
        app.comic_image_operation(pid, "edit", {**request,
            **common(app, pid, cid, "invalid-region-link"), "edit_scope": selected_scope})


def test_pending_draft_and_mixed_scope_preserve_explicit_targets(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app)
    report, _, _ = analyze(app, pid, cid, image, monkeypatch)
    local = {"region_id": "chosen", "label": "区域1", "finding_id": "selected",
             "bounds": [0.2, 0.2, 0.7, 0.8], "instruction": ""}
    pending = {"mode": "regions", "regions": [local], "whole_finding_ids": [],
               "instruction_target": {"mode": "selected_regions"}}
    draft = {"conversation_id": cid, "request_id": "pending-draft", "expected_version": 0,
             "image": image, "round_id": "mixed-round", "task": "检查主体",
             "analysis_artifact_id": report["artifact_id"],
             "selected_finding_ids": ["selected", "unselected"], "edit_scope": pending}
    assert app.save_comic_review_draft(image["image_id"], draft)["state"]["edit_scope"] == pending
    with pytest.raises(ToolError, match="尚未定位"):
        app.comic_image_operation(pid, "edit", {**common(app, pid, cid, "pending-edit"),
            **{key: value for key, value in draft.items() if key not in {
                "request_id", "expected_version", "conversation_id"}}})
    mixed = {**pending, "mode": "mixed", "whole_finding_ids": ["unselected"],
             "instruction_target": {"mode": "whole"}}
    compiled, request, seen = edit(app, pid, cid, image, report, monkeypatch,
        request_fields={"selected_finding_ids": ["selected", "unselected"],
                        "edit_scope": mixed, "instruction": "柔化整图光线",
                        "round_id": "mixed-round"})
    assert compiled["scope"]["edit_scope"] == mixed
    assert compiled["scope"]["user_instruction"] == "柔化整图光线"
    assert any(isinstance(item, dict) and item.get("edit_scope") == mixed for item in seen)
    count = len(seen)
    assert app.comic_image_operation(pid, "edit", request) == compiled
    assert len(seen) == count


@pytest.mark.parametrize("change", ["unknown", "duplicate", "overlap", "deleted_target"])
def test_invalid_mixed_authorization_never_calls_model(review_app, monkeypatch, change):
    app = review_app
    pid, cid, image = setup_image(app)
    report, _, _ = analyze(app, pid, cid, image, monkeypatch)
    scope = {"mode": "mixed", "regions": [{"region_id": "chosen", "bounds": [0.1, 0.1, 0.8, 0.8],
              "finding_id": "selected"}], "whole_finding_ids": ["unselected"],
             "instruction_target": {"mode": "whole"}}
    if change == "unknown":
        scope["whole_finding_ids"] = ["another-report"]
    elif change == "duplicate":
        scope["whole_finding_ids"] = ["unselected", "unselected"]
    elif change == "overlap":
        scope["whole_finding_ids"] = ["selected", "unselected"]
    else:
        scope["instruction_target"] = {"mode": "region", "region_id": "removed"}
        draft = {"conversation_id": cid, "request_id": "missing-target-draft",
            "expected_version": 0,
            "image": image, "round_id": "invalid-round", "task": "检查主体",
            "analysis_artifact_id": report["artifact_id"],
            "selected_finding_ids": ["selected", "unselected"],
            "edit_scope": scope, "instruction": "调整边缘"}
        assert app.save_comic_review_draft(image["image_id"], draft)["version"] == 1
    with pytest.raises((ToolError, ValidationError)):
        app.comic_image_operation(pid, "edit", {**common(app, pid, cid, "invalid-scope"),
            "image": image, "task": "检查主体", "analysis_artifact_id": report["artifact_id"],
            "selected_finding_ids": ["selected", "unselected"], "edit_scope": scope,
            "instruction": "调整边缘"})


def test_legacy_scope_confirmation_and_request_fingerprint_are_preserved(review_app, monkeypatch):
    app = review_app
    pid, cid, image = setup_image(app)
    report, _, _ = analyze(app, pid, cid, image, monkeypatch)
    scope = {"mode": "regions", "regions": [{"region_id": "legacy", "label": "区域1",
        "bounds": [0.1, 0.1, 0.8, 0.8], "finding_id": "selected", "instruction": ""}]}
    compiled, request, seen = edit(app, pid, cid, image, report, monkeypatch,
        request_fields={"edit_scope": scope, "round_id": "legacy-round"})
    artifact = app.runtime_store.get_artifact(compiled["artifact_id"])
    frozen_metadata = json.dumps(artifact.metadata, sort_keys=True)
    assert compiled["scope"]["edit_scope"] == scope
    app.comic_image_operation(pid, "confirm", {**common(app, pid, cid, "confirm-legacy"),
        "instruction_artifact_id": compiled["artifact_id"]})
    draft = {"conversation_id": cid, "request_id": "legacy-draft", "expected_version": 0,
        "image": image, "round_id": "legacy-round", "task": "检查主体",
        "analysis_artifact_id": report["artifact_id"], "selected_finding_ids": ["selected"],
        "edit_scope": scope, "instruction_artifact_id": compiled["artifact_id"], "phase": "review"}
    saved = app.save_comic_review_draft(image["image_id"], draft)
    assert saved["state"]["edit_scope"] == scope
    assert "dismissed_finding_region_ids" not in saved["state"]
    assert app.save_comic_review_draft(image["image_id"], draft) == saved
    count = len(seen)
    replay = app.comic_image_operation(pid, "edit", request)
    assert replay["artifact_id"] == compiled["artifact_id"] and len(seen) == count
    assert json.dumps(app.runtime_store.get_artifact(artifact.id).metadata,
                      sort_keys=True) == frozen_metadata


def test_missing_file_restore_and_many_versions_use_real_files(review_app):
    app = review_app
    pid, cid, source = setup_image(app)
    image = app.comic_media.get(source["image_id"])
    path = Path(app.runtime_store.get_artifact(image.artifact_id).location)
    content = path.read_bytes()
    from kantoku.config.runtime_paths import get_runtime_paths
    get_runtime_paths().remove_file(path, file_type="artifact")
    restore = {**common(app, pid, cid, "restore-original"), "image": source,
               "filename": "original.png",
               "data_url": "data:image/png;base64," + base64.b64encode(content).decode()}
    wrong = {**restore, "request_id": "different-file",
             "data_url": "data:image/png;base64," + base64.b64encode(png(200)).decode()}
    with pytest.raises(ToolError, match="内容不同"):
        app.restore_comic_image_file(source["image_id"], wrong)
    assert not path.exists()
    result = app.restore_comic_image_file(source["image_id"], restore)
    assert result["restored"] and path.read_bytes() == content
    from kantoku.config.runtime_paths import atomic_write_bytes
    with pytest.raises(FileExistsError):
        atomic_write_bytes(path, png(99), replace=False)
    assert path.read_bytes() == content
    assert app.restore_comic_image_file(source["image_id"], restore) == result
    for number in range(2, 9):
        upload(app, pid, cid, color=number, image_id=source["image_id"],
               expected_image_version=number - 1, parent=source)
    page = app.comic_image_versions(source["image_id"], cid, limit=3)
    assert [item["version"] for item in page["versions"]] == [8, 7, 6]
    older = app.comic_image_versions(source["image_id"], cid, before=page["next_before"], limit=3)
    assert [item["version"] for item in older["versions"]] == [5, 4, 3]
    assert all(item["parent"] == source for item in page["versions"])
    assert path.read_bytes() == content
    assert app.comic_media.list(pid)[0]["selection"] is None



def _legacy_family(app, pid, cid, *, color=0):
    """旧分镜上传：图片以 Artifact ID 充当 image_id，v1 只是它的只读投影。"""
    imported = upload(app, pid, cid, color=color, request_id=f"legacy-upload-{color}")
    original = app.comic_media.get(imported["image_id"])
    artifact = app.runtime_store.get_artifact(original.artifact_id).model_copy(update={
        "id": f"artifact-legacy-{color}", "source": "comic.external_image",
        "metadata": {"origin": "real", "creation_mode": "external_upload", "project_id": pid,
                     "shot_id": f"legacy-shot-{color}", "prompt_artifact_id": "legacy-prompt",
                     "sha256": original.sha256, "filename": "original.png"}})
    app.runtime_store.save_artifact(artifact)
    return artifact, {"image_id": artifact.id, "version": 1}


@pytest.mark.parametrize("lifecycle", ["trashed", "purged"])
def test_retired_legacy_family_can_be_removed_without_reactivating_its_file(review_app, lifecycle):
    app = review_app
    pid, cid, kept = setup_image(app)
    legacy, ref = _legacy_family(app, pid, cid, color=41)
    app.select_comic_image(legacy.id, {
        **common(app, pid, cid, "adopt-retired"), "version": 1, "expected_selection_version": 0})
    trashed = app.change_core_artifact(legacy.id, "trash", {"expected_lifecycle_version": 1})
    if lifecycle == "purged":
        app.change_core_artifact(legacy.id, "purge", {
            "expected_lifecycle_version": trashed["lifecycle_version"]})
    family = next(item for item in app.list_comic_images(pid, cid)["images"]
                  if item["image_id"] == legacy.id)
    assert family["available"] is False and family["selection"]["available"] is False
    removed = app.delete_comic_image(pid, legacy.id, common(app, pid, cid, "remove-retired"))
    assert removed["image_id"] == legacy.id
    assert not app.comic_stories.conversation_candidates(cid)["images"]
    assert app.runtime_store.get_artifact(legacy.id).lifecycle_state.value == lifecycle
    assert legacy.id not in {item["image_id"] for item in app.comic_media.list(pid)}
    assert app.comic_media.require_image(ImageVersionRef.model_validate(kept), pid, cid)
    with pytest.raises(ToolError, match="找不到指定图片版本"):
        app.comic_media.get(ref["image_id"], version=1, _legacy_history=True)


def test_reports_are_scoped_to_content_not_to_a_reused_version_number(review_app, monkeypatch):
    """版本号可以被复用（删除后重新导入），报告归属必须看内容指纹。"""
    app = review_app
    pid, cid, _imported = setup_image(app)
    legacy, image_v1 = _legacy_family(app, pid, cid, color=0)
    report, _, _ = analyze(app, pid, cid, image_v1, monkeypatch, request_id="analyze-legacy")
    # 同一图片再追加一版不同内容：v2 的 sha 与 v1 不同。
    upload(app, pid, cid, color=7, image_id=legacy.id, expected_image_version=1,
           parent=image_v1, request_id="legacy-v2")

    # 把 v1 的报告错误地挂到 v2 上（旧实现只看 image_id+version，会把它当成 v2 的分析）。
    artifact = app.runtime_store.get_artifact(report["artifact_id"])
    metadata = dict(artifact.metadata)
    payload = json.loads(json.dumps(metadata["payload"]))
    payload["basis"]["source_image"]["version"] = 2
    metadata.update(payload=payload, sha256=value_hash(payload))
    app.runtime_store.save_artifact(artifact.model_copy(
        update={"id": "artifact-stale-version", "metadata": metadata}))

    assert app.comic_image_reports(pid, cid, "analysis", image=ImageVersionRef(
        image_id=legacy.id, version=2))["items"] == []
    # 原图（v1）自己的报告仍然可取。
    assert [item["artifact_id"] for item in app.comic_image_reports(
        pid, cid, "analysis", image=ImageVersionRef.model_validate(image_v1))["items"]] == [
            report["artifact_id"]]


def test_deleting_a_legacy_image_trashes_its_artifact_so_it_cannot_reappear(review_app):
    """不回收旧产物，删掉的图片下次读取会复活成 v1，新导入的图会被当成它的 v2。"""
    app = review_app
    pid, cid, _imported = setup_image(app)
    legacy, image = _legacy_family(app, pid, cid, color=3)
    assert legacy.id in [item["image_id"] for item in app.comic_media.list(pid)]

    app.delete_comic_image(pid, legacy.id, common(app, pid, cid, "delete-legacy"))

    assert legacy.id not in [item["image_id"] for item in app.comic_media.list(pid)]
    assert app.runtime_store.get_artifact(
        legacy.id).lifecycle_state == ArtifactLifecycle.TRASHED
    with pytest.raises(ToolError, match="找不到指定图片版本"):
        app.comic_media.get(legacy.id)
    # 追加版本的旧路径必须失败，否则新图会被写进已删除的图片家族。
    with pytest.raises(ToolError, match="找不到指定图片版本"):
        upload(app, pid, cid, color=9, image_id=legacy.id, expected_image_version=1,
               parent=image)


@pytest.mark.parametrize("lifecycle", ["trashed", "purged"])
@pytest.mark.parametrize("retire_revision", [False, True])
def test_retired_legacy_source_keeps_work_list_and_version_history_readable(
    review_app, lifecycle, retire_revision,
):
    """全局回收/清除旧原图不应让整份作品列表崩溃，也不能复活已删除文件。"""
    app = review_app
    parent = app.comic_workspace.create({"request_id": "source-work", "title": "Source work"})
    child = app.comic_workspace.create_task(parent["work_id"], {
        "request_id": "source-task", "expected_version": parent["version"],
        "title": "Existing image", "requirement": "Keep this image's original provenance."})
    pid, cid = child["task_id"], child["conversation_id"]
    legacy, first = _legacy_family(app, pid, cid, color=21)
    revised = upload(app, pid, cid, color=22, image_id=legacy.id, expected_image_version=1,
                     parent=first, request_id="legacy-second-version")
    original_brief = app.comic_media.image_brief_version(legacy.id)
    other = app.comic_workspace.create({"request_id": "other-work", "title": "Unaffected work"})
    before = [item.model_dump(mode="json") for item in app.comic_media.versions(legacy.id)]

    for artifact_id in [legacy.id, *([revised["artifact_id"]] if retire_revision else [])]:
        artifact = app.runtime_store.get_artifact(artifact_id)
        removed = app.change_core_artifact(artifact_id, "trash", {
            "expected_lifecycle_version": artifact.lifecycle_version})
        if lifecycle == "purged":
            app.change_core_artifact(artifact_id, "purge", {
                "expected_lifecycle_version": removed["lifecycle_version"]})

    # 正常图片入口仍拒绝停用的旧图；列目录和读取历史不要求旧文件仍可用。
    with pytest.raises(ToolError, match="找不到指定图片版本"):
        app.comic_media.get(legacy.id, version=1)
    works = app.comic_workspace.list()["works"]
    assert {item["work_id"] for item in works} == {parent["work_id"], other["work_id"]}
    assert next(item for item in works if item["work_id"] == parent["work_id"])["tasks"]
    listed = app.list_comic_images(pid, cid)["images"]
    image = next(item for item in listed if item["image_id"] == legacy.id)
    assert image["version"] == 2 and image["label"] == "original.png"
    assert image["source_brief_version"] == original_brief == 1
    history = app.comic_image_versions(legacy.id, cid)["versions"]
    assert [{key: item[key] for key in before[0]} for item in history] == before
    assert [item["available"] for item in history] == [not retire_revision, False]
    assert history[-1]["lifecycle_state"] == lifecycle
    assert app.runtime_store.get_artifact(legacy.id).lifecycle_state.value == lifecycle
    with pytest.raises(ToolError):
        app.comic_media.require_image(ImageVersionRef.model_validate(first), pid, cid)
    if retire_revision:
        with pytest.raises(ToolError, match="图片产物已失效"):
            app.comic_media.require_image(ImageVersionRef.model_validate({
                "image_id": legacy.id, "version": 2}), pid, cid)
    else:
        current = app.comic_media.require_image(ImageVersionRef.model_validate({
            "image_id": legacy.id, "version": 2}), pid, cid)
        assert Path(app.runtime_store.get_artifact(current.artifact_id).location).is_file()

    # 用真实 HTTP 验证原报错入口；GET 不改故事、图片版本或生命周期。
    server = web_studio.make_server(app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request("GET", "/api/comic/works", headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["works"] == works
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert [item.model_dump(mode="json") for item in app.comic_media.versions(legacy.id)] == before

    # 用户明确移除整个版本链后，连历史投影也不能把旧图重新带回来。
    app.delete_comic_image(pid, legacy.id, common(app, pid, cid, "remove-retired-family"))
    assert legacy.id not in [item["image_id"] for item in app.comic_media.list(pid)]
    with pytest.raises(ToolError, match="找不到指定图片版本"):
        app.comic_media.get(legacy.id, version=1, _legacy_history=True)


@pytest.mark.parametrize("lifecycle", ["trashed", "purged"])
@pytest.mark.parametrize("retire_revision", [False, True])
def test_global_retirement_of_adopted_legacy_version_does_not_break_story_candidates(
    review_app, lifecycle, retire_revision,
):
    """采用的是停用 v1；有效 v2 与其他图片都不能掩盖或放大这个失效引用。"""
    app = review_app
    parent = app.comic_workspace.create({"request_id": "candidate-work", "title": "Candidate work"})
    child = app.comic_workspace.create_task(parent["work_id"], {
        "request_id": "candidate-task", "expected_version": parent["version"],
        "title": "Source frames",
        "requirement": "Only explicitly adopted frames enter this story."})
    pid, cid = child["task_id"], child["conversation_id"]
    legacy, first = _legacy_family(app, pid, cid, color=61)
    revised = upload(app, pid, cid, color=62, image_id=legacy.id, expected_image_version=1,
                     parent=first, request_id="candidate-revision")
    kept = upload(app, pid, cid, color=63, request_id="candidate-kept")
    for ref in (first, {"image_id": kept["image_id"], "version": 1}):
        app.select_comic_image(ref["image_id"], {
            **common(app, pid, cid, "adopt-" + ref["image_id"]),
            "version": ref["version"], "expected_selection_version": 0})
    original_versions = [item.model_dump(mode="json")
                         for item in app.comic_media.versions(legacy.id)]
    selection = app.comic_image_versions(legacy.id, cid)["selection"]
    workbench = {"conversation_id": cid, "request_id": "candidate-preference",
                 "expected_version": 0, "image": {"image_id": legacy.id, "version": 2},
                 "edit_image": first, "auto_analyze": False}
    saved_workbench = app.save_comic_workbench(pid, workbench)
    draft = {"conversation_id": cid, "request_id": "candidate-draft", "expected_version": 0,
             "image": first, "round_id": "retained-local-round", "task": "Retain this draft.",
             "selected_finding_ids": [], "instruction": "Keep this saved local requirement."}
    saved_draft = app.save_comic_review_draft(legacy.id, draft)

    retired = [legacy.id, *([revised["artifact_id"]] if retire_revision else [])]
    for artifact_id in retired:
        artifact = app.runtime_store.get_artifact(artifact_id)
        trashed = app.change_core_artifact(artifact_id, "trash", {
            "expected_lifecycle_version": artifact.lifecycle_version})
        if lifecycle == "purged":
            app.change_core_artifact(artifact_id, "purge", {
                "expected_lifecycle_version": trashed["lifecycle_version"]})

    candidates = app.comic_work_story(parent["work_id"], "images")["images"]
    by_image = {item["image_id"]: item for item in candidates}
    assert set(by_image) == {legacy.id, kept["image_id"]}
    assert by_image[legacy.id]["version"] == 1
    assert by_image[legacy.id]["selection"]["selected_version"] == 1
    assert by_image[legacy.id]["available"] is False
    assert by_image[legacy.id]["unavailable_reason"]
    assert by_image[legacy.id]["lifecycle_state"] == lifecycle
    assert by_image[kept["image_id"]]["available"] is True
    assert by_image[kept["image_id"]]["project_id"] == pid
    with pytest.raises(ToolError):
        app.comic_media.require_image(ImageVersionRef.model_validate(first), pid, cid)
    assert app.comic_media.require_image(ImageVersionRef(
        image_id=kept["image_id"], version=1), pid, cid).artifact_id == kept["artifact_id"]

    # 页面实际请求的作品级候选接口必须成功，不能因一个悬空采用指针整组报错。
    server = web_studio.make_server(app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request("GET", f"/api/comic/works/{parent['work_id']}/story/images",
                           headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200, payload
        assert {item["image_id"] for item in payload["images"]} == set(by_image)
        assert next(item for item in payload["images"]
                    if item["image_id"] == legacy.id)["available"] is False
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert [item.model_dump(mode="json") for item in app.comic_media.versions(legacy.id)] == \
        original_versions
    current_selection = app.comic_image_versions(legacy.id, cid)["selection"]
    assert current_selection["selected_version"] == selection["selected_version"] == 1
    assert current_selection["selection_version"] == selection["selection_version"]
    assert app.comic_workbench_state(pid, cid)["state"] == saved_workbench["state"]

    if lifecycle == "trashed":
        for artifact_id in retired:
            artifact = app.runtime_store.get_artifact(artifact_id)
            app.change_core_artifact(artifact_id, "restore", {
                "expected_lifecycle_version": artifact.lifecycle_version})
        restored = app.comic_work_story(parent["work_id"], "images")["images"]
        original = next(item for item in restored if item["image_id"] == legacy.id)
        assert original["version"] == 1 and original["available"] is True
        assert original["selection"]["selected_version"] == 1
        assert app.comic_review_draft(legacy.id, 1, cid)["state"] == saved_draft["state"]
        assert app.comic_media.require_image(ImageVersionRef.model_validate(first), pid, cid)
    else:
        artifact = app.runtime_store.get_artifact(legacy.id)
        with pytest.raises(ToolError):
            app.change_core_artifact(legacy.id, "restore", {
                "expected_lifecycle_version": artifact.lifecycle_version})
