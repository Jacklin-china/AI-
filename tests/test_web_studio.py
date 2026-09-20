"""浏览器工作台：HTTP 会话边界、付费确认、历史记录与恢复。"""

import json
import re
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_image_gen import _settings

from kantoku.config import ToolError
from kantoku.core import budget
from kantoku.domains.comic import services as comic_services
from kantoku.perception import report, review
from kantoku.schemas.qc import QcResult
from kantoku.shells import web_studio
from kantoku.tools import archive, studio
from kantoku.tools.image_gen import LocalFakeImageProvider


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> web_studio.StudioApplication:
    settings = _settings(tmp_path / "budget.db")
    settings.image = SimpleNamespace(
        prompt_max_chars=1000, force_single=True, model="test", width=100, height=100
    )
    settings.llm = SimpleNamespace(model_chat="text-test", model_vision="vision-test")
    for module in (web_studio, studio, budget):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    for module in (review, report, archive):
        monkeypatch.setattr(module, "_database_path", lambda: settings.storage.sqlite_path)
    monkeypatch.setattr(archive, "_archive_root", lambda: tmp_path / "archive")
    provider = LocalFakeImageProvider(tmp_path / "output", model_id="test", actual_fen=30)
    monkeypatch.setattr(web_studio, "_provider", lambda: provider)
    return web_studio.StudioApplication()


@pytest.fixture
def server(app: web_studio.StudioApplication) -> Iterator[int]:
    server = web_studio.make_server(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_root_and_session_boundary(server: int, app: web_studio.StudioApplication) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        assert app.token.encode() in response.read()
        for path in ("/api/state", "/media/anything"):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 403
            response.read()
        connection.request("GET", "/", headers={"Host": "attacker.example"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request(
            "POST",
            "/api/generate",
            body="{}",
            headers={
                "X-Studio-Token": app.token,
                "Origin": "https://attacker.example",
            },
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        assert app.job["state"] == "idle"
        connection.request("GET", "/api/state", headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        assert response.status == 200
        state = json.loads(response.read())
        assert state["tasks"] == []
        assert state["budgets"] == {}
    finally:
        connection.close()


def test_vue_bundle_is_served_without_exposing_session_token(
    server: int, app: web_studio.StudioApplication
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        asset = re.search(r'src="(/assets/[^"]+\.js)"', html)
        assert response.status == 200
        assert asset is not None
        assert "__TOKEN__" not in html

        connection.request("GET", asset.group(1))
        response = connection.getresponse()
        bundle = response.read()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/javascript"
        assert app.token.encode() not in bundle

        connection.request("GET", "/assets/%2e%2e/%2e%2e/.env")
        response = connection.getresponse()
        assert response.status == 404
        response.read()
    finally:
        connection.close()


def test_frontend_routes_serve_the_kantoku_app_shell(
    server: int,
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        for path in ("/workspace", "/runs/example", "/assets", "/domain/commerce"):
            connection.request("GET", path)
            response = connection.getresponse()
            html = response.read().decode("utf-8")
            assert response.status == 200
            assert "Kantoku" in html
            assert "__TOKEN__" not in html
    finally:
        connection.close()


def test_generate_requires_confirmation_and_reuses_original_task(
    app: web_studio.StudioApplication,
) -> None:
    data = {"project": "p", "prompt": "coffee", "shot_no": 1, "price": "0.30"}
    with pytest.raises(ToolError, match="明确确认"):
        app.perform("generate", data)
    assert studio.list_tasks() == []
    result = app.perform("generate", {**data, "confirmed": True})
    assert result["status"] == "succeeded"
    recovery = app.perform("recover", {"request_id": result["request_id"], "confirmed": True})
    assert recovery == result
    state = app.state()
    assert state["tasks"][0]["has_image"] is True
    assert state["budgets"]["p"]["settled_fen"] == 30
    assert state["budgets"]["p"]["held_fen"] == 0
    assert state["budgets"]["p"]["available_fen"] == 1970
    assert budget.summarize_budget("p").task_count == 1


def test_media_reads_only_recorded_asset(server: int, app: web_studio.StudioApplication) -> None:
    result = app.perform(
        "generate",
        {
            "project": "p",
            "prompt": "coffee",
            "shot_no": 1,
            "price": "0.30",
            "confirmed": True,
        },
    )
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        connection.request(
            "GET", "/media/" + result["request_id"], headers={"X-Studio-Token": app.token}
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.read().startswith(b"\x89PNG")
        connection.request("GET", "/media/../../.env", headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close()


def test_single_background_operation_and_recovery_of_worker_error(
    app: web_studio.StudioApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()
    completed = threading.Event()

    def blocked(action: str, data: dict[str, object]) -> dict[str, object]:
        gate.wait(5)
        completed.set()
        raise ToolError("mock failure")

    monkeypatch.setattr(app, "perform", blocked)
    app.start("compose", {})
    try:
        with pytest.raises(ToolError, match="已有操作"):
            app.start("generate", {})
    finally:
        gate.set()
        assert completed.wait(5)


def test_narrative_prompt_does_not_insert_poster_layout() -> None:
    narrative = studio.compose_prompt("雨天搀扶老人过街", "叙事静帧", "普通观众", "纪实")
    assert "海报要求" not in narrative
    assert "支撑关系" in narrative
    assert "海报要求" in studio.compose_prompt("咖啡", "宣传海报", "客人", "插画")


def test_quality_review_rework_archive_and_report_backend(
    app: web_studio.StudioApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = app.perform(
        "generate",
        {
            "project": "p",
            "prompt": "雨夜便利店中的克制情绪",
            "shot_no": 1,
            "price": "0.30",
            "confirmed": True,
        },
    )
    prediction = QcResult(
        broken_hands=False,
        watermark=False,
        composition_ok=False,
        persona_consistency=4,
        confidence=0.9,
        reason="主体层级不清楚",
    )
    monkeypatch.setattr(web_studio, "qc_image", lambda *args, **kwargs: prediction)
    request_id = generated["request_id"]
    app.perform(
        "qc",
        {
            "request_id": request_id,
            "purpose": "宣传图片",
            "audience": "普通观众",
            "style": "纪实摄影",
            "confirmed": True,
        },
    )
    reviewed = app.perform(
        "review",
        {
            "request_id": request_id,
            "purpose": "宣传图片",
            "audience": "普通观众",
            "cinematography_requirements": "主体明确，光源合理",
            "cinematography_notes": "人物和环境争抢注意力",
            "review_seconds": 20,
            "approved": False,
            "failure_reasons": ["composition"],
            "confirmed": True,
        },
    )
    assert reviewed["rework_created"] is True
    archived = app.perform(
        "archive", {"request_id": request_id, "confirmed": True}
    )
    assert archived["approved"] is False
    prepared = app.perform(
        "prepare_rework", {"request_id": request_id, "confirmed": True}
    )
    assert prepared["paid"] is False
    assert app.perform(
        "prepare_rework", {"request_id": request_id, "confirmed": True}
    )["request_id"] == prepared["request_id"]
    state = app.state()
    source = next(item for item in state["tasks"] if item["request_id"] == request_id)
    target = next(
        item for item in state["tasks"] if item["request_id"] == prepared["request_id"]
    )
    assert source["qc"] == prediction.model_dump()
    assert source["review"]["approved"] is False
    assert source["archived"] is True
    assert source["rework"]["status"] == "approved"
    assert target["status"] == "draft"
    assert app.perform("report", {"project": "p", "expected_shots": 1})[
        "report"
    ]["rework_count"] == 1


def test_human_reject_closes_rework_without_paid_generation(
    app: web_studio.StudioApplication,
) -> None:
    generated = app.perform(
        "generate",
        {
            "project": "rejected",
            "prompt": "主体不清楚的测试图片",
            "shot_no": 1,
            "price": "0.30",
            "confirmed": True,
        },
    )
    request_id = generated["request_id"]
    prediction = QcResult(
        broken_hands=False,
        watermark=False,
        composition_ok=False,
        persona_consistency=4,
        confidence=0.8,
        reason="主体层级不清楚",
    )
    reviewed = app.perform(
        "review",
        {
            "request_id": request_id,
            "purpose": "叙事静帧",
            "audience": "普通观众",
            "cinematography_requirements": "主体明确",
            "cinematography_notes": "不进入返工",
            "result": prediction.model_dump(),
            "approved": False,
            "decision": "reject",
            "failure_reasons": ["composition"],
            "confirmed": True,
        },
    )

    assert reviewed["decision"] == "reject"
    assert reviewed["rework_created"] is False
    assert review.get_rework_item(request_id).status == "cancelled"
    assert len(studio.list_tasks()) == 1
    assert budget.summarize_budget("rejected").task_count == 1


def test_core_api_run_approval_restart_resume_and_artifact(
    server: int,
    app: web_studio.StudioApplication,
) -> None:
    """HTTP Core API 使用数据库状态，并可由新应用实例继续。"""
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {
        "X-Studio-Token": app.token,
        "Content-Type": "application/json",
    }
    try:
        connection.request(
            "POST",
            "/api/runs",
            body=json.dumps({
                "domain": "commerce",
                "state": {"requirement": "便携阅读灯"},
            }),
            headers=headers,
        )
        response = connection.getresponse()
        run = json.loads(response.read())
        assert response.status == 201
        assert run["status"] == "waiting"
        assert run["current_node"] == "candidate_approval"
        run_id = run["id"]

        connection.request("GET", "/api/approvals", headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        approvals = json.loads(response.read())["approvals"]
        approval_id = approvals[0]["id"]
        connection.request(
            "POST",
            f"/api/approvals/{approval_id}/approve",
            body="{}",
            headers=headers,
        )
        response = connection.getresponse()
        assert response.status == 200
        candidate_result = json.loads(response.read())
        assert candidate_result["decision"] == "approve"
        assert candidate_result["current_node"] == "publish_approval"

        restarted = web_studio.StudioApplication()
        publish = restarted.runtime_store.approval_for_node(run_id, "publish_approval")
        assert publish is not None
        completed = restarted.decide_core_approval(publish.id, "approve", {})
        assert completed["status"] == "completed"
        assert completed["state"]["marketplace_draft"]["mock"] is True

        connection.request(
            "GET",
            f"/api/runs/{run_id}/artifacts",
            headers={"X-Studio-Token": app.token},
        )
        response = connection.getresponse()
        artifacts = json.loads(response.read())["artifacts"]
        assert response.status == 200
        assert artifacts[0]["metadata"]["mock"] is True

        connection.request(
            "GET",
            f"/api/artifacts?domain=commerce&run_id={run_id}",
            headers={"X-Studio-Token": app.token},
        )
        response = connection.getresponse()
        global_artifacts = json.loads(response.read())["artifacts"]
        assert response.status == 200
        assert {item["id"] for item in global_artifacts} == {
            item["id"] for item in artifacts
        }
        connection.request(
            "GET",
            f"/api/artifacts/{artifacts[0]['id']}",
            headers={"X-Studio-Token": app.token},
        )
        response = connection.getresponse()
        assert json.loads(response.read())["id"] == artifacts[0]["id"]

        connection.request("GET", "/api/skills", headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        skills = json.loads(response.read())["skills"]
        assert response.status == 200
        assert {item["domain"] for item in skills} >= {"comic", "commerce"}
        assert all(not str(item["handler_ref"]).startswith("C:\\") for item in skills)
    finally:
        connection.close()


def test_core_batch_http_api_lists_and_cancels(
    server: int,
    app: web_studio.StudioApplication,
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}
    try:
        connection.request(
            "POST",
            "/api/batches",
            body=json.dumps({
                "name": "HTTP five product demo",
                "workflow": "commerce.production.v1",
                "concurrency_limit": 2,
                "items": [{"requirement": f"product {index}"} for index in range(5)],
            }),
            headers=headers,
        )
        response = connection.getresponse()
        batch = json.loads(response.read())
        assert response.status == 201
        assert batch["status"] == "waiting"
        assert len(batch["runs"]) == 5

        connection.request("GET", "/api/batches", headers=headers)
        response = connection.getresponse()
        assert any(item["id"] == batch["id"] for item in json.loads(response.read())["batches"])
        connection.request("GET", f"/api/batches/{batch['id']}", headers=headers)
        response = connection.getresponse()
        assert json.loads(response.read())["id"] == batch["id"]
        connection.request(
            "POST", f"/api/batches/{batch['id']}/cancel", body="{}", headers=headers
        )
        response = connection.getresponse()
        cancelled = json.loads(response.read())
        assert response.status == 200
        assert cancelled["status"] == "cancelled"
        assert all(run["status"] == "cancelled" for run in cancelled["runs"])
    finally:
        connection.close()


def test_comic_core_uses_existing_generate_qc_review_and_archive(
    app: web_studio.StudioApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产适配器复用旧链路，而不是只让 Comic 跑 Fake Workflow。"""
    prediction = QcResult(
        broken_hands=False,
        watermark=False,
        composition_ok=True,
        persona_consistency=5,
        confidence=0.9,
        reason="离线测试通过",
    )
    monkeypatch.setattr(comic_services, "qc_image", lambda *args, **kwargs: prediction)
    waiting = app.create_core_run({
        "domain": "comic",
        "state": {
            "project": "core-comic",
            "prompt": "雨夜中的便利店",
            "shot_no": 1,
            "estimate_fen": 30,
            "confirmed": True,
        },
    })
    assert waiting["status"] == "waiting"
    assert budget.load_generation_result(waiting["state"]["request_id"]).status == "succeeded"
    assert review.load_qc_prediction(waiting["state"]["request_id"]) == prediction

    approval = app.runtime_store.list_approvals(pending_only=True)[0]
    app.decide_core_approval(
        approval.id,
        "approve",
        {"response": {"notes": "人工确认构图与硬缺陷均通过", "review_seconds": 10}},
    )
    completed = app.resume_core_run(waiting["id"])
    assert completed["status"] == "completed"
    assert Path(completed["state"]["archive_path"]).is_file()
    assert app.list_core_artifacts(waiting["id"])[0]["source"] == "comic.archive"
