"""Workspace-only production diagnostics; no live or paid model calls."""

import json

import pytest
from loguru import logger
from test_home_quick_domain import _workspace_production_request
from test_home_quick_domain import app as app_fixture
from test_home_quick_domain import production as production_fixture

from kantoku.config import ToolError
from kantoku.core import budget
from kantoku.core.runtime.models import ArtifactType
from kantoku.domains.comic.critic import require_approved_director

app = app_fixture
production = production_fixture


def test_selected_second_shot_reaches_provider_and_records_artifact(app, production):
    request = _workspace_production_request(app, creative_request="生成一个东方修仙少女图片")
    project_id = request["production_project_id"]
    board = app.create_comic_storyboard(project_id, {
        "expected_project_version": request["expected_project_version"], "generate": True,
        "task": "当前作品的一个关键画面",
    })["storyboard"]
    shot = app.create_comic_shot(board["storyboard_id"], {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_storyboard_version": board["version"],
        "shot": {"purpose": "角色细节", "subject": "东方修仙少女", "action": "站立",
                  "environment": "竹林"},
    })
    run = app.create_core_run({"domain": "comic", "state": {
        **request, "shot_id": shot["shot_id"], "shot_version": shot["version"],
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
    }})
    assert run["status"] == "completed"
    assert run["state"]["shot_no"] == 2
    assert run["state"]["quick_creation"]["shot_id"] == shot["shot_id"]
    events = [e.payload for e in app.runtime_store.list_events(run["id"])]
    started = next(e for e in events if e.get("kind") == "COMIC_IMAGE_GENERATION_STARTED")
    finished = next(e for e in events if e.get("kind") == "COMIC_IMAGE_GENERATION_COMPLETED")
    assert started["shot_id"] == shot["shot_id"] and started["prompt_hash"]
    assert finished["artifact_id"] == run["state"]["image_artifact_id"]
    assert finished["actual_fen"] == 30 and finished["duration_seconds"] >= 0
    artifact = app.runtime_store.get_artifact(finished["artifact_id"])
    assert artifact.metadata["shot_id"] == shot["shot_id"]
    assert artifact.metadata["prompt_version"] == run["state"]["quick_creation"]["prompt_version"]
    assert not app.runtime_store.approval_for_node(run["id"], "cost_approval")


@pytest.mark.parametrize("stage", ["storyboard", "prompt", "archive"])
def test_workspace_failure_preserves_exact_stage(app, production, monkeypatch, stage):
    request = _workspace_production_request(app)

    def fail(*args, **kwargs):
        raise ToolError(f"offline {stage} failure")

    if stage == "archive":
        monkeypatch.setattr("kantoku.domains.comic.services.StudioComicServices.archive", fail)
    else:
        monkeypatch.setattr(app, "create_comic_storyboard" if stage == "storyboard"
                            else "compile_comic_prompt", fail)
    run = app.create_core_run({"domain": "comic", "state": request})
    assert run["status"] == "failed" and run["current_node"] == stage
    failure = next(e.payload for e in app.runtime_store.list_events(run["id"])
                   if e.payload.get("kind") == "COMIC_IMAGE_GENERATION_FAILED")
    assert failure["stage"] == stage and failure["error_id"]
    assert failure["trace_id"] == run["state"]["trace_id"]
    assert failure["actual_fen"] == (30 if stage == "archive" else None)


def test_provider_403_is_not_reported_as_storyboard_or_approval_failure(
    app, production, monkeypatch,
):
    request = _workspace_production_request(app)

    def denied(**kwargs):
        error = ToolError("AccessDenied.Unpurchased")
        error.status_code = 403
        raise error

    monkeypatch.setattr(app.image_service.provider, "submit", denied)
    run = app.create_core_run({"domain": "comic", "state": request})
    assert run["status"] == "failed" and run["current_node"] == "generate"
    failure = next(e.payload for e in app.runtime_store.list_events(run["id"])
                   if e.payload.get("kind") == "COMIC_IMAGE_GENERATION_FAILED")
    assert failure["stage"] == "generate" and failure["actual_fen"] == 0
    assert "AccessDenied.Unpurchased" in failure["provider_response"]
    assert failure["error_id"] == budget.load_generation_result(
        run["state"]["request_id"]).error_id
    assert run["image_execution"]["can_regenerate"]


def test_workspace_defaults_to_current_director_board_and_keeps_history(
    app, production, monkeypatch,
):
    request = _workspace_production_request(app)
    project_id = request["production_project_id"]

    def create_board():
        return app.create_comic_storyboard(project_id, {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "generate": True, "task": "当前导演方案的关键画面",
        })

    historical = create_board()
    def semantic_change(messages):
        return ('{"edit_scopes":["character_context","world_context"]}'
                if "edit_scopes" in messages[0]["content"] else
                '{"new_creative_direction":false,"reason":"离线同一方案修改"}')
    monkeypatch.setattr(app, "_comic_intent_model", semantic_change)
    spec = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "conversation_id": request["conversation_id"], "creation_mode": "professional",
        "task": "重新设计：中式修仙少女站在竹林",
    })["director_spec"]
    app.confirm_comic_director(project_id, {
        "version": spec["version"],
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
    })
    current = create_board()
    listed = app.list_comic_storyboards(project_id)["storyboards"]
    assert listed[0]["storyboard_id"] == current["storyboard"]["storyboard_id"]
    assert listed[1]["storyboard_id"] == historical["storyboard"]["storyboard_id"]
    with pytest.raises(ToolError, match="导演方案已变化"):
        app.comic_prompts.source(historical["shots"][0]["shot_id"])
    app.comic_prompts.source(current["shots"][0]["shot_id"])


@pytest.mark.parametrize("critic_output", ["normal", "repaired", "unavailable"])
def test_professional_director_confirm_storyboard_and_selected_image(
    app, production, monkeypatch, critic_output,
):
    original = app._comic_director_model
    reviews = []

    def model(messages):
        response = original(messages)
        if '"title": "SemanticReview"' in messages[0]["content"]:
            reviews.append(messages)
            if (critic_output == "unavailable"
                    or (critic_output == "repaired" and len(reviews) == 1)):
                payload = json.loads(response)
                payload.pop("confidence")
                return json.dumps(payload)
        return response

    monkeypatch.setattr(app, "_comic_director_model", model)
    request = _workspace_production_request(app, confirm=False)
    project_id = request["production_project_id"]
    spec = app.comic_projects.get_director(project_id)
    assert len(reviews) == (1 if critic_output == "normal" else 2)
    director_run = next(run for run in app.runtime_store.list_runs()
                        if run.workflow == "comic.director")
    if critic_output == "unavailable":
        assert spec.critic_status == "unavailable" and spec.critic_result is None
        assert director_run.status.value == "waiting"
        events = [e.payload.get("director_event")
                  for e in app.runtime_store.list_events(director_run.id)]
        assert "critic_unavailable" in events and "node_warning" in events
        with pytest.raises(ToolError):
            require_approved_director(spec, allow_advisory=True)
    else:
        assert spec.critic_result.verdict == "pass"
    with pytest.raises(ToolError, match="确认|尚未通过"):
        app.comic_projects.require_confirmed_director(spec)
    if critic_output == "unavailable":
        # This legacy direct-project entry has no workspace_work_id recovery policy.
        # An unavailable first-generation validator must not authorize production.
        assert director_run.state["generation_incomplete"] is True
        assert not app.get_comic_director(project_id)["validation"]["can_confirm"]
        calls = list(production)
        with pytest.raises(ToolError, match="内部验证"):
            app.confirm_comic_director(project_id, {
                "version": spec.version,
                "expected_project_version": request["expected_project_version"],
            })
        assert production == calls
        assert not app.comic_projects.human_director_confirmed(spec)
        assert not app.runtime_store.list_approvals()
        assert not app.list_comic_storyboards(project_id)["storyboards"]
        assert not app.runtime_store.list_artifacts(type=ArtifactType.PROMPT)
        assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)
        assert not budget.list_ledger(project=project_id)
        return
    confirmed = app.confirm_comic_director(project_id, {
        "version": spec.version, "expected_project_version": request["expected_project_version"],
    })
    assert confirmed["user_confirmed"] and confirmed["approval"]["status"] == "approved"
    board = app.create_comic_storyboard(project_id, {
        "expected_project_version": request["expected_project_version"],
        "generate": True, "task": "当前导演方案分镜",
    })
    assert not [run for run in app.runtime_store.list_runs()
                if run.workflow == "comic.production.v1"]
    shot = board["shots"][0]
    calls = list(production)
    run = app.create_core_run({"domain": "comic", "state": {
        **request, "shot_id": shot["shot_id"], "shot_version": shot["version"],
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
    }})
    assert run["status"] == "completed"
    assert production[len(calls):] == ["prompt"]  # no repeated Director or Storyboard
    assert not [item for item in app.runtime_store.list_approvals() if item.run_id == run["id"]]
    artifact = app.runtime_store.get_artifact(run["state"]["image_artifact_id"])
    assert artifact.metadata["shot_id"] == shot["shot_id"]
    assert app.comic_projects.get_director(project_id).critic_result == spec.critic_result


def test_failed_shot_new_attempt_succeeds_and_cost_error_logs_are_complete(
    app, production, monkeypatch,
):
    request = _workspace_production_request(app)
    submit = app.image_service.provider.submit
    attempts = []

    def fail_once(**kwargs):
        attempts.append(kwargs["client_request_id"])
        if len(attempts) == 1:
            error = ToolError("explicit offline denial")
            error.status_code = 403
            error.provider_error_code = "AccessDenied.Unpurchased"
            error.provider_error_message = "offline denied api_key=sk-testsecret12345"
            raise error
        return submit(**kwargs)

    monkeypatch.setattr(app.image_service.provider, "submit", fail_once)
    lines = []
    sink = logger.add(lambda message: lines.append(str(message)), format="{message}")
    try:
        failed = app.create_core_run({"domain": "comic", "state": request})
        assert failed["status"] == "failed" and failed["image_execution"]["can_regenerate"]
        creation = failed["state"]["quick_creation"]
        shot = app.comic_storyboards.get_shot(creation["shot_id"])
        repeated = app.create_core_run({"domain": "comic", "state": request})
        assert repeated["id"] == failed["id"] and len(attempts) == 1
        retry = app.create_core_run({"domain": "comic", "state": {
            **request, "request_id": "workspace-explicit-retry",
            "shot_id": shot.shot_id, "shot_version": shot.version,
            "expected_project_version": app.comic_projects.get(
                shot.project_id).project.current_version,
        }})
    finally:
        logger.remove(sink)
    assert retry["status"] == "completed" and len(attempts) == 2
    assert attempts[0] != attempts[1] and retry["id"] != failed["id"]
    assert app.runtime_store.get_run(failed["id"]).status.value == "failed"
    assert app.comic_projects.get(shot.project_id).project.director_version == request[
        "director_version"]
    failed_event = next(e.payload for e in app.runtime_store.list_events(failed["id"])
                        if e.payload.get("kind") == "COMIC_IMAGE_GENERATION_FAILED")
    assert failed_event["http_status"] == "403"
    assert failed_event["provider_error_code"] == "AccessDenied.Unpurchased"
    assert failed_event["cost_cny"] == 0
    assert "sk-testsecret12345" not in json.dumps(failed_event)
    output = "\n".join(lines)
    for field in ("trace_id=", "project_id=", "shot_id=", "provider=", "model=",
                  "estimated_cost_cny=", "actual_cost_cny=0.3", "latency_ms=",
                  "http_status=403", "provider_error_code=AccessDenied.Unpurchased"):
        assert field in output


@pytest.mark.parametrize("critic_unavailable", [False, True])
def test_external_workspace_stops_at_real_prompt_without_any_image_side_effects(
    app, production, monkeypatch, tmp_path, critic_unavailable,
):
    original = app._comic_director_model

    def model(messages):
        response = original(messages)
        if critic_unavailable and '"title": "SemanticReview"' in messages[0]["content"]:
            payload = json.loads(response)
            payload.pop("confidence")
            return json.dumps(payload)
        return response

    monkeypatch.setattr(app, "_comic_director_model", model)
    request = _workspace_production_request(app, confirm=False)
    project_id = request["production_project_id"]
    spec = app.comic_projects.get_director(project_id)
    assert (spec.critic_status == "unavailable") is critic_unavailable
    assert (spec.critic_result is None) is critic_unavailable
    if critic_unavailable:
        director_run = next(run for run in app.runtime_store.list_runs()
                            if run.workflow == "comic.director")
        assert director_run.state["generation_incomplete"] is True
        assert not app.get_comic_director(project_id)["validation"]["can_confirm"]
        calls = list(production)
        with pytest.raises(ToolError, match="内部验证"):
            app.confirm_comic_director(project_id, {
                "version": spec.version,
                "expected_project_version": request["expected_project_version"],
            })
        assert production == calls
        assert not app.comic_projects.human_director_confirmed(spec)
        assert not app.runtime_store.list_approvals()
        assert not app.list_comic_storyboards(project_id)["storyboards"]
        assert not app.runtime_store.list_artifacts(type=ArtifactType.PROMPT)
        assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)
        assert not budget.list_ledger(project=project_id)
        assert not list(tmp_path.rglob("studio-*.json"))
        return
    app.confirm_comic_director(project_id, {
        "version": spec.version, "expected_project_version": request["expected_project_version"],
    })
    board = app.create_comic_storyboard(project_id, {
        "expected_project_version": request["expected_project_version"],
        "generate": True, "task": "当前导演方案分镜",
    })
    shot = app.comic_storyboards.get_shot(board["shots"][0]["shot_id"])
    before_tasks = list(tmp_path.rglob("studio-*.json"))
    before_ledger = budget.list_ledger(project=project_id)

    def forbidden(*args, **kwargs):
        pytest.fail("external workspace must never enter image execution or image billing")

    for target in (
        "kantoku.tools.image_gen.gen_image", "kantoku.tools.studio.gen_image",
        "kantoku.domains.comic.services.create_task",
        "kantoku.domains.comic.services.execute_task",
        "kantoku.domains.comic.services.qc_image",
        "kantoku.domains.comic.services.archive_reviewed_image",
        "kantoku.core.budget.reserve", "kantoku.core.budget.settle",
        "kantoku.core.budget.estimate_image_fen",
    ):
        monkeypatch.setattr(target, forbidden)
    for name in ("submit", "query", "validate_request", "check_access"):
        monkeypatch.setattr(app.image_service.provider, name, forbidden)
    calls = list(production)
    result = app.compile_comic_prompt(shot.shot_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_shot_version": shot.version, "image_mode": "external",
        "conversation_id": request["conversation_id"],
    })
    assert production[len(calls):] == ["prompt"]
    artifact = app.runtime_store.get_artifact(result["artifact_id"])
    assert artifact.type.value == "prompt" and artifact.metadata["positive_prompt"]
    assert artifact.metadata["model_target"] == "external"
    assert result["media_preparation"]["status"] == "awaiting_external_image"
    assert result["media_preparation"]["prompt_status"] == "ready"
    run = app.runtime_store.get_run(artifact.run_id)
    assert run.workflow == "comic.prompt.compile" and run.status.value == "completed"
    assert run.state["media_preparation"] == result["media_preparation"]
    assert run.state["conversation_id"] == request["conversation_id"]
    assert not run.state.get("request_id")  # no image-generation attempt or StudioTask
    assert not [r for r in app.runtime_store.list_runs() if r.workflow == "comic.production.v1"]
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)
    assert budget.list_ledger(project=project_id) == before_ledger
    assert list(tmp_path.rglob("studio-*.json")) == before_tasks
    assert app.comic_storyboards.get_shot(shot.shot_id) == shot
    assert app.comic_projects.get_director(project_id) == spec
    assert app.comic_prompts.get(shot.shot_id).artifact_id == artifact.id
    assert result["artifact_id"] == app.comic_prompts.versions(shot.shot_id)[0].artifact_id
    events = app.runtime_store.list_events(run.id)
    assert any(e.payload.get("status") == "awaiting_external_image" for e in events)
    assert not app.runtime_store.approval_for_node(run.id, "cost_approval")


def test_external_prompt_requires_human_confirmation_and_owned_guided_conversation(
    app, production,
):
    request = _workspace_production_request(app)
    project_id = request["production_project_id"]
    board = app.create_comic_storyboard(project_id, {
        "expected_project_version": request["expected_project_version"], "generate": True,
    })
    shot = board["shots"][0]
    body = {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_shot_version": shot["version"], "image_mode": "external",
    }
    other = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})
    before = list(production)
    with pytest.raises(ToolError, match="创作会话"):
        app.compile_comic_prompt(shot["shot_id"], {**body, "conversation_id": other["id"]})
    assert production == before
    # An automatic policy authorization cannot replace explicit director confirmation.
    for approval in app.runtime_store.list_approvals():
        if approval.request.get("kind") == "comic.director.confirmation":
            with app.runtime_store._connect() as connection:
                connection.execute("UPDATE approvals SET response_json=? WHERE id=?",
                                   (json.dumps({"human_review": False}), approval.id))
    with pytest.raises(ToolError, match="确认"):
        app.compile_comic_prompt(shot["shot_id"], {
            **body, "conversation_id": request["conversation_id"],
        })
    assert production == before
