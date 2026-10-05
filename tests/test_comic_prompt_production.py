"""Module 2 offline regression: full external Prompt, provenance, editing and upload."""

import base64
import json
import struct
import zlib
from http.client import HTTPConnection
from pathlib import Path

import pytest
from test_comic_prompts import _run, _setup
from test_home_quick_domain import _workspace_production_request
from test_home_quick_domain import app as app_fixture
from test_home_quick_domain import production as production_fixture
from test_web_studio import server as server_fixture

from kantoku.config import ToolError
from kantoku.core import budget
from kantoku.core.runtime.models import ArtifactType
from kantoku.domains.comic.models import ComicShotDraft
from kantoku.domains.comic.prompts import (
    CONTEXT_VERSION,
    PROMPT_SECTIONS,
    compiler_for_model,
)
from kantoku.shells import web_studio

app = app_fixture
production = production_fixture
server = server_fixture


def _sections():
    return {
        "director_summary": "保持当前导演执行方案",
        "character_context": "东方仙侠少女阿青，黑发，青色披风，固定相同身份与服装",
        "world_context": "东方仙侠世界，雨夜竹林，山石与竹叶有明确空间关系",
        "style_context": "东方幻想电影，真实摄影质感，冷色环境与暖色轮廓分离",
        "shot_context": "少女拔剑，远景，低机位，人物位于左侧三分之一，月光右上方照射",
        "negative_prompt": "禁止现代建筑、服装漂移、额外人物、文字水印",
    }


def test_external_image_http_import_accepts_normal_file_size_and_lists_prompt_assets(
    app,
    production,
    server,
    monkeypatch,
    tmp_path,
):
    request, shot, prompt = _external(app, monkeypatch)
    monkeypatch.setattr(
        web_studio.get_settings().image, "output_dir", tmp_path / "images", raising=False
    )
    small = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1sAAAAASUVORK5CYII="
    )
    payload = b"QA\0" + b"a" * 60000
    chunk = (
        struct.pack(">I", len(payload))
        + b"tEXt"
        + payload
        + struct.pack(">I", zlib.crc32(b"tEXt" + payload))
    )
    content = small[:-12] + chunk + small[-12:]
    body = json.dumps(
        {
            "expected_project_version": app.comic_projects.get(
                request["production_project_id"]
            ).project.current_version,
            "expected_shot_version": shot["version"],
            "expected_prompt_version": prompt["version"],
            "conversation_id": request["conversation_id"],
            "filename": "qa-user-image.png",
            "data_url": "data:image/png;base64," + base64.b64encode(content).decode(),
        }
    )
    assert len(body) > 65536
    connection = HTTPConnection("127.0.0.1", server, timeout=10)
    try:
        connection.request(
            "POST",
            f"/api/comic/shots/{shot['shot_id']}/external-image",
            body=body,
            headers={"X-Studio-Token": app.token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 201, result
        artifact = app.runtime_store.get_artifact(result["artifact_id"])
        assert Path(artifact.location).read_bytes() == content
        connection.request(
            "GET", f"/api/comic/projects/{request['production_project_id']}/prompts",
            headers={"X-Studio-Token": app.token},
        )
        response = connection.getresponse()
        assets = json.loads(response.read())["prompts"]
        assert response.status == 200 and assets[0]["final_prompt"] == prompt["final_prompt"]
        assert assets[0]["external_image"]["artifact_id"] == artifact.id
        assert assets[0]["shot_sequence_number"] == 1
    finally:
        connection.close()


def _compile(prompts, shot_id, call=None):
    snapshot, director, board, shot, assets = prompts.source(shot_id)
    return compiler_for_model("external").compile(
        snapshot=snapshot,
        director=director,
        storyboard=board,
        shot=shot,
        assets=assets,
        model_target="external",
        complete_prompt=True,
        reused_context=prompts.reused_context(shot, director),
        model_call=call or (lambda _: json.dumps(_sections(), ensure_ascii=False)),
    )


def _save(prompts, shot_id, draft):
    snapshot, _director, _board, shot, _assets = prompts.source(shot_id)
    return prompts.save(
        shot_id,
        draft,
        expected_project_version=snapshot.project.current_version,
        expected_shot_version=shot.version,
        model_target="external",
        compiler_version="comic-prompt-1",
        run_id=_run(prompts.runtime),
    )


def test_complete_prompt_keeps_pinned_facts_and_excludes_other_character(tmp_path):
    runtime, _projects, _assets, _boards, prompts, shot, unrelated = _setup(tmp_path / "db")
    requests = []

    def call(messages):
        requests.append(messages)
        return json.dumps(_sections(), ensure_ascii=False)

    draft = _compile(prompts, shot.shot_id, call)
    record = _save(prompts, shot.shot_id, draft)
    full = prompts.payload(record)["final_prompt"]
    for heading in [*PROMPT_SECTIONS.values(), "Negative Constraint / 禁止内容"]:
        assert full.count(heading) == 1
    for fact in ("阿青", "黑发少女", "青色披风", "雨夜竹林", "电影", "右上方", "文字水印"):
        assert fact in full
    assert unrelated.asset_id not in requests[0][1]["content"] and "银发少年" not in full
    sources = record.context_sources
    assert sources["source_type"] == "model_choice" and sources["model_request_id"]
    assert sources["user_facts"]["sha256"] and len(sources["asset_facts"]) == 2
    assert all(item["sha256"] and item["version"] == 1 for item in sources["asset_facts"])
    assert runtime.get_artifact(record.artifact_id).metadata["final_prompt"] == full
    assert not runtime.list_artifacts(type=ArtifactType.IMAGE)


def test_same_character_bibles_reuse_exact_prompt_revision_not_new_random_choice(tmp_path):
    _runtime, projects, _assets, boards, prompts, shot, _unrelated = _setup(tmp_path / "db")
    first = _save(prompts, shot.shot_id, _compile(prompts, shot.shot_id))
    board = boards.get(shot.storyboard_id)
    next_shot = boards.add_shot(
        board.storyboard_id,
        ComicShotDraft.model_validate(
            shot.model_dump(
                include={
                    "purpose",
                    "subject",
                    "environment",
                    "character_asset_versions",
                    "scene_asset_versions",
                }
            )
        ),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_storyboard_version=board.version,
    )
    next_shot = boards.edit_shot(
        next_shot.shot_id,
        ComicShotDraft.model_validate(
            next_shot.model_dump(
                include={
                    "purpose",
                    "environment",
                    "character_asset_versions",
                    "scene_asset_versions",
                }
            )
            | {"subject": "阿青右肩与青色披风的轮廓"}
        ),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_version=next_shot.version,
        status=next_shot.status,
    )
    changed = _sections() | {
        "character_context": "模型试图换成红发",
        "style_context": "模型随机选新风格",
    }
    draft = _compile(prompts, next_shot.shot_id, lambda _: json.dumps(changed, ensure_ascii=False))
    assert draft.character_context == first.character_context
    assert draft.style_context == first.style_context
    assert (
        draft.context_sources["reused_context"]["character_context"]["artifact_id"]
        == first.artifact_id
    )
    # Changed identity cannot inherit the previous character Bible even if the old Prompt exists.
    edited = boards.edit_shot(
        next_shot.shot_id,
        ComicShotDraft(purpose="另一角色", subject="阿白", environment="雪原"),
        expected_project_version=projects.get(shot.project_id).project.current_version,
        expected_version=next_shot.version,
        status=next_shot.status,
    )
    assert "character_context" not in prompts.reused_context(
        edited, projects.get_director(shot.project_id)
    )


@pytest.mark.parametrize(
    "missing", ["character_context", "world_context", "negative_prompt", "director_summary"]
)
def test_incomplete_model_output_never_creates_complete_artifact(tmp_path, missing):
    runtime, _p, _a, _b, prompts, shot, _u = _setup(tmp_path / "db")
    result = _sections()
    result.pop(missing)
    with pytest.raises(ToolError, match="缺少"):
        _compile(prompts, shot.shot_id, lambda _: json.dumps(result, ensure_ascii=False))
    assert not runtime.list_artifacts(type=ArtifactType.PROMPT)


def _external(app, monkeypatch):
    request = _workspace_production_request(app)
    project_id = request["production_project_id"]
    board = app.create_comic_storyboard(
        project_id,
        {
            "expected_project_version": request["expected_project_version"],
            "generate": True,
        },
    )
    shot = board["shots"][0]
    monkeypatch.setattr(
        app, "_comic_storyboard_model", lambda _: json.dumps(_sections(), ensure_ascii=False)
    )
    prompt = app.compile_comic_prompt(
        shot["shot_id"],
        {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "expected_shot_version": shot["version"],
            "image_mode": "external",
            "complete_prompt": True,
            "conversation_id": request["conversation_id"],
        },
    )
    return request, shot, prompt


def test_manual_prompt_edit_keeps_sources_and_invalidates_confirmation(
    app, production, monkeypatch
):
    request, shot, prompt = _external(app, monkeypatch)
    project_id = request["production_project_id"]

    def current_version():
        return app.comic_projects.get(project_id).project.current_version

    confirmed = app.confirm_comic_prompt(
        shot["shot_id"],
        {
            "expected_project_version": current_version(),
            "expected_version": prompt["version"],
            "conversation_id": request["conversation_id"],
        },
    )
    assert confirmed["user_confirmed"]
    edited = app.edit_comic_prompt(
        shot["shot_id"],
        {
            "expected_project_version": current_version(),
            "expected_version": prompt["version"],
            "draft": {
                "director_summary": prompt["director_summary"],
                "positive_prompt": prompt["positive_prompt"].replace(
                    "左侧三分之一", "右侧三分之一"
                ),
                "negative_prompt": prompt["negative_prompt"],
                "context_sources": {"source_type": "user_fact", "trust_status": "confirmed_fact"},
            },
        },
    )
    assert edited["context_version"] == CONTEXT_VERSION and not edited["user_confirmed"]
    assert "右侧三分之一" in edited["shot_context"]
    assert edited["context_sources"]["source_type"] == "manual_edit"
    fields = edited["context_sources"]["field_provenance"]
    assert fields["shot_context"]["source_type"] == "manual_edit"
    assert (
        fields["character_context"]
        == prompt["context_sources"]["field_provenance"]["character_context"]
    )
    assert edited["context_sources"]["parent_artifact_id"] == prompt["artifact_id"]
    assert (
        edited["context_sources"]["director_sources"]
        == prompt["context_sources"]["director_sources"]
    )
    assert app.comic_prompts.list(project_id)[0].version == 2
    assert len(app.comic_prompts.versions(shot["shot_id"])) == 2
    with pytest.raises(ToolError, match="版本已变化"):
        app.confirm_comic_prompt(
            shot["shot_id"],
            {
                "expected_project_version": current_version(),
                "expected_version": prompt["version"],
                "conversation_id": request["conversation_id"],
            },
        )
    assert not [r for r in app.runtime_store.list_runs() if r.workflow == "comic.production.v1"]
    assert not [a for a in app.runtime_store.list_approvals() if a.node_id == "cost_approval"]


def test_prompt_uses_current_shot_camera_and_keeps_director_light(app, production, monkeypatch):
    request, raw_shot, _prompt = _external(app, monkeypatch)
    original = app.comic_storyboards.get_shot(raw_shot["shot_id"])
    shot = app.comic_storyboards.edit_shot(
        original.shot_id,
        ComicShotDraft(
            purpose=original.purpose,
            subject=original.subject,
            environment=original.environment,
            shot_size="中景",
            camera_angle="侧面平视",
        ),
        expected_project_version=app.comic_projects.get(
            original.project_id
        ).project.current_version,
        expected_version=original.version,
        status=original.status,
    )
    draft = _compile(
        app.comic_prompts,
        shot.shot_id,
        lambda _: json.dumps(
            _sections() | {"shot_context": "中景、侧面平视，沿用导演的主光方向"}, ensure_ascii=False
        ),
    )
    assert "shot_size=中景" in draft.shot_context and "camera_angle=侧面平视" in draft.shot_context
    director = app.comic_projects.get_director(request["production_project_id"])
    assert f"light_direction={director.cinematography.light_direction}" in draft.shot_context


def test_external_upload_registers_core_artifact_without_provider_budget_or_workflow(
    app,
    production,
    monkeypatch,
    tmp_path,
):
    request, shot, prompt = _external(app, monkeypatch)
    project_id = request["production_project_id"]
    monkeypatch.setattr(
        web_studio.get_settings().image, "output_dir", tmp_path / "images", raising=False
    )

    def forbidden(*_a, **_k):
        pytest.fail("External Prompt pipeline must not invoke image production or billing")

    for name in ("submit", "query", "check_access", "validate_request"):
        monkeypatch.setattr(app.image_service.provider, name, forbidden)
    for target in (
        "kantoku.core.budget.reserve",
        "kantoku.core.budget.settle",
        "kantoku.tools.image_gen.gen_image",
        "kantoku.domains.comic.services.create_task",
    ):
        monkeypatch.setattr(target, forbidden)
    ledger = budget.list_ledger(project=project_id)
    content = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1sAAAAASUVORK5CYII="
    )
    body = {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_shot_version": shot["version"],
        "expected_prompt_version": prompt["version"],
        "conversation_id": request["conversation_id"],
        "filename": "external-user-image.png",
        "data_url": "data:image/png;base64," + base64.b64encode(content).decode(),
    }
    result = app.import_comic_external_image(shot["shot_id"], body)
    duplicate = app.import_comic_external_image(shot["shot_id"], body)
    assert duplicate["artifact_id"] == result["artifact_id"]
    artifact = app.runtime_store.get_artifact(result["artifact_id"])
    assert Path(artifact.location).read_bytes() == content
    assert artifact.metadata["creation_mode"] == "external_upload"
    assert artifact.conversation_id == request["conversation_id"]
    assert artifact.metadata["prompt_artifact_id"] == prompt["artifact_id"]
    assert (
        app.comic_prompts.payload(app.comic_prompts.get(shot["shot_id"]))["external_image"][
            "status"
        ]
        == "image_uploaded"
    )
    assert app.comic_storyboards.get_shot(shot["shot_id"]).version == shot["version"]
    assert budget.list_ledger(project=project_id) == ledger
    assert len(app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)) == 1
    assert not [r for r in app.runtime_store.list_runs() if r.workflow == "comic.production.v1"]
    with pytest.raises(ToolError, match="文件内容"):
        app.import_comic_external_image(
            shot["shot_id"],
            {
                **body,
                "data_url": "data:image/png;base64," + base64.b64encode(b"fake image").decode(),
            },
        )
    failed = next(r for r in app.runtime_store.list_runs() if r.status.value == "failed")
    assert failed.state["error_id"] and failed.state["trace_id"]
    with pytest.raises(ToolError, match="版本已变化"):
        app.import_comic_external_image(shot["shot_id"], {**body, "expected_prompt_version": 99})
