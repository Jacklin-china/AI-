"""Workspace-only production diagnostics; no live or paid model calls."""

import pytest
from test_home_quick_domain import _workspace_production_request
from test_home_quick_domain import app as app_fixture
from test_home_quick_domain import production as production_fixture

from kantoku.config import ToolError
from kantoku.core import budget

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


def test_workspace_defaults_to_current_director_board_and_keeps_history(app, production):
    request = _workspace_production_request(app)
    project_id = request["production_project_id"]

    def create_board():
        return app.create_comic_storyboard(project_id, {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "generate": True, "task": "当前导演方案的关键画面",
        })

    historical = create_board()
    spec = app.create_comic_director(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "conversation_id": request["conversation_id"], "creation_mode": "professional",
        "task": "中式修仙少女站在竹林",
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
