"""Story revisions and exported media contracts; every model is an offline substitute."""

import base64
import copy
import json
import struct
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from kantoku.config import ToolError, get_settings
from kantoku.config.runtime_paths import atomic_write_bytes
from kantoku.core import llm
from kantoku.core.runtime.models import ArtifactLifecycle
from kantoku.domains.comic.models import ComicStoryDraft
from kantoku.shells import web_studio
from kantoku.tools.image_gen import LocalFakeImageProvider


def _png(color=0):
    def chunk(name, content):
        return (struct.pack(">I", len(content)) + name + content
                + struct.pack(">I", zlib.crc32(name + content)))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, color, 0, 0])))
            + chunk(b"IEND", b""))


def _forbidden(*_args, **_kwargs):
    pytest.fail("Story offline tests must never call an unmocked paid model")


@pytest.fixture
def story_app(monkeypatch, tmp_path):
    """Use the application's real domain stores and one managed, isolated database."""
    from kantoku.core.runtime import storage

    settings = get_settings().model_copy(deep=True)
    settings.storage.sqlite_path = tmp_path / "stories.db"
    settings.image.output_dir = tmp_path / "images"
    monkeypatch.setattr(web_studio, "get_settings", lambda: settings)
    monkeypatch.setattr(storage, "get_settings", lambda: settings)
    monkeypatch.setattr(web_studio, "_provider", lambda: LocalFakeImageProvider(
        tmp_path / "fake", model_id="offline-story-test", actual_fen=0))
    for module in (llm, web_studio):
        monkeypatch.setattr(module, "chat", _forbidden)
        monkeypatch.setattr(module, "vision_chat", _forbidden)
    application = web_studio.StudioApplication()
    monkeypatch.setattr(application, "_comic_image_vision_model", _forbidden)
    monkeypatch.setattr(application, "_comic_constraint_model", _forbidden)
    try:
        yield application
    finally:
        application.runner.close()


def _conversation(app):
    return app.create_conversation({"interaction_mode": "guided", "domain": "comic"})["id"]


def _project(app, instruction="Independent offline image"):
    return app.create_comic_project({"title": "Offline source", "brief": {
        "original_request": instruction}})["project"]["project_id"]


def _media_request(app, pid, cid, key):
    return {"conversation_id": cid, "request_id": key,
            "expected_project_version": app.comic_projects.get(pid).project.current_version}


def _upload(app, pid, cid, *, content=None, mime="image/png", filename="source.png", **fields):
    content = _png() if content is None else content
    return app.import_comic_image(pid, {
        **_media_request(app, pid, cid, f"import-{uuid4().hex}"), "filename": filename,
        "data_url": f"data:{mime};base64," + base64.b64encode(content).decode(), **fields})


def _ref(image):
    return {"image_id": image["image_id"], "version": image["version"]}


def _select(app, cid, image, *, expected=0):
    return app.select_comic_image(image["image_id"], {
        **_media_request(app, image["project_id"], cid, f"select-{uuid4().hex}"),
        "version": image["version"], "expected_selection_version": expected})


def _setup(app):
    cid = _conversation(app)
    pid = _project(app)
    image = _upload(app, pid, cid)
    _select(app, cid, image)
    return pid, cid, image


def _request(app, pid, cid, key, *, version=None):
    if version is None:
        story = app.get_comic_story(cid)["story"]
        version = story["version"] if story else 0
    return {**_media_request(app, pid, cid, key), "anchor_project_id": pid,
            "expected_story_version": version}


def _draft(*images, title="Offline bridge sequence"):
    return {"title": title, "summary": "An editable sequence based on explicitly selected images.",
            "instruction": "Preserve the supplied image identities.", "shots": [
                {"shot_id": f"story-shot-{index}", "image": _ref(image),
                 "action": f"A small visible movement in shot {index}.",
                 "camera": "Camera remains still.", "duration_seconds": 4.0,
                 "transition": "Cut."} for index, image in enumerate(images, 1)]}


def _save(app, pid, cid, draft, key="save"):
    return app.comic_story_operation(cid, "save", {
        **_request(app, pid, cid, key), "draft": draft})


def _story_draft(story):
    return {key: story[key] for key in ("title", "summary", "instruction", "shots")}


def _review(app, pid, cid, monkeypatch, *, output=None, key="review"):
    seen = []

    def model(messages):
        seen.append(messages)
        return json.dumps(output or {"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_constraint_model", model)
    result = app.comic_story_operation(cid, "review", _request(app, pid, cid, key))
    return result, seen


def _confirm(app, pid, cid, monkeypatch, *, key="confirm"):
    _review(app, pid, cid, monkeypatch, key=key + "-review")
    return app.comic_story_operation(cid, "confirm", _request(app, pid, cid, key))


def test_fixed_versions_across_projects_survive_reselection_and_reload(story_app):
    app = story_app
    pid, cid, first = _setup(app)
    newer = _upload(app, pid, cid, content=_png(31), image_id=first["image_id"],
                    expected_image_version=1, parent=_ref(first))
    related_pid = _project(app, "A separate mechanical subject")
    second = _upload(app, related_pid, cid, content=_png(52))
    _select(app, cid, second)
    candidates = app.comic_story_images(cid)["images"]
    assert {_ref(item)["image_id"]: item["version"] for item in candidates} == {
        first["image_id"]: 1, second["image_id"]: 1}
    saved = _save(app, pid, cid, _draft(second, first, second))
    assert saved["story"]["shots"][1]["image"] == _ref(first)
    assert len(saved["story"]["fixed_images"]) == 2
    _select(app, cid, newer, expected=1)
    restored = app.get_comic_story(cid)
    assert restored["story"] == saved["story"]
    assert restored["story"]["shots"][0]["image"] == _ref(second)
    # A new service instance must rebuild the story from the original database.
    from kantoku.domains.comic.stories import ComicStoryService
    reloaded = ComicStoryService(app.comic_media).view(cid)
    assert reloaded["story"] == saved["story"]
    edited = _save(app, pid, cid, _draft(second, first, second, title="Reuse fixed old version"),
                   "reuse-old-version")
    assert edited["story"]["shots"][1]["image"] == _ref(first)


def test_cross_conversation_and_unadopted_images_cannot_enter_story(story_app):
    app = story_app
    pid, cid, first = _setup(app)
    _save(app, pid, cid, _draft(first))
    original = app.get_comic_story(cid)["story"]
    other_cid = _conversation(app)
    other_pid = _project(app)
    foreign = _upload(app, other_pid, other_cid)
    _select(app, other_cid, foreign)
    unadopted = _upload(app, pid, cid, content=_png(11))
    for index, image in enumerate((foreign, unadopted)):
        with pytest.raises(ToolError):
            _save(app, pid, cid, _draft(image), f"invalid-scope-{index}")
        assert app.get_comic_story(cid)["story"] == original


@pytest.mark.parametrize("modern", [False, True])
def test_story_uses_frozen_artifact_when_a_historical_image_version_is_reused(
    story_app, monkeypatch, modern,
):
    app = story_app
    if modern:
        work = app.comic_workspace.create({"request_id": uuid4().hex, "title": "Snapshot story"})
        task = app.comic_workspace.create_task(work["work_id"], {
            "request_id": uuid4().hex, "expected_version": work["version"],
            "title": "Original image", "requirement": "Original image"})
        pid, cid = task["task_id"], task["conversation_id"]
        image = _upload(app, pid, cid)
        _select(app, cid, image)
    else:
        pid, cid, image = _setup(app)
    original = _upload(app, pid, cid, content=_png(31), image_id=image["image_id"],
                       expected_image_version=1, parent=_ref(image))
    _select(app, cid, original, expected=1)

    def read():
        return app.comic_work_story(work["work_id"]) if modern else app.get_comic_story(cid)

    def mutate(kind, **fields):
        if modern:
            return app.comic_work_story(work["work_id"], kind, {
                "request_id": uuid4().hex,
                "expected_project_version": app.comic_workspace.get(work["work_id"])["version"],
                "expected_story_version": read()["story_version"], **fields})
        return app.comic_story_operation(cid, kind, {
            **_request(app, pid, cid, uuid4().hex), **fields})

    saved = mutate("save", draft=_draft(original))
    replacement = _upload(app, pid, cid, content=_png(81))
    # 仅在隔离测试库回放历史损坏：相同 image_id/version 后来指向了另一份内容。
    reused = {**app.comic_media.get(replacement["image_id"]).model_dump(mode="json"),
              "image_id": original["image_id"], "version": 2}
    with app.comic_projects._connect() as connection:
        connection.execute(
            "UPDATE comic_entity_versions SET payload_json=? "
            "WHERE project_id=? AND entity_type='image' AND entity_id=? AND version=2",
            (json.dumps(reused), pid, original["image_id"]),
        )
    assert app.comic_media.get(original["image_id"], version=2).sha256 == replacement["sha256"]
    assert read()["story"] == saved["story"] and not read()["missing_images"]
    assert mutate("save", draft=_draft(original))["story"] == saved["story"]
    pixels = []

    def vision(messages):
        pixels.extend(base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                      for part in messages[-1]["content"] if part["type"] == "image_url")
        return json.dumps(_draft(original))

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model",
                        lambda _: json.dumps({"status": "pass", "findings": []}))
    generated = mutate("generate", images=[_ref(original)], instruction="One original shot.")
    assert generated["story"]["fixed_images"] == saved["story"]["fixed_images"]
    assert pixels == [_png(31)]
    mutate("review")
    mutate("confirm")
    exported = mutate("exports")
    artifact = app.runtime_store.get_artifact(exported["export_artifact_id"])
    with ZipFile(artifact.location) as bundle:
        filename = exported["manifest"]["images"][0]["filename"]
        assert bundle.read(filename) == _png(31)
    # 快照也必须实际验哈希，不能因为采用新读取方式而放行损坏的原图。
    old_artifact = app.runtime_store.get_artifact(original["artifact_id"])
    atomic_write_bytes(Path(old_artifact.location), _png(200), owner="offline.story.tamper")
    assert read()["missing_images"]
    with pytest.raises(ToolError):
        mutate("exports")


@pytest.mark.parametrize("duration", ["4s", float("inf"), float("nan")])
def test_duration_and_duplicate_identity_are_checked_as_structural_contract(duration):
    draft = _draft({"image_id": "offline-image", "version": 1})
    draft["shots"][0]["duration_seconds"] = duration
    with pytest.raises(ValidationError):
        ComicStoryDraft.model_validate(draft)
    draft["shots"][0]["duration_seconds"] = 4.0
    draft["shots"].append(copy.deepcopy(draft["shots"][0]))
    with pytest.raises(ValidationError):
        ComicStoryDraft.model_validate(draft)


@pytest.mark.parametrize("duration", [0, -1, 61, 0.05])
def test_invalid_duration_is_saved_then_located_by_validation(story_app, monkeypatch, duration):
    app = story_app
    pid, cid, image = _setup(app)
    draft = _draft(image)
    draft["shots"][0]["duration_seconds"] = duration
    _save(app, pid, cid, draft)
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)
    checked = app.comic_story_operation(cid, "review", _request(app, pid, cid, "duration-check"))
    assert checked["review"]["status"] == "needs_revision"
    finding = checked["review"]["findings"][0]
    assert (finding["shot_id"], finding["field"], finding["code"]) == (
        "story-shot-1", "duration_seconds", "duration_conflict")
    assert "0.1–60" in finding["suggestion"]
    with pytest.raises(ToolError, match="尚未通过校验"):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "invalid-duration"))


def test_content_edit_clears_confirmation_restore_appends_and_stale_write_fails(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    v1 = _save(app, pid, cid, _draft(image))
    confirmed = _confirm(app, pid, cid, monkeypatch)
    assert confirmed["confirmation"]
    stale = {**_request(app, pid, cid, "stale"), "draft": _draft(image, title="Stale copy")}
    v2 = _save(app, pid, cid, _draft(image, title="Edited title"), "save-2")
    assert v2["story"]["version"] == 2
    assert not v2["confirmation"]
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "save", stale)
    restored = app.comic_story_operation(cid, "restore", {
        **_request(app, pid, cid, "restore"), "version": 1})
    assert restored["story"]["version"] == 3
    assert _story_draft(restored["story"]) == _story_draft(v1["story"])
    assert not restored["confirmation"]
    assert app.get_comic_story(cid, version=1)["story"] == v1["story"]


def test_incomplete_draft_is_saved_but_not_confirmed_and_review_invalidates_old_confirmation(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    incomplete = _draft(image)
    incomplete["shots"][0]["duration_seconds"] = None
    saved = _save(app, pid, cid, incomplete)
    assert saved["story"]["shots"][0]["duration_seconds"] is None
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "incomplete-confirm"))
    _save(app, pid, cid, _draft(image), "complete-fields")
    _confirm(app, pid, cid, monkeypatch)
    warning = {"status": "warning", "findings": [{"severity": "warning",
        "shot_id": "story-shot-1", "field": "camera", "message": "Consider another angle.",
        "evidence": "Camera remains still."}]}
    # 内容没改：复核沿用原结论与确认，不会"上次通过、这次不通过"。
    _review(app, pid, cid, monkeypatch, output=warning, key="same-content-review")
    assert app.get_comic_story(cid)["confirmation"]
    # 真的改了内容：旧结论失效，新的校验结果会覆盖原来的确认。
    _save(app, pid, cid, _draft(image, title="改过的标题"), "edited-after-confirm")
    reviewed, _ = _review(app, pid, cid, monkeypatch, output=warning, key="new-review")
    assert not reviewed["confirmation"]
    assert reviewed["review"]["status"] == "warning"
    assert not app.get_comic_story(cid)["confirmation"]


def test_first_story_anchor_is_claimed_once_under_two_projects(story_app):
    app = story_app
    pid, cid, first = _setup(app)
    other_pid = _project(app)
    second = _upload(app, other_pid, cid, content=_png(40))
    _select(app, cid, second)
    requests = [(pid, {**_request(app, pid, cid, "anchor-a"), "draft": _draft(first)}),
                (other_pid, {**_request(app, other_pid, cid, "anchor-b"),
                             "draft": _draft(second)})]
    barrier = threading.Barrier(2)

    def save(item):
        project_id, request = item
        barrier.wait(timeout=10)
        try:
            result = app.comic_story_operation(cid, "save", request)
            return project_id, result
        except ToolError:
            return project_id, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, requests))
    winners = [(project_id, result) for project_id, result in results if result is not None]
    assert len(winners) == 1
    assert app.get_comic_story(cid)["story"]["anchor_project_id"] == winners[0][0]
    assert len(app.comic_story_versions(cid)["versions"]) == 1


def test_ai_draft_receives_selected_pixels_only_and_replay_makes_no_calls(story_app, monkeypatch):
    app = story_app
    pid, cid, first = _setup(app)
    other_pid = _project(app, "Selected second source")
    second = _upload(app, other_pid, cid, content=_png(71))
    _select(app, cid, second)
    excluded_pid = _project(app, "UNSELECTED_SOURCE_MUST_NOT_ENTER_STORY")
    excluded = _upload(app, excluded_pid, cid, content=_png(93))
    _select(app, cid, excluded)
    draft = _draft(second, first)
    draft["instruction"] = "Two independent subjects."
    vision_calls, review_calls = [], []

    def vision(messages):
        vision_calls.append(messages)
        return json.dumps(draft)

    def review(messages):
        review_calls.append(messages)
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    request = {**_request(app, pid, cid, "generate-pixels"),
               "images": [_ref(second), _ref(first)], "instruction": "Two independent subjects."}
    result = app.comic_story_operation(cid, "generate", request)
    assert _story_draft(result["story"]) == draft
    urls = [part["image_url"]["url"] for message in vision_calls[0]
            if isinstance(message["content"], list) for part in message["content"]
            if part["type"] == "image_url"]
    assert len(urls) == 2
    assert {base64.b64decode(url.split(",", 1)[1]) for url in urls} == {_png(), _png(71)}
    public_input = json.dumps(vision_calls + review_calls, ensure_ascii=False)
    assert "UNSELECTED_SOURCE_MUST_NOT_ENTER_STORY" not in public_input
    assert excluded["image_id"] not in public_input
    assert len(vision_calls) == len(review_calls) == 1
    monkeypatch.setattr(app, "_comic_image_vision_model", _forbidden)
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)
    replay = app.comic_story_operation(cid, "generate", request)
    assert replay["story"] == result["story"]
    assert app.get_comic_story(cid)["story"] == result["story"]


@pytest.mark.parametrize("failure", ["bad-json", "unknown-image", "provider"])
def test_failed_generation_preserves_saved_story_and_replay_never_resubmits(
    story_app, monkeypatch, failure,
):
    app = story_app
    pid, cid, image = _setup(app)
    original = _save(app, pid, cid, _draft(image))["story"]
    calls = []

    def model(messages):
        calls.append(messages)
        if failure == "provider":
            raise ToolError("Offline provider result unavailable")
        if failure == "bad-json":
            return "not valid JSON"
        draft = _draft(image)
        draft["instruction"] = "A small independent sequence."
        draft["shots"][0]["image"]["image_id"] = "unknown-image"
        return json.dumps(draft)

    monkeypatch.setattr(app, "_comic_image_vision_model", model)
    request = {**_request(app, pid, cid, "unavailable-generation"),
               "images": [_ref(image)], "instruction": "A small independent sequence."}
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "generate", request)
    assert app.get_comic_story(cid)["story"] == original
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "generate", request)
    assert len(calls) == 1


def test_review_ids_and_warnings_cannot_be_bypassed(story_app, monkeypatch):
    app = story_app
    pid, cid, image = _setup(app)
    _save(app, pid, cid, _draft(image))
    finding = {"severity": "warning", "shot_id": "story-shot-1", "field": "camera",
               "message": "Offline artistic suggestion", "evidence": "Camera remains still."}
    with pytest.raises(ToolError):
        _review(app, pid, cid, monkeypatch, output={"status": "warning", "findings": [
            {**finding, "shot_id": "unknown-shot"}]}, key="invalid-review")
    assert not app.get_comic_story(cid)["confirmation"]
    result, _ = _review(app, pid, cid, monkeypatch,
                       output={"status": "warning", "findings": [finding]}, key="valid-warning")
    assert result["review"]["status"] == "warning"
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "reject-warning"))
    accepted = app.comic_story_operation(cid, "confirm", {
        **_request(app, pid, cid, "accept-warning"), "accept_warnings": True})
    assert accepted["confirmation"]
    assert accepted["review"]["status"] == "warning"


def test_submitted_request_does_not_issue_parallel_model_call(story_app, monkeypatch):
    from kantoku.domains.comic.models import ComicStoryGenerateRequest
    from kantoku.domains.comic.stories import ComicStoryService

    app = story_app
    pid, cid, image = _setup(app)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def model(messages):
        calls.append(messages)
        entered.set()
        assert release.wait(timeout=15)
        draft = _draft(image)
        draft["instruction"] = "Keep the fixed image."
        return json.dumps(draft)

    monkeypatch.setattr(app, "_comic_image_vision_model", model)
    monkeypatch.setattr(app, "_comic_constraint_model",
                        lambda _: json.dumps({"status": "pass", "findings": []}))
    request = {**_request(app, pid, cid, "pending-generation"),
               "images": [_ref(image)], "instruction": "Keep the fixed image."}
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(app.comic_story_operation, cid, "generate", request)
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(ToolError):
                ComicStoryService(app.comic_media).generate(
                    ComicStoryGenerateRequest.model_validate(request),
                    vision_model_call=_forbidden, model_call=_forbidden,
                    model="offline", vision_model="offline", run_id="unexecuted-replay")
        finally:
            release.set()
        assert pending.result(timeout=15)["story"]
    assert len(calls) == 1


def test_late_generation_is_retained_as_candidate_without_overwriting_or_recalling_model(
    story_app, monkeypatch,
):
    from kantoku.domains.comic.models import ComicStorySaveRequest
    from kantoku.domains.comic.stories import ComicStoryService

    app = story_app
    pid, cid, image = _setup(app)
    _save(app, pid, cid, _draft(image))
    entered, release = threading.Event(), threading.Event()
    candidate = _draft(image, title="Late offline candidate")
    candidate["instruction"] = "Generate a candidate."

    def model(_messages):
        entered.set()
        assert release.wait(timeout=15)
        return json.dumps(candidate)

    monkeypatch.setattr(app, "_comic_image_vision_model", model)
    monkeypatch.setattr(app, "_comic_constraint_model",
                        lambda _: json.dumps({"status": "pass", "findings": []}))
    request = {**_request(app, pid, cid, "late-generation"),
               "images": [_ref(image)], "instruction": "Generate a candidate."}
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(app.comic_story_operation, cid, "generate", request)
        try:
            assert entered.wait(timeout=10)
            edited = ComicStoryService(app.comic_media).save(ComicStorySaveRequest.model_validate({
                **_request(app, pid, cid, "during-model"),
                "draft": _draft(image, title="User's later edit")}))
        finally:
            release.set()
        with pytest.raises(ToolError):
            pending.result(timeout=15)
    assert app.get_comic_story(cid)["story"] == edited["story"]
    runs = app.runtime_store.list_runs(domain="comic", conversation_id=cid)
    candidates = [run for run in runs if run.state.get("story_candidate")]
    assert len(candidates) == 1
    assert candidates[0].state["story_candidate"]["draft"] == candidate
    monkeypatch.setattr(app, "_comic_image_vision_model", _forbidden)
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)
    adopted = app.comic_story_operation(cid, "adopt-candidate", {
        **_request(app, pid, cid, "adopt-late"), "candidate_run_id": candidates[0].id})
    assert adopted["story"]["version"] == edited["story"]["version"] + 1
    assert _story_draft(adopted["story"]) == candidate
    assert not adopted["confirmation"]


def _jpeg():
    # A real 1x1 JPEG fixture, encoded once with the operating system image codec.
    return base64.b64decode(
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYI"
        "DAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkF"
        "BQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU"
        "FBQUFBQUFBQUFBT/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAA"
        "AAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJx"
        "FDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVW"
        "V1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKz"
        "tLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
        "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAx"
        "EEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
        "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJma"
        "oqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3"
        "+Pn6/9oADAMBAAIRAxEAPwD876KKK6D6A//Z")


def _webp():
    return base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")


def test_export_zip_preserves_formats_bytes_duplicate_mapping_and_shot_order(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, png_image = _setup(app)
    jpeg_image = _upload(app, pid, cid, content=_jpeg(), mime="image/jpeg", filename="source.jpeg")
    webp_image = _upload(app, pid, cid, content=_webp(), mime="image/webp",
                         filename="../../unsafe.webp")
    for image in (jpeg_image, webp_image):
        _select(app, cid, image)
    _save(app, pid, cid, _draft(webp_image, png_image, jpeg_image, webp_image))
    _confirm(app, pid, cid, monkeypatch)
    monkeypatch.setattr(app, "_comic_image_vision_model", _forbidden)
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)
    request = _request(app, pid, cid, "export-formats")
    result = app.comic_story_operation(cid, "exports", request)
    artifact = app.runtime_store.get_artifact(result["export_artifact_id"])
    with ZipFile(BytesIO(Path(artifact.location).read_bytes())) as bundle:
        names = bundle.namelist()
        assert all(not name.startswith(("/", "\\")) and ".." not in name.split("/")
                   for name in names)
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest == result["manifest"]
        assert [item["shot_id"] for item in manifest["shots"]] == [
            "story-shot-1", "story-shot-2", "story-shot-3", "story-shot-4"]
        assert len(manifest["images"]) == 3
        expected = {webp_image["image_id"]: (_webp(), ".webp"),
                    png_image["image_id"]: (_png(), ".png"),
                    jpeg_image["image_id"]: (_jpeg(), ".jpg")}
        for image in manifest["images"]:
            content, suffix = expected[image["image_id"]]
            assert Path(image["filename"]).suffix == suffix
            assert bundle.read(image["filename"]) == content
        assert manifest["shots"][0]["filename"] == manifest["shots"][3]["filename"]
        assert bundle.read("story.md").decode("utf-8") == result["export_text"]
    assert app.comic_story_operation(cid, "exports", request)["export_artifact_id"] == artifact.id
    assert len(app.get_comic_story(cid)["exports"]) == 1


@pytest.mark.parametrize("failure", ["missing", "hash", "trashed"])
def test_unavailable_image_preserves_history_and_blocks_export(story_app, monkeypatch, failure):
    app = story_app
    pid, cid, image = _setup(app)
    story = _save(app, pid, cid, _draft(image))["story"]
    _confirm(app, pid, cid, monkeypatch)
    artifact = app.runtime_store.get_artifact(image["artifact_id"])
    path = Path(artifact.location)
    if failure == "missing":
        path.unlink()
    elif failure == "hash":
        atomic_write_bytes(path, _png(210), owner="offline.story.tamper")
    else:
        app.runtime_store.change_artifact_lifecycle(
            artifact.id, ArtifactLifecycle.TRASHED, artifact.lifecycle_version)
    view = app.get_comic_story(cid)
    assert view["story"] == story
    assert view["missing_images"]
    assert view["export_text"]
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "exports", _request(app, pid, cid, "invalid-export"))
    assert not app.get_comic_story(cid)["exports"]


@pytest.mark.parametrize("stage", ["write", "record"])
def test_atomic_export_failure_does_not_publish_a_fake_success(story_app, monkeypatch, stage):
    from kantoku.domains.comic import stories

    app = story_app
    pid, cid, image = _setup(app)
    _save(app, pid, cid, _draft(image))
    confirmed = _confirm(app, pid, cid, monkeypatch)

    def failed_output(*_args, **_kwargs):
        raise OSError("Offline disk failure before archive commit")

    if stage == "write":
        monkeypatch.setattr(stories, "atomic_write_bytes", failed_output)
    else:
        original_insert = app.comic_projects._insert_version

        def insert_version(*args, **kwargs):
            if kwargs.get("entity_type") == "story_export":
                raise OSError("Offline export record failure after archive write")
            return original_insert(*args, **kwargs)

        monkeypatch.setattr(app.comic_projects, "_insert_version", insert_version)
    with pytest.raises((OSError, ToolError)):
        app.comic_story_operation(cid, "exports", _request(app, pid, cid, "disk-failure"))
    view = app.get_comic_story(cid)
    assert view["story"] == confirmed["story"]
    assert view["confirmation"] == confirmed["confirmation"]
    assert view["exports"] == []
    assert not [artifact for artifact in app.runtime_store.list_artifacts()
                if artifact.source == "comic.story.export"]


def test_generated_instruction_is_bound_to_user_original_before_independent_review(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    instruction = "Use one image for two continuous segments; keep the source identity."
    draft = _draft(image, image)
    draft["instruction"] = "An invented model rewrite of the original request."
    inputs = []
    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(draft))

    def review(messages):
        inputs.append(json.loads(messages[-1]["content"]))
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_constraint_model", review)
    generated = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "original-instruction"),
        "images": [_ref(image)], "instruction": instruction})
    assert generated["story"]["instruction"] == instruction
    assert inputs[0]["draft"]["instruction"] == instruction


def test_direct_upload_sources_are_local_and_do_not_inherit_project_briefs(story_app, monkeypatch):
    app = story_app
    cid = _conversation(app)
    pid = _project(app, "UNRELATED_OLD_USER_REQUEST_A")
    other_pid = _project(app, "UNRELATED_OLD_USER_REQUEST_B")
    images = [_upload(app, pid, cid), _upload(app, other_pid, cid, content=_png(92))]
    for image in images:
        _select(app, cid, image)
    seen = []

    def vision(messages):
        seen.append(json.loads(messages[-1]["content"][0]["text"]))
        draft = _draft(*images)
        draft["instruction"] = "Model text must not replace the user's request."
        return json.dumps(draft)

    def review(messages):
        seen.append(json.loads(messages[-1]["content"]))
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "local-sources"), "images": [_ref(image) for image in images],
        "instruction": "Treat these images as independent subjects."})
    expected = {image["image_id"]: image["project_id"] for image in images}
    for payload in seen:
        sources = payload["image_sources"]
        assert {source["image"]["image_id"]: source["source_project_id"]
                for source in sources} == expected
        assert all(source["constraints"] == [] for source in sources)
        assert all("negative_prompt" not in source for source in sources)
    assert "UNRELATED_OLD_USER_REQUEST" not in json.dumps(seen)
    assert len(result["story"]["source_contexts"]) == 2


@pytest.mark.parametrize("failure", ["hash", "trashed"])
def test_export_content_gateway_rechecks_original_zip(story_app, monkeypatch, failure):
    app = story_app
    pid, cid, image = _setup(app)
    _save(app, pid, cid, _draft(image))
    _confirm(app, pid, cid, monkeypatch)
    exported = app.comic_story_operation(cid, "exports", _request(app, pid, cid, "public-export"))
    artifact = app.runtime_store.get_artifact(exported["export_artifact_id"])
    path = app.get_core_artifact_content(artifact.id)
    assert path.read_bytes().startswith(b"PK")
    if failure == "hash":
        atomic_write_bytes(path, b"PK\x03\x04not-the-published-archive",
                           file_type="export", owner="offline.story.tamper")
    else:
        app.runtime_store.change_artifact_lifecycle(
            artifact.id, ArtifactLifecycle.TRASHED, artifact.lifecycle_version)
    with pytest.raises(ToolError):
        app.get_core_artifact_content(artifact.id)
    assert app.get_comic_story(cid)["exports"][0]["available"] is False


def test_http_story_save_and_read_routes_persist_without_model_calls(story_app):
    app = story_app
    pid, cid, image = _setup(app)
    server = web_studio.make_server(app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}
    path = f"/api/comic/conversations/{cid}/story"
    try:
        connection.request("PUT", path, json.dumps({
            **_request(app, pid, cid, "http-save"), "draft": _draft(image)}), headers)
        response = connection.getresponse()
        assert response.status in (200, 201)
        saved = json.loads(response.read())
        created = [event for event in app.runtime_store.list_events(saved["run_id"])
                   if event.payload.get("artifact_id") == saved["story"]["artifact_id"]]
        assert len(created) == 1 and created[0].payload["type"] == "document"
        for suffix in ("", "?version=1", "/versions", "/images"):
            connection.request("GET", path + suffix, headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            payload = json.loads(response.read())
            if suffix in ("", "?version=1"):
                assert payload["story"] == saved["story"]
        connection.request("POST", path + "/confirm", json.dumps(
            _request(app, pid, cid, "http-unreviewed-confirm")), headers)
        response = connection.getresponse()
        assert response.status == 400
        failure = json.loads(response.read())
        assert failure["error_id"] and failure["trace_id"]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()


def _generate(app, pid, cid, image, monkeypatch, *, draft, vision, review=None, key="generate"):
    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(vision))
    monkeypatch.setattr(app, "_comic_constraint_model",
                        lambda _: json.dumps(review or {"status": "pass", "findings": []}))
    return app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, key), "images": [_ref(image)],
        "instruction": draft.get("instruction", ""), "draft": draft})


def test_generate_uses_the_filled_framework_and_keeps_user_words(story_app, monkeypatch):
    app = story_app
    pid, cid, image = _setup(app)
    skeleton = {
        "title": "橙留香独自面对敌军", "summary": "他举起刀，独自走向阵前。", "instruction": "",
        "shots": [{"shot_id": "user-shot-1", "image": _ref(image), "action": "",
                   "camera": "", "duration_seconds": None, "transition": ""}],
    }
    model_draft = {"title": "模型改写的标题", "summary": "模型改写的概述", "instruction": "",
                   "shots": [{"shot_id": "ai-shot", "image": _ref(image),
                              "action": "他把握在手里的青龙偃月刀缓缓抬起", "camera": "推近到刀身",
                              "duration_seconds": 3.5, "transition": "切入下一镜"}]}
    seen = []

    def vision(messages):
        seen.append(json.loads(messages[-1]["content"][0]["text"]))
        return json.dumps(model_draft)

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model",
                        lambda _: json.dumps({"status": "pass", "findings": []}))
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "framework"), "images": [_ref(image)],
        "instruction": "", "draft": skeleton})

    story = result["story"]
    # 用户已经写好的文字不被模型改写；模型只补空缺。
    assert story["title"] == "橙留香独自面对敌军"
    assert story["summary"] == "他举起刀，独自走向阵前。"
    assert story["shots"][0]["shot_id"] == "user-shot-1"
    assert story["shots"][0]["image"] == _ref(image)
    assert story["shots"][0]["action"] == "他把握在手里的青龙偃月刀缓缓抬起"
    assert story["shots"][0]["duration_seconds"] == 3.5
    # 模型确实收到了这份框架，且要求为空是合法的。
    assert seen[0]["current_draft"]["title"] == "橙留香独自面对敌军"
    assert seen[0]["instruction"] == ""
    assert result["review"]["status"] == "pass"


def test_review_names_the_shot_field_type_and_fix_without_calling_the_model(story_app, monkeypatch):
    app = story_app
    pid, cid, image = _setup(app)
    _save(app, pid, cid, {"title": "只有标题", "summary": "", "instruction": "", "shots": [
        {"shot_id": "shot-a", "image": _ref(image), "action": "",
         "camera": "", "duration_seconds": None, "transition": ""}]}, "incomplete")
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)

    result = app.comic_story_operation(cid, "review", _request(app, pid, cid, "structural"))
    review = result["review"]
    assert review["status"] == "needs_revision"
    assert {(item["field"], item["code"]) for item in review["findings"]} == {
        ("summary", "missing_summary"), ("transition", "missing_transition"),
        ("action", "missing_action"), ("camera", "missing_camera"),
        ("duration_seconds", "missing_duration")}
    assert {item["shot_id"] for item in review["findings"]} == {None, "shot-a"}
    assert all(item["severity"] == "error" and item["suggestion"] for item in review["findings"])
    assert all("镜头 1" in item["message"] for item in review["findings"] if item["shot_id"])
    with pytest.raises(ToolError, match="尚未通过校验"):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "blocked"))


def test_unchanged_content_reuses_the_same_verdict_instead_of_asking_again(story_app, monkeypatch):
    app = story_app
    pid, cid, image = _setup(app)
    _save(app, pid, cid, _draft(image))
    calls = []

    def model(messages):
        calls.append(messages)
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_constraint_model", model)
    first = app.comic_story_operation(cid, "review", _request(app, pid, cid, "first"))
    second = app.comic_story_operation(cid, "review", _request(app, pid, cid, "second"))
    assert first["review"]["status"] == second["review"]["status"] == "pass"
    assert len(calls) == 1 and second.get("review_reused") is True


def test_ai_draft_passes_validation_until_the_user_edits_it(story_app, monkeypatch):
    app = story_app
    pid, cid, image = _setup(app)
    review_calls = []

    def review_model(messages):
        review_calls.append(messages)
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(_draft(image)))
    monkeypatch.setattr(app, "_comic_constraint_model", review_model)
    generated = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "gen"), "images": [_ref(image)], "instruction": "one shot"})
    assert generated["review"]["validation_requested"] is False
    with pytest.raises(ToolError, match="尚未通过校验"):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "before-check"))
    checked = app.comic_story_operation(cid, "review", _request(app, pid, cid, "check"))
    assert checked["review"]["status"] == "pass"
    assert checked["review"]["validation_requested"] is True
    # 生成时已核验一次；内容没改，校验直接复用，不再付费调用。
    assert len(review_calls) == 1

    edited = _draft(image, title="改过的标题")
    _save(app, pid, cid, edited, "edit-after-draft")
    assert app.get_comic_story(cid)["review"] is None
    again = app.comic_story_operation(cid, "review", _request(app, pid, cid, "recheck"))
    assert again["review"]["status"] == "pass"
    assert len(review_calls) == 2

    confirmed = app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "confirmed"))
    assert confirmed["confirmation"]["confirmed"]
    exported = app.comic_story_operation(cid, "exports", _request(app, pid, cid, "exported"))
    assert exported["exports"][0]["story_version"] == again["story"]["version"]


def test_ai_repairs_only_reported_fields_before_publishing_a_passing_draft(story_app, monkeypatch):
    app = story_app
    pid, cid, image = _setup(app)
    draft = _draft(image)
    fixed = copy.deepcopy(draft)
    fixed["shots"][0]["camera"] = "Slowly move forward."
    fixed["title"] = "Unrequested rewrite must be ignored"
    outputs = iter([draft, fixed])
    review_outputs = iter([{"status": "needs_revision", "findings": [{
        "severity": "error", "shot_id": "story-shot-1", "field": "camera",
        "code": "camera_conflict", "message": "Camera conflicts with the requested movement.",
        "evidence": draft["shots"][0]["camera"], "suggestion": "Move the camera forward."}]},
        {"status": "pass", "findings": []}])
    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(next(outputs)))
    monkeypatch.setattr(app, "_comic_constraint_model", lambda _: json.dumps(next(review_outputs)))
    generated = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "repair"), "images": [_ref(image)], "instruction": "Push in."})
    assert generated["story"]["title"] == draft["title"]
    assert generated["story"]["shots"][0]["camera"] == "Slowly move forward."
    assert generated["review"]["status"] == "pass"
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)
    checked = app.comic_story_operation(cid, "review", _request(app, pid, cid, "check-repair"))
    assert checked["review"]["status"] == "pass" and checked["review_reused"]


@pytest.mark.parametrize("structural", [False, True])
def test_unresolved_ai_errors_preserve_candidate_and_never_replace_current_story(
    story_app, monkeypatch, structural,
):
    app = story_app
    pid, cid, image = _setup(app)
    original = _save(app, pid, cid, _draft(image))["story"]
    draft = _draft(image, title="Invalid generated candidate")
    if structural:
        draft["shots"][0]["transition"] = ""
    output = {"status": "needs_revision", "findings": [{
        "severity": "error", "shot_id": "story-shot-1", "field": "camera",
        "code": "camera_conflict", "message": "Conflicting camera movement.",
        "evidence": draft["shots"][0]["camera"], "suggestion": "Remove the conflicting movement."}]}
    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(draft))
    monkeypatch.setattr(app, "_comic_constraint_model",
                        _forbidden if structural else lambda _: json.dumps(output))
    with pytest.raises(ToolError, match="初稿自动修复尚未完成.*候选已保留") as error:
        app.comic_story_operation(cid, "generate", {
            **_request(app, pid, cid, "unresolved"), "images": [_ref(image)],
            "instruction": "A new sequence."})
    assert len(error.value.message) < 100
    assert app.get_comic_story(cid)["story"] == original
    runs = app.runtime_store.list_runs(domain="comic")
    candidates = [run.state["story_candidate"] for run in runs
                  if run.state.get("story_candidate")]
    assert len(candidates) == 1 and candidates[0]["review"]["status"] == "needs_revision"


def test_user_edit_is_rechecked_and_errors_block_confirmation_until_fixed(story_app, monkeypatch):
    app = story_app
    pid, cid, image = _setup(app)
    _save(app, pid, cid, _draft(image))
    _confirm(app, pid, cid, monkeypatch)
    edited = _draft(image)
    edited["shots"][0]["camera"] = "Move left and right simultaneously."
    _save(app, pid, cid, edited, "bad-edit")
    assert app.get_comic_story(cid)["review"] is None
    output = {"status": "pass", "findings": [{
        "severity": "error", "shot_id": "story-shot-1", "field": "camera",
        "code": "camera_conflict", "message": "Choose one camera direction.",
        "evidence": edited["shots"][0]["camera"], "suggestion": "Move left only."}]}
    checked, _ = _review(app, pid, cid, monkeypatch, output=output, key="bad-edit-review")
    assert checked["review"]["status"] == "needs_revision"
    assert checked["review"]["findings"][0]["suggestion"] == "Move left only."
    with pytest.raises(ToolError, match="尚未通过校验"):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "bad-edit-confirm"))
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "exports", _request(app, pid, cid, "bad-edit-export"))
    edited["shots"][0]["camera"] = "Move left only."
    _save(app, pid, cid, edited, "fixed-edit")
    assert _confirm(app, pid, cid, monkeypatch, key="fixed-confirm")["confirmation"]["confirmed"]


def test_review_rejects_failure_without_location_and_supplies_legacy_suggestion(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    draft = _draft(image)
    _save(app, pid, cid, draft)
    with pytest.raises(ToolError, match="位置和修改建议"):
        _review(app, pid, cid, monkeypatch,
                output={"status": "needs_revision", "findings": []}, key="empty-findings")
    checked, _ = _review(app, pid, cid, monkeypatch, key="legacy", output={
        "status": "needs_revision", "findings": [{"severity": "error", "shot_id": "story-shot-1",
        "field": "action", "message": "Describe one clear action.",
        "evidence": draft["shots"][0]["action"]}]})
    assert checked["review"]["findings"][0]["suggestion"]


@pytest.mark.parametrize("representation", [
    "whitespace", "duration", "duration_unit", "image_json",
])
def test_review_accepts_equivalent_field_formatting_without_dropping_findings(
    story_app, monkeypatch, representation,
):
    app = story_app
    pid, cid, image = _setup(app)
    draft = _draft(image)
    draft["shots"][0]["camera"] = "Track  slowly\nforward."
    _save(app, pid, cid, draft)
    field, evidence = {
        "whitespace": ("camera", "Track slowly forward."),
        "duration": ("duration_seconds", "4"),
        "duration_unit": ("duration_seconds", "4.00 秒"),
        "image_json": ("image", json.dumps({"version": image["version"],
                                            "image_id": image["image_id"]}, separators=(",", ":"))),
    }[representation]
    checked, calls = _review(app, pid, cid, monkeypatch, output={
        "status": "needs_revision", "findings": [{
            "severity": "error", "shot_id": "story-shot-1", "field": field,
            "message": "Resolve this field conflict.", "evidence": evidence,
            "suggestion": "Choose one consistent value."}]})
    assert len(calls) == 1
    review = checked["review"]
    assert review["status"] == "needs_revision" and len(review["findings"]) == 1
    quote = review["findings"][0]["evidence"]
    if field == "image":
        assert json.loads(quote) == _ref(image)
    elif field == "duration_seconds":
        assert quote == "4.0"
    else:
        assert quote == draft["shots"][0][field]
    with pytest.raises(ToolError, match="尚未通过校验"):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "blocked"))


@pytest.mark.parametrize("invalid", [
    "blank", "invented", "wrong_field", "duration_substring", "wrong_image_version",
    "string_image_version", "extra_image_field", "unknown_shot",
])
def test_review_never_accepts_evidence_from_another_value_or_scope(story_app, monkeypatch, invalid):
    app = story_app
    pid, cid, image = _setup(app)
    _save(app, pid, cid, _draft(image))
    finding = {"severity": "error", "shot_id": "story-shot-1", "field": "camera",
               "message": "A public conflict.", "evidence": "invented evidence"}
    finding.update({
        "blank": {"evidence": " \n "},
        "invented": {},
        "wrong_field": {"evidence": "A small visible movement in shot 1."},
        "duration_substring": {"field": "duration_seconds", "evidence": "14"},
        "wrong_image_version": {"field": "image", "evidence": json.dumps({
            "image_id": image["image_id"], "version": image["version"] + 1})},
        "string_image_version": {"field": "image", "evidence": json.dumps({
            "image_id": image["image_id"], "version": str(image["version"])})},
        "extra_image_field": {"field": "image", "evidence": json.dumps({
            **_ref(image), "untrusted": True})},
        "unknown_shot": {"shot_id": "not-a-current-shot", "evidence": "Camera remains still."},
    }[invalid])
    calls = []

    def model(messages):
        calls.append(messages)
        return json.dumps({"status": "needs_revision", "findings": [finding]})

    monkeypatch.setattr(app, "_comic_constraint_model", model)
    with pytest.raises(ToolError, match="审核"):
        app.comic_story_operation(cid, "review", _request(app, pid, cid, "invalid-evidence"))
    assert len(calls) == 2
    assert app.get_comic_story(cid)["review"] is None
    with pytest.raises(ToolError, match="尚未通过校验"):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "blocked"))


def test_review_corrects_bad_public_structure_once_and_preserves_verified_errors(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    _save(app, pid, cid, _draft(image))
    good = {"severity": "error", "shot_id": "story-shot-1", "field": "camera",
            "message": "A real camera conflict.", "evidence": "Camera remains still."}
    bad = {**good, "field": "action", "evidence": "INVALID_RESPONSE_MUST_NOT_BE_ECHOED"}
    replies = iter([{"status": "needs_revision", "findings": [good, bad]},
                    {"status": "pass", "findings": []}])
    calls = []

    def model(messages):
        calls.append(messages)
        return json.dumps(next(replies))

    monkeypatch.setattr(app, "_comic_constraint_model", model)
    checked = app.comic_story_operation(cid, "review", _request(app, pid, cid, "correct-review"))
    assert len(calls) == 2
    assert "INVALID_RESPONSE_MUST_NOT_BE_ECHOED" not in json.dumps(calls[1])
    assert "verified_findings" in calls[1][-1]["content"]
    assert checked["review"]["status"] == "needs_revision"
    assert len(checked["review"]["findings"]) == 1
    with pytest.raises(ToolError, match="尚未通过校验"):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "blocked"))


@pytest.mark.parametrize("stage", ["first_review", "after_repair"])
@pytest.mark.parametrize("failure", ["evidence", "schema", "transport"])
def test_review_failures_retain_recoverable_candidate_without_replacing_current_story(
    story_app, monkeypatch, stage, failure,
):
    app = story_app
    pid, cid, image = _setup(app)
    original = _save(app, pid, cid, _draft(image))["story"]
    draft = _draft(image, title="A new independent sequence")
    fixed = copy.deepcopy(draft)
    fixed["shots"][0]["camera"] = "Track forward."
    visions = iter([draft, fixed])
    vision_calls, review_calls = [], []
    first_review = {"status": "needs_revision", "findings": [{
        "severity": "error", "shot_id": "story-shot-1", "field": "camera",
        "code": "camera_conflict", "message": "Choose the requested movement.",
        "evidence": "Camera remains still.", "suggestion": "Track forward."}]}

    def vision(messages):
        vision_calls.append(messages)
        return json.dumps(next(visions))

    def review(messages):
        review_calls.append(messages)
        if stage == "after_repair" and len(review_calls) == 1:
            return json.dumps(first_review)
        if failure == "transport":
            raise ToolError("Offline transport result unknown")
        if failure == "schema":
            return '{"private_reasoning":"DO_NOT_ECHO_INVALID_RESPONSE"}'
        output = copy.deepcopy(first_review)
        output["findings"][0]["evidence"] = "Not present in any current field."
        return json.dumps(output)

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    request = {**_request(app, pid, cid, "review-failed"),
               "images": [_ref(image)], "instruction": "Track forward."}
    with pytest.raises(ToolError, match="候选已保留"):
        app.comic_story_operation(cid, "generate", request)
    assert len(vision_calls) == (2 if stage == "after_repair" else 1)
    assert len(review_calls) == (1 if failure == "transport" else 2) + (stage == "after_repair")
    assert "DO_NOT_ECHO_INVALID_RESPONSE" not in json.dumps(review_calls)
    assert app.get_comic_story(cid)["story"] == original
    runs = [run for run in app.runtime_store.list_runs(domain="comic", conversation_id=cid)
            if run.state.get("story_candidate")]
    assert len(runs) == 1
    candidate = runs[0].state["story_candidate"]
    assert candidate["draft"]["title"] == draft["title"]
    assert candidate["draft"]["shots"] == draft["shots"]
    assert ("review" in candidate) == (stage == "after_repair")
    if stage == "after_repair":
        assert candidate["review"]["status"] == "needs_revision"
        assert candidate["review"]["findings"][0]["evidence"] == "Camera remains still."
    monkeypatch.setattr(app, "_comic_image_vision_model", _forbidden)
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "generate", request)
    recovered = app.comic_story_operation(cid, "adopt-candidate", {
        **_request(app, pid, cid, "recover"), "candidate_run_id": runs[0].id})
    assert recovered["confirmation"] is None
    with pytest.raises(ToolError, match="尚未通过校验"):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "blocked"))
    if stage == "after_repair":
        # 原候选有真实错误，恢复后不能靠重按校验将错误清掉；先修改被指出的字段。
        corrected = _story_draft(recovered["story"])
        corrected["shots"][0]["camera"] = fixed["shots"][0]["camera"]
        _save(app, pid, cid, corrected, "fix-recovered")
    assert _confirm(app, pid, cid, monkeypatch)["confirmation"]["confirmed"]


def test_repair_with_no_effective_change_keeps_real_review_without_calling_again(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    skeleton = _draft(image)
    generated = copy.deepcopy(skeleton)
    vision_calls, review_calls = [], []

    def vision(messages):
        vision_calls.append(messages)
        return json.dumps(generated)

    def review(messages):
        review_calls.append(messages)
        assert len(review_calls) == 1, "An unchanged draft must not be reviewed twice"
        return json.dumps({"status": "needs_revision", "findings": [{
            "severity": "error", "shot_id": "story-shot-1", "field": "camera",
            "message": "User camera conflicts with requested tracking.",
            "evidence": "Camera remains still.", "suggestion": "Track forward."}]})

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    with pytest.raises(ToolError, match="初稿自动修复尚未完成"):
        app.comic_story_operation(cid, "generate", {
            **_request(app, pid, cid, "no-op-repair"), "images": [_ref(image)],
            "draft": skeleton, "instruction": "Track forward."})
    assert len(vision_calls) == 2 and len(review_calls) == 1


def test_ai_corrects_nonempty_problem_fields_and_preserves_valid_user_content(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, first = _setup(app)
    second = _upload(app, pid, cid, content=_png(7), filename="another-source.png")
    _select(app, cid, second)
    skeleton = _draft(first, second, title="User's independent sequence")
    skeleton["summary"] = "An unfinished, inconsistent summary."
    skeleton["shots"][0].update(action="Sit and stand at the same time.",
                                camera="Move left and right at the same time.",
                                transition="Continue the incompatible movement.")
    skeleton["shots"][1]["action"] = "Raise a hand and deliver the requested spoken line."
    fixed = copy.deepcopy(skeleton)
    fixed["summary"] = "A coherent sequence with the requested speech."
    fixed["shots"][0].update(action="Stand up, then remain standing.",
                             camera="Track slowly to the left.", transition="Cut to the next shot.")
    fixed["shots"][1]["duration_seconds"] = 8.0
    # 完整模型响应中的顺手改写不应落盘，问题字段则必须改掉已有原文。
    fixed["title"] = "Unrequested model title"
    fixed["shots"][1]["camera"] = "Track slowly forward while the spoken line completes."
    fixed["shots"].reverse()
    changed = [(None, "summary"), ("story-shot-1", "action"), ("story-shot-1", "camera"),
               ("story-shot-1", "transition"), ("story-shot-2", "duration_seconds")]
    findings = []
    for shot_id, field in changed:
        source = next(shot for shot in skeleton["shots"] if shot["shot_id"] == shot_id) \
            if shot_id else skeleton
        findings.append({"severity": "error", "shot_id": shot_id, "field": field,
                         "message": "Resolve the inconsistent field.",
                         "evidence": str(source[field]), "suggestion": "Use a consistent value."})
    visions = iter([skeleton, fixed])
    reviews = iter([{"status": "needs_revision", "findings": findings},
                    {"status": "pass", "findings": []}])
    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(next(visions)))
    monkeypatch.setattr(app, "_comic_constraint_model", lambda _: json.dumps(next(reviews)))
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "correct-filled"), "images": [_ref(first), _ref(second)],
        "draft": skeleton, "instruction": "Preserve the dialogue; correct inconsistent fields."})
    story = result["story"]
    assert result["review"]["status"] == "pass"
    assert story["title"] == skeleton["title"]
    assert story["summary"] == fixed["summary"]
    assert [shot["shot_id"] for shot in story["shots"]] == ["story-shot-1", "story-shot-2"]
    assert [shot["image"] for shot in story["shots"]] == [_ref(first), _ref(second)]
    assert story["shots"][0]["action"] == "Stand up, then remain standing."
    assert story["shots"][0]["camera"] == "Track slowly to the left."
    assert story["shots"][0]["transition"] == "Cut to the next shot."
    assert story["shots"][1]["duration_seconds"] == 8.0
    assert story["shots"][1]["camera"] == "Track slowly forward while the spoken line completes."
    assert story["shots"][1]["action"] == skeleton["shots"][1]["action"]
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)
    monkeypatch.setattr(app, "_comic_image_vision_model", _forbidden)
    checked = app.comic_story_operation(cid, "review", _request(app, pid, cid, "check-corrected"))
    assert checked["review_reused"] and checked["review"]["validation_requested"]
    confirmed = app.comic_story_operation(
        cid, "confirm", _request(app, pid, cid, "confirm-corrected"))
    assert confirmed["confirmation"]["confirmed"]
    exported = app.comic_story_operation(
        cid, "exports", _request(app, pid, cid, "export-corrected"))
    assert exported["exports"][0]["story_version"] == story["version"]


@pytest.mark.parametrize("passes", [True, False])
def test_ai_applies_followup_corrections_but_stops_after_two_content_repairs(
    story_app, monkeypatch, passes,
):
    app = story_app
    pid, cid, image = _setup(app)
    skeleton = _draft(image)
    first = copy.deepcopy(skeleton)
    first["shots"][0]["camera"] = "Track forward."
    second = copy.deepcopy(first)
    second["shots"][0]["duration_seconds"] = 8.0
    calls = {"vision": 0, "review": 0}
    visions = iter([skeleton, first, second])

    def vision(_messages):
        calls["vision"] += 1
        return json.dumps(next(visions))

    def review(messages):
        calls["review"] += 1
        draft = json.loads(messages[1]["content"])["draft"]
        if draft["shots"][0]["camera"] != "Track forward.":
            field = "camera"
        elif draft["shots"][0]["duration_seconds"] != 8.0 or not passes:
            field = "duration_seconds"
        else:
            return json.dumps({"status": "pass", "findings": []})
        return json.dumps({"status": "needs_revision", "findings": [{
            "severity": "error", "shot_id": "story-shot-1", "field": field,
            "message": "Correct this remaining conflict.",
            "evidence": str(draft["shots"][0][field]), "suggestion": "Adjust this field."}]})

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    request = {**_request(app, pid, cid, "two-corrections"), "images": [_ref(image)],
               "draft": skeleton, "instruction": "Track forward with enough time."}
    if passes:
        result = app.comic_story_operation(cid, "generate", request)
        assert result["review"]["status"] == "pass"
        assert result["story"]["shots"][0]["duration_seconds"] == 8.0
        assert result["story"]["shots"][0]["camera"] == "Track forward."
    else:
        with pytest.raises(ToolError, match="自动修复尚未完成"):
            app.comic_story_operation(cid, "generate", request)
        assert app.get_comic_story(cid)["story"] is None
    assert calls == {"vision": 3, "review": 3}


@pytest.mark.parametrize("subject", ["A robot crosses a rain-soaked bridge",
                                   "A keeper lights a coastal lighthouse"])
def test_repaired_multishot_draft_recovers_from_stale_evidence_then_validates_and_exports(
    story_app, monkeypatch, subject,
):
    app = story_app
    pid, cid, first = _setup(app)
    second = _upload(app, pid, cid, content=_webp(), mime="image/webp", filename="second.webp")
    _select(app, cid, second)
    draft = _draft(first, second, title=subject)
    draft["summary"] = subject + "; two related moments."
    fixed = copy.deepcopy(draft)
    fixed["shots"][1]["camera"] = "Track  slowly\nforward."
    visions = iter([draft, fixed])
    first_review = {"status": "needs_revision", "findings": [{
        "severity": "error", "shot_id": "story-shot-2", "field": "camera",
        "message": "Movement must follow the request.", "evidence": "Camera remains still.",
        "suggestion": "Track forward."}]}
    # 复审误引修复前的旧字段；一次契约纠正应读取当前稿，而不是丢弃整个故事。
    replies = iter([first_review, first_review, {"status": "pass", "findings": []}])
    calls = []

    def review(messages):
        calls.append(messages)
        return json.dumps(next(replies))

    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(next(visions)))
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    generated = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "complete-pipeline"), "images": [_ref(first), _ref(second)],
        "instruction": "Track forward in the second shot."})
    assert len(calls) == 3
    assert generated["story"]["title"] == subject
    assert generated["story"]["shots"][1]["camera"] == fixed["shots"][1]["camera"]
    assert generated["review"]["status"] == "pass"
    assert generated["review"]["validation_requested"] is False
    monkeypatch.setattr(app, "_comic_image_vision_model", _forbidden)
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)
    checked = app.comic_story_operation(cid, "review", _request(app, pid, cid, "validate"))
    assert checked["review_reused"] and checked["review"]["validation_requested"]
    confirmed = app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "confirm"))
    assert confirmed["confirmation"]["confirmed"]
    exported = app.comic_story_operation(cid, "exports", _request(app, pid, cid, "export"))
    artifact = app.runtime_store.get_artifact(exported["exports"][0]["artifact_id"])
    with ZipFile(artifact.location) as bundle:
        contents = [bundle.read(name) for name in bundle.namelist() if name.startswith("images/")]
    assert sorted(contents) == sorted([_png(), _webp()])


@pytest.mark.parametrize("old_contract", [None, "comic-story-1", "comic-story-2"])
def test_restoring_an_old_candidate_rechecks_instead_of_relabeling_its_old_review(
    story_app, monkeypatch, old_contract,
):
    app = story_app
    pid, cid, image = _setup(app)
    draft = _draft(image)
    old_review = {"status": "needs_revision", "findings": [{
        "severity": "error", "shot_id": "story-shot-1", "field": "camera",
        "message": "An old static-image policy rejected this camera.",
        "evidence": draft["shots"][0]["camera"], "suggestion": "Reconsider under current policy."}]}
    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(draft))
    monkeypatch.setattr(app, "_comic_constraint_model", lambda _: json.dumps(old_review))
    with pytest.raises(ToolError, match="自动修复尚未完成"):
        app.comic_story_operation(cid, "generate", {
            **_request(app, pid, cid, "legacy-candidate"), "images": [_ref(image)],
            "instruction": "A coherent sequence."})
    run = next(run for run in app.runtime_store.list_runs(domain="comic", conversation_id=cid)
               if run.state.get("story_candidate"))
    candidate = copy.deepcopy(run.state["story_candidate"])
    if old_contract is None:
        candidate.pop("review_contract")
    else:
        candidate["review_contract"] = old_contract
    app.runtime_store.update_run(run.id, status=run.status,
        state={**run.state, "story_candidate": candidate}, current_node=run.current_node,
        error=run.error)
    recovered = app.comic_story_operation(cid, "adopt-candidate", {
        **_request(app, pid, cid, "restore-legacy"), "candidate_run_id": run.id})
    assert recovered["story"]["title"] == draft["title"]
    assert recovered["review"] is None and recovered["confirmation"] is None
    checked, calls = _review(app, pid, cid, monkeypatch, key="new-policy-review")
    assert len(calls) == 1 and not checked.get("review_reused")
    assert checked["review"]["status"] == "pass"
    assert checked["review"]["contract"] == "comic-story-3"
    confirmed = app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "new-confirm"))
    assert confirmed["confirmation"]["confirmed"]


def test_initial_generation_keeps_coordinated_execution_fields_from_the_model(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    skeleton = _draft(image, title="Keep the user's sequence title")
    skeleton["summary"] = "Raise the signal lamp, then call the waiting ship."
    skeleton["instruction"] = ""
    skeleton["shots"][0].update(
        action="Raise the lamp and call the ship.", camera="Start close, then reveal the coast.",
        duration_seconds=4.0, transition="Move to the opposite view.")
    generated = copy.deepcopy(skeleton)
    generated.update(title="An unsolicited model title", summary="An unsolicited summary")
    generated["shots"][0].update(
        action="Raise the lamp over two seconds, then call the waiting ship.",
        camera="Pull back from the lamp to reveal the keeper and the coast.",
        duration_seconds=8.0, transition="Hold the wide view to end the sequence.")
    vision_calls, review_calls = [], []

    def vision(messages):
        vision_calls.append(messages)
        return json.dumps(generated)

    def review(messages):
        review_calls.append(messages)
        draft = json.loads(messages[1]["content"])["draft"]
        assert draft["shots"] == generated["shots"], "Merging must not restore inconsistent fields"
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "coordinated-initial"), "images": [_ref(image)],
        "draft": skeleton, "instruction": ""})
    assert result["story"]["title"] == skeleton["title"]
    assert result["story"]["summary"] == skeleton["summary"]
    assert result["story"]["shots"] == generated["shots"]
    assert result["review"]["status"] == "pass"
    assert len(vision_calls) == len(review_calls) == 1


def test_action_finding_can_repair_timing_and_camera_without_changing_another_shot(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, first = _setup(app)
    second = _upload(app, pid, cid, content=_png(51), filename="next-moment.png")
    _select(app, cid, second)
    skeleton = _draft(first, second)
    skeleton["shots"][0]["action"] = "Lift the beacon, speak the required line, and hold it aloft."
    fixed = copy.deepcopy(skeleton)
    fixed["shots"][0].update(
        action="Lift the beacon, say the required line, then hold it aloft for two seconds.",
        camera="Pull back slowly over the complete action.", duration_seconds=9.0,
        transition="Hold before a clean cut to the next moment.")
    fixed["shots"][1]["action"] = "UNRELATED_SHOT_REWRITE_MUST_NOT_BE_APPLIED"
    visions = iter([skeleton, fixed])
    review_calls = []

    def review(messages):
        review_calls.append(messages)
        draft = json.loads(messages[1]["content"])["draft"]
        if len(review_calls) == 1:
            return json.dumps({"status": "needs_revision", "findings": [{
                "severity": "error", "shot_id": "story-shot-1", "field": "action",
                "code": "duration_conflict",
                "message": "The action needs time to complete the line.",
                "evidence": draft["shots"][0]["action"],
                "suggestion": "Coordinate the action, camera movement and timing."}]})
        assert draft["shots"][0] == fixed["shots"][0]
        assert draft["shots"][1] == skeleton["shots"][1]
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(next(visions)))
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "action-timing-group"),
        "images": [_ref(first), _ref(second)], "draft": skeleton,
        "instruction": "Keep the required line and give each action enough time."})
    assert result["review"]["status"] == "pass"
    assert result["story"]["shots"][0]["duration_seconds"] == 9.0
    assert result["story"]["shots"][1] == skeleton["shots"][1]
    assert len(review_calls) == 2


def test_same_image_repeated_shots_match_stable_ids_when_model_order_is_reversed(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    skeleton = _draft(image, image)
    for shot in skeleton["shots"]:
        shot.update(action="", camera="", duration_seconds=None, transition="")
    generated = copy.deepcopy(skeleton)
    generated["shots"][0].update(
        action="First the keeper finds the signal lamp.", camera="Push in toward the lamp.",
        duration_seconds=3.5, transition="Cut to the keeper lifting it.")
    generated["shots"][1].update(
        action="Then the keeper raises the lamp and calls the ship.", camera="Pull back slowly.",
        duration_seconds=7.0, transition="Hold the final view.")
    expected = copy.deepcopy(generated["shots"])
    generated["shots"].reverse()
    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(generated))
    monkeypatch.setattr(app, "_comic_constraint_model",
                        lambda _: json.dumps({"status": "pass", "findings": []}))
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "same-image-stable-shots"), "images": [_ref(image)],
        "draft": skeleton, "instruction": "Find the lamp first, then lift it and call the ship."})
    assert result["story"]["shots"] == expected
    assert len(result["story"]["fixed_images"]) == 1


def test_original_user_intent_reaches_generation_repair_and_internal_review_only(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    skeleton = _draft(image)
    skeleton["instruction"] = ""
    skeleton["shots"][0]["action"] = "Lift the beacon and say ORIGINAL_SIGNAL_LINE_74."
    fixed = copy.deepcopy(skeleton)
    fixed["shots"][0].update(
        action="Lift the beacon, then clearly say ORIGINAL_SIGNAL_LINE_74.",
        duration_seconds=8.0)
    visions = iter([skeleton, fixed])
    vision_inputs, review_inputs = [], []

    def vision(messages):
        payload = json.loads(messages[-1]["content"][0]["text"])
        vision_inputs.append(payload)
        assert payload["user_intent"] == skeleton
        return json.dumps(next(visions))

    def review(messages):
        payload = json.loads(messages[1]["content"])
        review_inputs.append(payload)
        if len(review_inputs) <= 2:
            assert payload["user_intent"] == skeleton
        else:
            assert payload.get("user_intent") is None
            assert "ORIGINAL_SIGNAL_LINE_74" not in json.dumps(messages)
            assert "NEW_MANUAL_SIGNAL_LINE_85" in payload["draft"]["shots"][0]["action"]
        if len(review_inputs) == 1:
            return json.dumps({"status": "needs_revision", "findings": [{
                "severity": "error", "shot_id": "story-shot-1", "field": "action",
                "code": "duration_conflict", "message": "The requested line needs more time.",
                "evidence": skeleton["shots"][0]["action"],
                "suggestion": "Preserve the line and extend the action timing."}]})
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    generated = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "original-intent"), "images": [_ref(image)],
        "draft": skeleton, "instruction": ""})
    assert generated["review"]["status"] == "pass"
    assert len(vision_inputs) == len(review_inputs) == 2
    assert review_inputs[1]["draft"]["shots"][0]["action"] != skeleton["shots"][0]["action"]
    manually_edited = _story_draft(generated["story"])
    manually_edited["shots"][0]["action"] = "Lower the beacon and say NEW_MANUAL_SIGNAL_LINE_85."
    _save(app, pid, cid, manually_edited, "replace-user-intent")
    result = app.comic_story_operation(cid, "review", _request(app, pid, cid, "manual-intent"))
    assert result["review"]["status"] == "pass"
    assert len(review_inputs) == 3


def test_model_inputs_separate_static_negative_prompt_from_explicit_persistent_requirements(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    negative = "Do not show the face or rotate toward the viewer in this still image."
    persistent = "The silver lighthouse badge remains visible throughout the entire video."
    source_constraints = [
        {"constraint_id": "source-starting-view", "scope": "shot",
         "requirement": "The keeper starts with their back toward the camera.",
         "source_quote": "Start with the keeper's back toward the camera.",
         "origin": "user_request", "trust": "source_verified", "status": "active"},
        {"constraint_id": "source-continuous-badge", "scope": "identity",
         "requirement": persistent, "source_quote": persistent,
         "origin": "user_request", "trust": "source_verified", "status": "active"},
    ]
    original_sources = app.comic_stories._source_contexts

    def sources(images, *, previous=None):
        return [{**source, "constraints": copy.deepcopy(source_constraints),
                 "negative_prompt": negative}
                for source in original_sources(images, previous=previous)]

    monkeypatch.setattr(app.comic_stories, "_source_contexts", sources)
    skeleton = _draft(image)
    skeleton["shots"][0].update(
        action="Show the badge, then lift the signal lamp.",
        camera="Begin behind the keeper, then rotate to a front three-quarter view.")
    fixed = copy.deepcopy(skeleton)
    fixed["shots"][0]["duration_seconds"] = 8.0
    visions = iter([skeleton, fixed])
    model_inputs = []

    def check_sources(payload):
        model_inputs.append(payload)
        assert len(payload["image_sources"]) == 1
        source = payload["image_sources"][0]
        assert source["reference_role"] == "initial_frame"
        assert source["constraints"] == source_constraints
        assert "negative_prompt" not in source
        assert negative not in json.dumps(payload)
        assert persistent in json.dumps(payload)

    def vision(messages):
        check_sources(json.loads(messages[-1]["content"][0]["text"]))
        return json.dumps(next(visions))

    def review(messages):
        payload = json.loads(messages[1]["content"])
        check_sources(payload)
        if payload["draft"]["shots"][0]["duration_seconds"] == 4.0:
            return json.dumps({"status": "needs_revision", "findings": [{
                "severity": "error", "shot_id": "story-shot-1", "field": "duration_seconds",
                "code": "duration_conflict", "message": "Give the lamp lift and camera orbit time.",
                "evidence": "4", "suggestion": "Extend the duration while retaining the badge."}]})
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "source-roles"), "images": [_ref(image)],
        "draft": skeleton, "instruction": "Start behind the keeper, then turn to the front."})
    assert result["review"]["status"] == "pass"
    assert len(model_inputs) == 4, "Generation, repair and both reviews use the same source roles"
    assert result["story"]["source_contexts"][0]["negative_prompt"] == negative
    assert result["story"]["source_contexts"][0]["constraints"] == source_constraints


@pytest.mark.parametrize("code", ["continuity", "transition_conflict"])
@pytest.mark.parametrize("severity", ["error", "warning"])
def test_only_real_transition_errors_authorize_repairing_the_next_shot(
    story_app, monkeypatch, code, severity,
):
    app = story_app
    pid, cid, image = _setup(app)
    skeleton = _draft(image, image, image)
    skeleton["shots"][0]["transition"] = "Cut before the hand reaches the signal lever."
    fixed = copy.deepcopy(skeleton)
    for index, shot in enumerate(fixed["shots"], 1):
        shot.update(action=f"Complete coordinated step {index}.",
                    camera=f"Follow the movement in step {index}.",
                    duration_seconds=float(index + 6), transition=f"Close step {index} smoothly.")
    visions = iter([skeleton, fixed])
    vision_calls, review_calls = [], []
    editable = {"story-shot-1", "story-shot-2"} if severity == "error" else {"story-shot-3"}

    def vision(messages):
        payload = json.loads(messages[-1]["content"][0]["text"])
        vision_calls.append(payload)
        if len(vision_calls) == 2:
            scope = payload["repair_scope"]
            assert set(scope["shots"]) == editable
            assert all(set(fields) == {"action", "camera", "duration_seconds", "transition"}
                       for fields in scope["shots"].values())
            assert scope["story"] == []
        return json.dumps(next(visions))

    def review(messages):
        payload = json.loads(messages[1]["content"])
        review_calls.append(payload)
        if len(review_calls) == 1:
            findings = [{
                "severity": severity, "shot_id": "story-shot-1", "field": "transition",
                "code": code, "message": "The lever movement must continue into the next shot.",
                "evidence": skeleton["shots"][0]["transition"],
                "suggestion": "Coordinate this ending with the next shot's starting action."}]
            if severity == "warning":
                findings.append({
                    "severity": "error", "shot_id": "story-shot-3", "field": "action",
                    "code": "duration_conflict", "message": "The final action needs more time.",
                    "evidence": skeleton["shots"][2]["action"],
                    "suggestion": "Coordinate the final action and its duration."})
            return json.dumps({"status": "needs_revision", "findings": findings})
        for index, shot in enumerate(payload["draft"]["shots"]):
            expected = fixed if shot["shot_id"] in editable else skeleton
            assert shot == expected["shots"][index]
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", vision)
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "boundary-authorization"), "images": [_ref(image)],
        "draft": skeleton, "instruction": "Keep the three moments in this order."})
    assert result["review"]["status"] == "pass"
    assert len(vision_calls) == len(review_calls) == 2
    assert [shot["image"] for shot in result["story"]["shots"]] == [_ref(image)] * 3


@pytest.mark.parametrize("swap_both", [False, True])
def test_initial_model_cannot_reuse_a_user_shot_id_for_another_images_execution_fields(
    story_app, monkeypatch, swap_both,
):
    app = story_app
    pid, cid, first = _setup(app)
    second = _upload(app, pid, cid, content=_png(32), filename="different-frame.png")
    _select(app, cid, second)
    skeleton = _draft(first, second)
    generated = copy.deepcopy(skeleton)
    generated["shots"][0].update(
        image=_ref(second), action="FOREIGN_IMAGE_FIELDS_MUST_NOT_BE_BORROWED",
        camera="A camera instruction for a different source.", duration_seconds=12.0,
        transition="A transition describing a different source.")
    if swap_both:
        generated["shots"][1].update(
            image=_ref(first), action="FOREIGN_IMAGE_FIELDS_MUST_NOT_BE_BORROWED",
            camera="Another mismatched camera.", duration_seconds=11.0,
            transition="Another mismatched ending.")
    else:
        generated["shots"][1]["camera"] = "A valid adjustment using the second shot's own image."
    seen = []

    def review(messages):
        draft = json.loads(messages[1]["content"])["draft"]
        seen.append(draft)
        assert draft["shots"][0] == skeleton["shots"][0]
        assert draft["shots"][1] == (skeleton if swap_both else generated)["shots"][1]
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(generated))
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "reject-cross-image-field-borrowing"),
        "images": [_ref(first), _ref(second)], "draft": skeleton,
        "instruction": "Keep each shot bound to its original image."})
    assert result["review"]["status"] == "pass"
    assert len(seen) == 1
    assert "FOREIGN_IMAGE_FIELDS_MUST_NOT_BE_BORROWED" not in json.dumps(result["story"])
    assert [shot["image"] for shot in result["story"]["shots"]] == [_ref(first), _ref(second)]


def test_missing_shots_explicitly_authorizes_creation_in_the_public_repair_scope():
    from kantoku.domains.comic.stories import ComicStoryService

    draft = ComicStoryDraft(title="Signal from the coast", summary="A keeper signals a ship.")
    findings = ComicStoryService._structural_findings(draft)
    assert len(findings) == 1 and findings[0]["code"] == "missing_shots"
    scope = ComicStoryService._repair_scope(draft, findings)
    assert scope["create_shots"] is True
    assert scope["shots"] == {} and scope["story"] == []
    advisory = [{**findings[0], "severity": "warning"}]
    assert ComicStoryService._repair_scope(draft, advisory)["create_shots"] is False


def test_repeated_image_does_not_lend_one_unmatched_model_shot_to_two_user_events(
    story_app, monkeypatch,
):
    app = story_app
    pid, cid, image = _setup(app)
    skeleton = _draft(image, image)
    skeleton["shots"][0]["action"] = "FIRST_USER_EVENT: find the signal lamp."
    skeleton["shots"][1]["action"] = "SECOND_USER_EVENT: lift the lamp and call the ship."
    skeleton["shots"][1]["duration_seconds"] = 7.0
    generated = _draft(image)
    generated["shots"][0].update(
        shot_id="model-lost-the-original-identities",
        action="AMBIGUOUS_MODEL_EVENT_MUST_NOT_REPLACE_EITHER_USER_EVENT",
        camera="An ambiguous camera for one of two repeated images.", duration_seconds=12.0,
        transition="An ending with no known corresponding user shot.")
    seen = []

    def review(messages):
        draft = json.loads(messages[1]["content"])["draft"]
        seen.append(draft)
        assert draft["shots"] == skeleton["shots"]
        return json.dumps({"status": "pass", "findings": []})

    monkeypatch.setattr(app, "_comic_image_vision_model", lambda _: json.dumps(generated))
    monkeypatch.setattr(app, "_comic_constraint_model", review)
    result = app.comic_story_operation(cid, "generate", {
        **_request(app, pid, cid, "ambiguous-duplicate-image"), "images": [_ref(image)],
        "draft": skeleton, "instruction": skeleton["instruction"]})
    assert result["review"]["status"] == "pass"
    assert len(seen) == 1
    assert result["story"]["shots"] == skeleton["shots"]
    assert "AMBIGUOUS_MODEL_EVENT" not in json.dumps(result["story"])


@pytest.mark.parametrize("lifecycle", ["trashed", "purged"])
def test_global_asset_lifecycle_preserves_story_approval_but_blocks_unavailable_exports(
    story_app, monkeypatch, lifecycle,
):
    """全局生命周期与领域整图删除不同：内容和旧交付保留，失效图不能再用于制作。"""
    app = story_app
    pid, cid, image = _setup(app)
    saved = _save(app, pid, cid, _draft(image))
    confirmed = _confirm(app, pid, cid, monkeypatch)
    delivered = app.comic_story_operation(cid, "exports", _request(app, pid, cid, "before-trash"))
    package = delivered["exports"][0]
    package_path = app.get_core_artifact_content(package["artifact_id"])
    package_bytes = package_path.read_bytes()
    artifact = app.runtime_store.get_artifact(image["artifact_id"])
    image_path = Path(artifact.location)
    metadata_before = copy.deepcopy(artifact.metadata)
    trashed = app.change_core_artifact(artifact.id, "trash", {
        "expected_lifecycle_version": artifact.lifecycle_version})
    if lifecycle == "purged":
        app.change_core_artifact(artifact.id, "purge", {
            "expected_lifecycle_version": trashed["lifecycle_version"]})
        assert not image_path.exists()
    else:
        assert image_path.is_file()
    monkeypatch.setattr(app, "_comic_constraint_model", _forbidden)
    monkeypatch.setattr(app, "_comic_image_vision_model", _forbidden)

    unavailable = app.get_comic_story(cid)
    assert unavailable["story"] == saved["story"]
    assert unavailable["confirmation"] == confirmed["confirmation"]
    assert {item["shot_id"] for item in unavailable["missing_images"]} == {"story-shot-1"}
    candidates = app.comic_story_images(cid)["images"]
    selected = next(item for item in candidates if item["image_id"] == image["image_id"])
    assert selected["available"] is False
    assert selected["selection"]["selected_version"] == 1
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "confirm", _request(app, pid, cid, "inactive-confirm"))
    with pytest.raises(ToolError):
        app.comic_story_operation(cid, "exports", _request(app, pid, cid, "inactive-export"))
    assert app.get_core_artifact_content(package["artifact_id"]).read_bytes() == package_bytes
    assert app.runtime_store.get_artifact(artifact.id).metadata == metadata_before

    current = app.runtime_store.get_artifact(artifact.id)
    if lifecycle == "trashed":
        app.change_core_artifact(artifact.id, "restore", {
            "expected_lifecycle_version": current.lifecycle_version})
        restored = app.get_comic_story(cid)
        assert restored["story"] == saved["story"]
        assert restored["confirmation"] == confirmed["confirmation"]
        assert restored["missing_images"] == []
        assert image_path.read_bytes() == _png()
        assert next(item for item in app.comic_story_images(cid)["images"]
                    if item["image_id"] == image["image_id"])["available"] is True
        exported = app.comic_story_operation(
            cid, "exports", _request(app, pid, cid, "restored-export"))
        assert any(item["story_version"] == saved["story"]["version"]
                   and item["artifact_id"] != package["artifact_id"]
                   for item in exported["exports"])
    else:
        with pytest.raises(ToolError):
            app.change_core_artifact(artifact.id, "restore", {
                "expected_lifecycle_version": current.lifecycle_version})
    assert app.get_core_artifact_content(package["artifact_id"]).read_bytes() == package_bytes


def test_family_delete_protects_parent_work_story_without_locking_its_old_references_forever(
    story_app,
):
    app = story_app
    parent = app.comic_workspace.create({"request_id": "protected-work", "title": "Parent story"})
    child = app.comic_workspace.create_task(parent["work_id"], {
        "request_id": "protected-task", "expected_version": parent["version"],
        "title": "Child image source", "requirement": "A frame owned by this child task."})
    pid, cid = child["task_id"], child["conversation_id"]
    first = _upload(app, pid, cid, filename="first-source.png")
    replacement = _upload(app, pid, cid, content=_png(93), filename="replacement-source.png")
    for image in (first, replacement):
        _select(app, cid, image)

    def write_story(image, expected_story_version, request_id):
        return app.comic_work_story(parent["work_id"], "save", {
            "request_id": request_id,
            "expected_project_version": app.comic_workspace.get(parent["work_id"])["version"],
            "expected_story_version": expected_story_version, "draft": _draft(image)})

    original = write_story(first, 0, "parent-story-v1")
    assert original["story"]["anchor_project_id"] == parent["work_id"] != pid
    before = app.comic_projects.get(pid).project.current_version
    with pytest.raises(ToolError, match="故事"):
        app.delete_comic_image(pid, first["image_id"],
                               _media_request(app, pid, cid, "reject-child-deletion"))
    assert app.comic_projects.get(pid).project.current_version == before
    assert app.runtime_store.get_artifact(first["artifact_id"]).lifecycle_state == \
        ArtifactLifecycle.ACTIVE
    assert app.comic_work_story(parent["work_id"])["story"] == original["story"]

    current = write_story(replacement, 1, "parent-story-v2")
    app.delete_comic_image(pid, first["image_id"],
                           _media_request(app, pid, cid, "delete-unfixed-history-source"))
    live = app.comic_work_story(parent["work_id"])
    assert live["story"] == current["story"] and live["missing_images"] == []
    historical = app.comic_work_story(parent["work_id"], version=1)
    assert historical["story"] == original["story"]
    assert {item["shot_id"] for item in historical["missing_images"]} == {"story-shot-1"}
    assert app.runtime_store.get_artifact(first["artifact_id"]).lifecycle_state == \
        ArtifactLifecycle.TRASHED
    assert app.runtime_store.get_artifact(replacement["artifact_id"]).lifecycle_state == \
        ArtifactLifecycle.ACTIVE
