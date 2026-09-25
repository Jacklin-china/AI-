"""浏览器工作台：HTTP 会话边界、付费确认、历史记录与恢复。"""

import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_image_gen import _settings

from kantoku.capabilities import creative
from kantoku.config import ToolError, logging_setup
from kantoku.core import budget
from kantoku.core.conversations import (
    ConversationMessageRecord,
    InteractionMode,
    MediaJobStatus,
    MessageRole,
    MessageType,
)
from kantoku.core.runtime.models import ArtifactType
from kantoku.domains.comic import services as comic_services
from kantoku.perception import report, review
from kantoku.schemas.media import ImageGenerationResult
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
    settings.budget.autonomous_image_auto_cny = Decimal("0.30")
    settings.budget.image_conversation_cny = Decimal("5.00")
    for module in (web_studio, studio, budget):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    for module in (review, report, archive):
        monkeypatch.setattr(module, "_database_path", lambda: settings.storage.sqlite_path)
    monkeypatch.setattr(archive, "_archive_root", lambda: tmp_path / "archive")
    provider = LocalFakeImageProvider(tmp_path / "output", model_id="test", actual_fen=30)
    monkeypatch.setattr(web_studio, "_provider", lambda: provider)
    monkeypatch.setattr(web_studio, "_brief_deltas", lambda brief: iter([brief]))
    monkeypatch.setattr(
        web_studio, "_image_result_summary",
        lambda requirement, _prompt, _trace: f"已按你的要求生成一张{requirement}。",
    )
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


def test_http_trace_and_error_id_match_structured_log(
    server: int, app: web_studio.StudioApplication,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging_setup, "LOG_DIR", tmp_path / "logs")
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        logging_setup.setup_logging("INFO")
        headers = {"X-Studio-Token": app.token, "X-Trace-ID": "trace-http-test-123"}
        connection.request("GET", "/api/runs", headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-Trace-ID") == headers["X-Trace-ID"]
        response.read()
        connection.request("POST", "/api/runs", body="{", headers=headers)
        response = connection.getresponse()
        assert response.status == 400
        failure = json.loads(response.read())
        assert failure["trace_id"] == headers["X-Trace-ID"]
        assert failure["error_kind"] == "invalid_input"
        records = [
            json.loads(line)["record"] for line in
            (tmp_path / "logs" / "kantoku.log").read_text(encoding="utf-8").splitlines()
        ]
        assert any(record["extra"]["trace_id"] == headers["X-Trace-ID"]
                   and "request start" in record["message"] for record in records)
        assert any(record["extra"]["error_id"] == failure["error_id"]
                   and "web_studio.py" in record["message"] for record in records)
        assert app.token not in json.dumps(records)
    finally:
        connection.close()
        logging_setup.logger.remove()


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


def _chat_message(role: MessageRole) -> ConversationMessageRecord:
    return ConversationMessageRecord(
        id="m1", conversation_id="c1", role=role, type=MessageType.TEXT,
        content="内容", run_id=None, event_id=None,
        created_at=datetime.now(UTC),
    )


def test_image_count_parses_quantity_words() -> None:
    assert web_studio._image_count("帮我生成一张写实人像") == 1
    assert web_studio._image_count("生成3张海报") == 3
    assert web_studio._image_count("做五张概念图") == 5
    assert web_studio._image_count("画两只猫") == 2
    assert web_studio._image_count("随便聊聊") == 1
    assert web_studio._image_count("生成99张图") == 20


def test_execution_confirmation_needs_short_phrase_and_prior_guidance() -> None:
    assistant = [_chat_message(MessageRole.ASSISTANT)]
    user_only = [_chat_message(MessageRole.USER)]
    assert web_studio._is_execution_confirmed("可以", assistant) is True
    assert web_studio._is_execution_confirmed("开始生成", assistant) is True
    assert web_studio._is_execution_confirmed("可以", user_only) is False
    assert web_studio._is_execution_confirmed("可以" * 40, assistant) is False
    assert web_studio._is_execution_confirmed("帮我做一张图", assistant) is False


def test_home_image_chat_generates_conversation_artifact_without_run_or_studio_task(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_studio, "_enhance_prompt", lambda text, _trace: text)
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    request = {
        "content": "生成一个蜡笔小新头像",
        "generation_request_id": "generation-home-test-1",
    }
    events = list(app.stream_conversation(conversation["id"], request))
    assert not any(name == "run" for name, _ in events)
    assert not app.runtime_store.list_runs()
    assert not app.list_core_runs()
    assert not studio.list_tasks()
    assert not app.runtime_store.list_approvals()
    message = next(payload for name, payload in events
                   if name == "message" and payload["type"] == "artifact")
    assert message["type"] == "artifact"
    artifact = app.runtime_store.get_artifact(message["artifact_id"])
    assert artifact.conversation_id == conversation["id"]
    assert artifact.run_id is None
    assert not app.query_core_artifacts()
    assert Path(str(artifact.location)).is_file()
    assert app.runtime_store.get_conversation(conversation["id"]).active_run_id is None
    reservation = budget.get_reservation(request["generation_request_id"])
    assert reservation is not None
    assert reservation.conversation_id == conversation["id"]
    assert reservation.run_id is None
    assert reservation.artifact_id == artifact.id
    assert reservation.status == "settled"
    restored = type(app.runtime_store)(app.runtime_store.path)
    restored_messages = restored.list_conversation_messages(conversation["id"])
    assert any(item.artifact_id == artifact.id for item in restored_messages)
    assert restored_messages[-1].event_id == "generation-summary:generation-home-test-1"
    replay = list(app.stream_conversation(conversation["id"], request))
    assert next(payload for name, payload in replay
                if name == "message" and payload["type"] == "artifact")["id"] == message["id"]
    assert app.image_service.provider.submit_count == 1


def test_home_natural_image_request_skips_parameter_questions_and_preserves_subject(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = "请给我一张蜡笔小新中正男的卡通图片"
    monkeypatch.setattr(web_studio, "_enhance_prompt", lambda text, _trace: f"原始主体：{text}")
    conversation = app.create_conversation({"interaction_mode": "autonomous"})

    events = list(app.stream_conversation(conversation["id"], {
        "content": subject, "generation_request_id": "generation-home-natural-language",
    }))

    assert next(payload for name, payload in events if name == "intent")["tool"] == "image.generate"
    names = [name for name, _ in events]
    assert names.index("prompt_prepared") < names.index("image_generating")
    assert names.index("image_generating") < names.index("image_ready")
    assert names.index("image_ready") < names.index("image_summary")
    prepared = next(payload for name, payload in events
                    if name == "message" and payload["event_id"].startswith("generation-prompt:"))
    assert "正男" in prepared["content"]
    message = next(payload for name, payload in events
                   if name == "message" and payload["type"] == "artifact")
    assert message["type"] == "artifact"
    assert subject in app.runtime_store.get_conversation_generation(
        "generation-home-natural-language"
    )["prompt"]
    assert not app.runtime_store.list_runs()
    assert not app.runtime_store.list_approvals()


def test_home_image_followups_generate_again_without_clarification(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_studio, "_enhance_prompt", lambda text, _trace: text)
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    for index, (content, expected_action) in enumerate((
        ("帮我生成一张干物妹小埋中的海老名卡通图片", "image.generate"),
        ("再帮我生成小埋", "image.generate"),
        ("换成海老名", "image.edit"),
    )):
        request_id = f"generation-followup-{index}"
        events = list(app.stream_conversation(conversation["id"], {
            "content": content, "generation_request_id": request_id,
        }))
        intent = next(payload for name, payload in events if name == "intent")
        assert intent["tool"] == expected_action
        assert any(name == "message" and payload["type"] == "artifact" for name, payload in events)
        assert not any(
            "确认" in payload.get("content", "")
            for name, payload in events if name == "message"
        )
        assert content in app.runtime_store.get_conversation_generation(request_id)["prompt"]
    assert app.image_service.provider.submit_count == 3
    assert not app.runtime_store.list_runs()
    assert not app.runtime_store.list_approvals()


def test_image_brief_streams_before_provider_submission(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_studio, "_brief_deltas",
        lambda brief: (brief[i:i + 5] for i in range(0, len(brief), 5)),
    )
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    events = []
    for name, payload in app.stream_conversation(conversation["id"], {
        "content": "帮我生成小埋卡通头像", "generation_request_id": "generation-brief-chunks",
    }):
        events.append((name, payload))
        if name == "delta":
            assert app.image_service.provider.submit_count == 0
    chunks = [payload["content"] for name, payload in events if name == "delta"]
    assert len(chunks) > 1
    assert all(len(chunk) <= 5 for chunk in chunks)
    assert "小埋" in "".join(chunks)
    assert app.image_service.provider.submit_count == 1
    assert "小埋" in app.runtime_store.get_conversation_generation(
        "generation-brief-chunks"
    )["prompt"]


def test_generic_image_followup_reuses_subject_instead_of_inventing_one(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_studio, "_enhance_prompt", lambda text, _trace: text)
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    app.runtime_store.add_conversation_message(
        conversation["id"], role=MessageRole.USER, type=MessageType.TEXT,
        content="请给我一张蜡笔小新中正男的卡通图片",
    )
    events = list(app.stream_conversation(conversation["id"], {
        "content": "生成图片", "generation_request_id": "generation-home-context-followup",
    }))
    assert any(name == "message" and payload["type"] == "artifact" for name, payload in events)
    assert "正男" in app.runtime_store.get_conversation_generation(
        "generation-home-context-followup"
    )["prompt"]


def test_home_creative_followup_uses_completed_artifact_as_real_reference(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = app.image_service.provider
    submitted: list[tuple[str, tuple[str, ...]]] = []
    original_submit = provider.submit

    def capture_submit(**kwargs: object) -> str:
        submitted.append((str(kwargs["prompt"]), tuple(kwargs["reference_urls"])))
        return original_submit(**kwargs)

    monkeypatch.setattr(provider, "submit", capture_submit)
    monkeypatch.setattr(creative, "chat", lambda *_args, **_kwargs: SimpleNamespace(
        content=(
            '{"action":"image.edit","subject":"熊二","style":"写实",'
            '"composition":"半身头像","background":"树林","use_reference":true}'
        ),
    ))
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    first = list(app.stream_conversation(conversation["id"], {
        "content": "帮我生成熊大的写实头像", "generation_request_id": "generation-creative-first",
    }))
    first_artifact = next(payload["artifact_id"] for name, payload in first
                          if name == "image_ready")
    second = list(app.stream_conversation(conversation["id"], {
        "content": "把熊二的卡通照片跟熊大的一样给我就行",
        "generation_request_id": "generation-creative-second",
    }))
    assert any(name == "image_ready" for name, _ in second)
    assert provider.submit_count == 2
    assert not submitted[0][1]
    assert submitted[1][1][0].startswith("data:image/png;base64,")
    assert "把熊二的卡通照片跟熊大的一样给我就行" in submitted[1][0]
    saved = app.runtime_store.get_conversation_generation("generation-creative-second")
    assert saved["reference_artifact_id"] == first_artifact
    assert json.loads(saved["context_json"])["subject"] == "熊二"
    second_artifact = app.runtime_store.list_artifacts(conversation_id=conversation["id"])[0]
    assert second_artifact.metadata["parent_artifact_id"] == first_artifact
    assert not app.runtime_store.list_runs()
    replay = list(app.stream_conversation(conversation["id"], {
        "content": "把熊二的卡通照片跟熊大的一样给我就行",
        "generation_request_id": "generation-creative-second",
    }))
    assert any(name == "image_ready" for name, _ in replay)
    assert provider.submit_count == 2

    before = provider.submit_count
    reminder = list(app.stream_conversation(conversation["id"], {"content": "图片在哪里"}))
    assert provider.submit_count == before
    assert any(name == "message" and payload["artifact_id"] == second_artifact.id
               for name, payload in reminder)


@pytest.mark.parametrize(("suffix", "image_bytes", "mime"), [
    ("png", b"\x89PNG\r\n\x1a\nlegacy-image", "image/png"),
    ("jpg", b"\xff\xd8\xfflegacy-image", "image/jpeg"),
    ("webp", b"RIFF\x10\x00\x00\x00WEBPlegacy-image", "image/webp"),
])
def test_home_recovers_legacy_image_context_without_media_job(
    app: web_studio.StudioApplication, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, suffix: str, image_bytes: bytes, mime: str,
) -> None:
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    app.runtime_store.add_conversation_message(
        conversation["id"], role=MessageRole.USER, type=MessageType.TEXT,
        content="帮我生成熊大的卡通头像",
    )
    image_path = tmp_path / f"legacy.{suffix}"
    image_path.write_bytes(image_bytes)
    artifact = app.runtime_store.create_artifact(
        type=ArtifactType.IMAGE, conversation_id=conversation["id"],
        node_id="image.generate", source="conversation.image.generate",
        location=str(image_path),
    )
    app.runtime_store.add_conversation_message(
        conversation["id"], role=MessageRole.ASSISTANT, type=MessageType.ARTIFACT,
        content="图片已生成", artifact_id=artifact.id,
    )
    context = app._recent_image_context(conversation["id"])
    assert context is not None
    assert context.artifact_id == artifact.id
    assert "熊大" in context.subject
    provider = app.image_service.provider
    original_submit = provider.submit
    references: list[tuple[str, ...]] = []

    def capture_submit(**kwargs: object) -> str:
        references.append(tuple(kwargs["reference_urls"]))
        return original_submit(**kwargs)

    monkeypatch.setattr(provider, "submit", capture_submit)
    monkeypatch.setattr(creative, "chat", lambda *_args, **_kwargs: SimpleNamespace(
        content='{"action":"image.edit","subject":"熊二","use_reference":true}',
    ))
    events = list(app.stream_conversation(conversation["id"], {
        "content": "把熊二做成和熊大一样", "generation_request_id": f"legacy-{suffix}",
    }))
    assert any(name == "image_ready" for name, _ in events)
    assert references[0][0].startswith(f"data:{mime};base64,")


def test_prompt_enhancer_cannot_replace_user_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "请给我一张蜡笔小新中正男的卡通图片"
    monkeypatch.setattr(
        web_studio, "chat", lambda _messages: SimpleNamespace(content="古镇雨巷里的白衣女子"),
    )
    prompt = web_studio._enhance_prompt(original, "trace-preserve-subject")
    assert original in prompt
    assert "白衣女子" not in prompt
    assert "古镇雨巷" not in prompt


def test_home_image_brief_and_model_summary_keep_the_requested_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirement = "帮我生成鬼灭之刃无一郎的卡通头像"
    prompt = f"严格遵照用户原始要求：{requirement}。主体居中，轮廓清晰。"
    monkeypatch.setattr(
        web_studio, "chat",
        lambda _messages: SimpleNamespace(content=json.dumps({
            "elements": "无一郎", "style": "卡通", "composition": "主体居中",
        })),
    )
    brief = web_studio._image_brief(requirement)
    summary = web_studio._image_result_summary(requirement, prompt, "trace-summary")
    assert brief.startswith("我会生成一张鬼灭之刃无一郎的卡通头像")
    assert "无一郎" in summary and "卡通" in summary and "主体居中" in summary
    assert "白衣女子" not in summary


def test_home_image_summary_ignores_unverified_model_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirement = "请给我一张蜡笔小新中正男的卡通图片"
    monkeypatch.setattr(
        web_studio, "chat",
        lambda _messages: SimpleNamespace(content=json.dumps({
            "elements": "白衣女子", "style": "油画", "composition": "雨巷",
        })),
    )
    summary = web_studio._image_result_summary(requirement, requirement, "trace-summary")
    assert "正男" in summary
    assert "白衣女子" not in summary
    assert "雨巷" not in summary


def test_home_image_flow_ignores_unrelated_model_prompt_text(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "请给我一张蜡笔小新中正男的卡通图片"
    monkeypatch.setattr(
        web_studio, "chat", lambda _messages: SimpleNamespace(content="古镇雨巷里的白衣女子"),
    )
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    events = list(app.stream_conversation(conversation["id"], {
        "content": original, "generation_request_id": "generation-preserve-character",
    }))
    assert any(name == "message" and payload["type"] == "artifact" for name, payload in events)
    saved = app.runtime_store.get_conversation_generation("generation-preserve-character")
    assert saved is not None
    prompt = saved["prompt"]
    assert original in prompt
    assert "白衣女子" not in prompt


def test_home_video_intent_does_not_start_mock_generation(
    app: web_studio.StudioApplication,
) -> None:
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    events = list(app.stream_conversation(conversation["id"], {
        "content": "帮我制作一段猫咪奔跑的视频",
    }))
    assert next(payload for name, payload in events if name == "intent")["tool"] == "video.generate"
    message = next(payload for name, payload in events if name == "message")
    assert "尚未接入真实视频生成服务" in message["content"]
    assert not app.runtime_store.list_runs()
    assert not app.runtime_store.list_artifacts(conversation_id=conversation["id"])


def test_home_image_http_stream_returns_real_artifact_only_in_chat(
    server: int, app: web_studio.StudioApplication,
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}
    try:
        connection.request(
            "POST", "/api/conversations",
            body=json.dumps({"interaction_mode": "autonomous"}), headers=headers,
        )
        response = connection.getresponse()
        conversation = json.loads(response.read())
        assert response.status == 201
        connection.request(
            "POST", f"/api/conversations/{conversation['id']}/messages/stream",
            body=json.dumps({
                "content": "生成一个蜡笔小新头像",
                "generation_request_id": "generation-http-home-test",
            }),
            headers=headers,
        )
        response = connection.getresponse()
        stream = response.read().decode("utf-8")
        assert response.status == 200
        assert "event: prompt_prepared" in stream
        assert "event: image_generating" in stream
        assert "event: image_ready" in stream
        assert "event: image_summary" in stream
        assert "event: run" not in stream
        assert "event: error" not in stream
        assert "event: approval" not in stream
        assert "\"type\": \"artifact\"" in stream
        artifact = app.runtime_store.list_artifacts(conversation_id=conversation["id"])[0]
        connection.request(
            "GET", f"/api/artifacts/{artifact.id}/content",
            headers={"X-Studio-Token": app.token},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.read().startswith(b"\x89PNG")
        connection.request("GET", "/api/runs", headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        assert json.loads(response.read())["runs"] == []
        connection.request("GET", "/api/state", headers={"X-Studio-Token": app.token})
        response = connection.getresponse()
        assert json.loads(response.read())["tasks"] == []
    finally:
        connection.close()


def test_another_conversation_replies_while_image_generation_is_waiting(
    server: int, app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_chat = app.create_conversation({"interaction_mode": "autonomous"})
    ordinary_chat = app.create_conversation({"interaction_mode": "autonomous"})
    generating = threading.Event()
    release = threading.Event()
    image_response: list[str] = []
    original_generate = app.image_service.generate

    def slow_generate(**kwargs: object) -> ConversationMessageRecord:
        generating.set()
        assert release.wait(5)
        return original_generate(**kwargs)

    monkeypatch.setattr(app.image_service, "generate", slow_generate)
    monkeypatch.setattr(
        web_studio, "stream_chat", lambda *_args, **_kwargs: iter(["普通聊天已回复"]),
    )
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}

    def request_image() -> None:
        connection = HTTPConnection("127.0.0.1", server, timeout=8)
        try:
            connection.request(
                "POST", f"/api/conversations/{image_chat['id']}/messages/stream",
                body=json.dumps({
                    "content": "帮我生成一张小埋卡通图片",
                    "generation_request_id": "generation-concurrent-image",
                }), headers=headers,
            )
            image_response.append(connection.getresponse().read().decode("utf-8"))
        finally:
            connection.close()

    thread = threading.Thread(target=request_image, daemon=True)
    thread.start()
    try:
        assert generating.wait(5)
        connection = HTTPConnection("127.0.0.1", server, timeout=2)
        try:
            connection.request(
                "POST", f"/api/conversations/{ordinary_chat['id']}/messages/stream",
                body=json.dumps({"content": "你好"}), headers=headers,
            )
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "普通聊天已回复" in body
            assert not image_response
        finally:
            connection.close()
    finally:
        release.set()
        thread.join(8)
    assert image_response and "event: image_ready" in image_response[0]


def test_two_chats_generate_images_and_reopen_recovers_persisted_state(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = app.image_service.provider
    original_query = provider.query
    first_query = threading.Event()
    second_query = threading.Event()
    release = threading.Event()
    query_lock = threading.Lock()
    query_count = 0

    def waiting_query(provider_job_id: str) -> ImageGenerationResult:
        nonlocal query_count
        with query_lock:
            query_count += 1
            (first_query if query_count == 1 else second_query).set()
        assert release.wait(5)
        return original_query(provider_job_id)

    monkeypatch.setattr(provider, "query", waiting_query)
    chat_a = app.create_conversation({"interaction_mode": "autonomous"})
    chat_b = app.create_conversation({"interaction_mode": "autonomous"})
    stream_a = app.stream_conversation(chat_a["id"], {
        "content": "帮我生成熊二图片", "generation_request_id": "generation-bear-a",
    })
    while next(stream_a)[0] != "image_generating":
        pass
    assert first_query.wait(5)
    stream_a.close()  # Simulate leaving/closing the original chat stream.
    responses_b: list[tuple[str, dict[str, object]]] = []
    thread = threading.Thread(target=lambda: responses_b.extend(app.stream_conversation(
        chat_b["id"], {
            "content": "帮我生成一只蓝色小鸟图片",
            "generation_request_id": "generation-bird-b",
        },
    )), daemon=True)
    thread.start()
    try:
        assert second_query.wait(5), "B must reach the provider while A is still generating"
        reopened = type(app.runtime_store)(app.runtime_store.path)
        assert reopened.get_media_job("generation-bear-a").status == MediaJobStatus.GENERATING
        assert reopened.get_media_job("generation-bird-b").status == MediaJobStatus.GENERATING
        assert app.conversation(chat_a["id"])["media_jobs"][0]["status"] == "generating"
        assert any(
            message["event_id"] == "generation-prompt:generation-bear-a"
            for message in app.conversation(chat_a["id"])["messages"]
        )
    finally:
        release.set()
        thread.join(8)
    assert not thread.is_alive()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if reopened.get_media_job("generation-bear-a").status == MediaJobStatus.COMPLETED:
            break
        time.sleep(0.02)
    assert reopened.get_media_job("generation-bear-a").status == MediaJobStatus.COMPLETED
    assert reopened.get_media_job("generation-bird-b").status == MediaJobStatus.COMPLETED
    assert provider.submit_count == 2
    assert len(reopened.list_artifacts(conversation_id=chat_a["id"])) == 1
    assert len(reopened.list_artifacts(conversation_id=chat_b["id"])) == 1
    assert any(name == "image_ready" for name, _ in responses_b)
    assert not reopened.list_runs()


def test_media_job_cannot_claim_completion_without_real_artifact(
    app: web_studio.StudioApplication,
) -> None:
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    user = app.runtime_store.add_conversation_message(
        conversation["id"], role=MessageRole.USER,
        type=MessageType.TEXT, content="生成一张熊二图片",
    )
    app.runtime_store.create_media_job("generation-missing-artifact", conversation["id"], user.id)
    with pytest.raises(ToolError, match="真实 Artifact"):
        app.runtime_store.update_media_job(
            "generation-missing-artifact", MediaJobStatus.COMPLETED,
            artifact_id="artifact-does-not-exist",
        )
    assert app.runtime_store.get_media_job("generation-missing-artifact").status == (
        MediaJobStatus.PENDING
    )


def test_home_image_over_auto_budget_never_submits(
    app: web_studio.StudioApplication,
) -> None:
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    events = list(app.stream_conversation(conversation["id"], {
        "content": "生成两张蜡笔小新头像",
        "generation_request_id": "generation-over-auto-test",
    }))
    assert not any(name in {"run", "activity"} for name, _ in events)
    assert next(payload for name, payload in events if name == "message")["type"] == "text"
    assert app.image_service.provider.submit_count == 0
    assert not app.runtime_store.list_runs()
    assert not app.runtime_store.list_artifacts(conversation_id=conversation["id"])


def test_home_image_budget_identity_is_conversation_not_project_shot(
    app: web_studio.StudioApplication,
) -> None:
    settings = web_studio.get_settings()
    settings.budget.image_project_cny = Decimal("0.01")
    settings.budget.image_episode_cny = Decimal("0.01")
    settings.budget.image_shot_cny = Decimal("0.01")
    for index in range(2):
        conversation = app.create_conversation({"interaction_mode": "autonomous"})
        request_id = f"generation-independent-chat-{index}"
        events = list(app.stream_conversation(conversation["id"], {
            "content": "生成一个蜡笔小新头像",
            "generation_request_id": request_id,
        }))
        assert any(name == "message" and payload["type"] == "artifact" for name, payload in events)
        reservation = budget.get_reservation(request_id)
        assert reservation is not None and reservation.conversation_id == conversation["id"]
    assert app.image_service.provider.submit_count == 2


def test_home_image_restart_queries_old_provider_job_without_second_submit(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = app.image_service.provider
    original_query = provider.query
    query_count = 0

    def pending_once(provider_job_id: str) -> ImageGenerationResult:
        nonlocal query_count
        query_count += 1
        if query_count == 1:
            return ImageGenerationResult(
                path=None, provider_job_id=provider_job_id,
                status="unknown", actual_fen=None, error="still pending",
            )
        return original_query(provider_job_id)

    monkeypatch.setattr(provider, "query", pending_once)
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    request = {
        "content": "生成一个蜡笔小新头像",
        "generation_request_id": "generation-restart-same-job",
    }
    first = list(app.stream_conversation(conversation["id"], request))
    assert any(name == "message" and payload["type"] == "status" for name, payload in first)
    assert not any(name == "image_failed" for name, _ in first)
    assert app.runtime_store.get_media_job(
        request["generation_request_id"]
    ).status == MediaJobStatus.GENERATING
    saved = budget.get_reservation(request["generation_request_id"])
    assert saved is not None and saved.provider_job_id is not None
    assert saved.status == "unknown"

    restarted = web_studio.StudioApplication()
    second = list(restarted.stream_conversation(conversation["id"], request))
    assert any(name == "message" and payload["type"] == "artifact" for name, payload in second)
    assert provider.submit_count == 1
    assert query_count == 2
    assert budget.get_reservation(request["generation_request_id"]).status == "settled"
    assert restarted.runtime_store.get_media_job(
        request["generation_request_id"]
    ).status == MediaJobStatus.COMPLETED
    assert not restarted.runtime_store.list_runs()


def test_home_image_edit_restart_preserves_reference_without_resubmission(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = app.image_service.provider
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    first = list(app.stream_conversation(conversation["id"], {
        "content": "帮我生成熊大的卡通头像",
        "generation_request_id": "generation-edit-restart-original",
    }))
    reference_id = next(payload["artifact_id"] for name, payload in first
                        if name == "image_ready")
    monkeypatch.setattr(creative, "chat", lambda *_args, **_kwargs: SimpleNamespace(
        content='{"action":"image.edit","subject":"熊二","use_reference":true}',
    ))
    original_query = provider.query
    query_count = 0

    def pending_once(provider_job_id: str) -> ImageGenerationResult:
        nonlocal query_count
        query_count += 1
        if query_count == 1:
            return ImageGenerationResult(
                path=None, provider_job_id=provider_job_id,
                status="unknown", actual_fen=None, error="still pending",
            )
        return original_query(provider_job_id)

    monkeypatch.setattr(provider, "query", pending_once)
    request = {
        "content": "把熊二做成和熊大一样",
        "generation_request_id": "generation-edit-restart-followup",
    }
    pending = list(app.stream_conversation(conversation["id"], request))
    assert not any(name == "image_ready" for name, _ in pending)
    saved = app.runtime_store.get_conversation_generation(request["generation_request_id"])
    assert saved is not None and saved["reference_artifact_id"] == reference_id

    restarted = web_studio.StudioApplication()
    completed = list(restarted.stream_conversation(conversation["id"], request))
    artifact_id = next(payload["artifact_id"] for name, payload in completed
                       if name == "image_ready")
    artifact = restarted.runtime_store.get_artifact(artifact_id)
    assert artifact.metadata["parent_artifact_id"] == reference_id
    assert provider.submit_count == 2  # one original, one edit; restart only queried
    assert query_count == 2


def test_continue_task_resumes_same_image_job_and_reopens_real_artifact(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = app.image_service.provider
    original_query = provider.query
    allow_query = threading.Event()
    query_count = 0

    def pending_then_complete(provider_job_id: str) -> ImageGenerationResult:
        nonlocal query_count
        query_count += 1
        if query_count == 1:
            return ImageGenerationResult(
                path=None, provider_job_id=provider_job_id,
                status="unknown", actual_fen=None, error="still processing",
            )
        assert allow_query.wait(5)
        return original_query(provider_job_id)

    monkeypatch.setattr(provider, "query", pending_then_complete)
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    first = list(app.stream_conversation(conversation["id"], {
        "content": "帮我生成熊大的卡通头像",
        "generation_request_id": "generation-resume-command",
    }))
    assert not any(name == "image_ready" for name, _ in first)
    resumed = list(app.stream_conversation(conversation["id"], {"content": "继续任务"}))
    assert any(name == "image_generating" for name, _ in resumed)
    assert provider.submit_count == 1
    assert len(app.runtime_store.list_media_jobs(conversation["id"])) == 1
    allow_query.set()
    for _ in range(50):
        if app.runtime_store.get_media_job("generation-resume-command").status == (
            MediaJobStatus.COMPLETED
        ):
            break
        time.sleep(0.05)
    completed = list(app.stream_conversation(conversation["id"], {"content": "继续任务"}))
    artifact_id = next(payload["artifact_id"] for name, payload in completed
                       if name == "image_ready")
    assert app.runtime_store.get_artifact(artifact_id).location is not None
    assert provider.submit_count == 1
    assert len(app.runtime_store.list_media_jobs(conversation["id"])) == 1


def test_continue_task_reports_persisted_failure_without_resubmitting(
    app: web_studio.StudioApplication,
) -> None:
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    user = app.runtime_store.add_conversation_message(
        conversation["id"], role=MessageRole.USER, type=MessageType.TEXT,
        content="帮我生成一张头像",
    )
    app.runtime_store.create_media_job("generation-failed-resume", conversation["id"], user.id)
    app.runtime_store.update_media_job(
        "generation-failed-resume", MediaJobStatus.FAILED,
        error_message="供应商返回失败；没有生成图片。",
    )
    resumed = list(app.stream_conversation(conversation["id"], {"content": "继续任务"}))
    assert any(name == "image_failed" for name, _ in resumed)
    assert any(name == "message" and "供应商返回失败" in payload["content"]
               for name, payload in resumed)
    assert app.image_service.provider.submit_count == 0


def test_continue_task_never_resubmits_unknown_provider_request_without_task_id(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = app.image_service.provider
    submits = 0

    def uncertain_submit(**_kwargs: object) -> str:
        nonlocal submits
        submits += 1
        raise TimeoutError("provider response lost")

    monkeypatch.setattr(provider, "submit", uncertain_submit)
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    first = list(app.stream_conversation(conversation["id"], {
        "content": "帮我生成一张头像",
        "generation_request_id": "generation-resume-unknown-id",
    }))
    assert any(name == "image_failed" for name, _ in first)
    resumed = list(app.stream_conversation(conversation["id"], {"content": "继续任务"}))
    assert any(name == "image_failed" for name, _ in resumed)
    assert any(name == "message" and "人工对账" in payload["content"]
               for name, payload in resumed)
    assert submits == 1


def test_reopened_chat_keeps_polling_existing_provider_job_until_ready(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = app.image_service.provider
    original_query = provider.query
    query_count = 0

    def processing_twice(provider_job_id: str) -> ImageGenerationResult:
        nonlocal query_count
        query_count += 1
        if query_count < 3:
            return ImageGenerationResult(
                path=None, provider_job_id=provider_job_id,
                status="unknown", actual_fen=None, error="still processing",
            )
        return original_query(provider_job_id)

    monkeypatch.setattr(provider, "query", processing_twice)
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    request_id = "generation-auto-poll"
    list(app.stream_conversation(conversation["id"], {
        "content": "帮我生成熊大的卡通头像", "generation_request_id": request_id,
    }))
    assert app.runtime_store.get_media_job(request_id).status == MediaJobStatus.GENERATING
    app.conversation(conversation["id"])
    app._media_futures[request_id].result(timeout=5)
    assert app.runtime_store.get_media_job(request_id).status == MediaJobStatus.GENERATING
    app._media_poll_at[request_id] = 0.0  # Advance the test past the polling throttle.
    app.conversation(conversation["id"])
    app._media_futures[request_id].result(timeout=5)
    detail = app.conversation(conversation["id"])
    assert detail["media_jobs"][0]["status"] == "completed"
    assert any(message["artifact_id"] for message in detail["messages"])
    assert provider.submit_count == 1
    assert query_count == 3


def test_task_history_hides_legacy_home_run_but_keeps_guided_run(
    app: web_studio.StudioApplication,
) -> None:
    title = "旧首页图片请求"
    home = app.create_conversation({"interaction_mode": "autonomous", "title": title})
    app.runtime_store.add_conversation_message(
        home["id"], role=MessageRole.USER, type=MessageType.TEXT, content="生成图片",
    )
    legacy = app.runtime_store.create_run(
        "comic", "comic.v1", {"project": title}, "prepare",
    )
    guided = app.create_conversation({
        "interaction_mode": "guided", "domain": "comic", "title": title,
    })
    app.runtime_store.add_conversation_message(
        guided["id"], role=MessageRole.USER, type=MessageType.TEXT, content="生成图片",
    )
    professional = app.runtime_store.create_run(
        "comic", "comic.v1", {"project": title}, "prepare",
    )
    visible = {run["id"] for run in app.list_core_runs()}
    assert legacy.id not in visible
    assert professional.id in visible
    assert app.runtime_store.get_run(legacy.id).id == legacy.id


def test_guided_image_chat_waits_for_explicit_start(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.runner, "submit", lambda execute: execute())
    monkeypatch.setattr(
        web_studio, "stream_chat",
        lambda *_args, **_kwargs: iter(["请确认风格与比例，回复开始后提交。"]),
    )
    conversation = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})
    first = list(app.stream_conversation(conversation["id"], {
        "content": "帮我生成一个写实版的大耳朵图图",
    }))
    assert not any(name == "run" for name, _ in first)
    assert first[-1] == ("done", {"run_id": None})
    second = list(app.stream_conversation(conversation["id"], {"content": "开始"}))
    run = next(payload for name, payload in second if name == "run")
    assert app.runtime_store.get_run(run["id"]).status.value == "waiting"
    assert "大耳朵图图" in run["state"]["prompt"]


def test_home_fast_domain_persists_and_uses_shared_image_capability(
    app: web_studio.StudioApplication,
) -> None:
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    conversation_id = conversation["id"]
    selected = app.set_fast_domain(conversation_id, "studio")
    assert selected["domain"] == "studio"
    assert app.conversation(conversation_id)["domain"] == "studio"

    events = list(app.stream_conversation(conversation_id, {
        "content": "帮我生成一张森林中的卡通小熊",
    }))
    assert next(payload for name, payload in events if name == "intent")["tool"] == "image.generate"
    assert any(name == "image_ready" for name, _ in events)
    assert app.runtime_store.list_runs(interaction_mode=InteractionMode.AUTONOMOUS) == []
    detail = app.conversation(conversation_id)
    assert any(message["artifact_id"] for message in detail["messages"])
    assert detail["media_jobs"][0]["status"] == "completed"
    assert app.set_fast_domain(conversation_id, None)["domain"] is None
    with pytest.raises(ToolError):
        app.set_fast_domain(conversation_id, "unknown")


def test_home_fast_commerce_reuses_workflow_without_professional_approvals(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.runner, "submit", lambda execute: execute())
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    conversation_id = conversation["id"]
    app.set_fast_domain(conversation_id, "commerce")

    events = list(app.stream_conversation(conversation_id, {
        "content": "帮我制作一个便携阅读灯的 Ozon 商品 Listing",
    }))
    intent = next(payload for name, payload in events if name == "intent")
    assert intent["tool"] == "workflow.start"
    run = next(payload for name, payload in events if name == "run")
    stored = app.runtime_store.get_run(run["id"])
    assert run["id"] in {
        item.id for item in app.runtime_store.list_runs(
            interaction_mode=InteractionMode.AUTONOMOUS,
        )
    }
    assert stored.status.value == "completed"
    assert stored.state["execution_mode"] == "fast"
    assert stored.state["data_mode"] == "demo"
    assert stored.state["marketplace_draft"]["mock"] is True
    assert app.runtime_store.approval_for_node(run["id"], "candidate_approval") is None
    assert app.runtime_store.approval_for_node(run["id"], "publish_approval") is None
    assert run["id"] not in {item["id"] for item in app.list_core_runs()}
    assert any(
        message["run_id"] == run["id"]
        for message in app.conversation(conversation_id)["messages"]
    )
    assert any("Mock" in payload["content"] for name, payload in events if name == "message"
               and payload["role"] == "assistant")


def test_home_fast_comic_complex_request_uses_existing_graph_without_start_confirmation(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.runner, "submit", lambda _execute: None)
    monkeypatch.setattr(
        web_studio, "plan_creative_turn",
        lambda content, _prior, **_kwargs: creative.CreativeDecision(
            action="chat", request=content,
        ),
    )
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    app.set_fast_domain(conversation["id"], "comic")
    events = list(app.stream_conversation(conversation["id"], {
        "content": "帮我制作三镜头的漫剧",
    }))
    assert next(payload for name, payload in events if name == "intent")["tool"] == "workflow.start"
    run = next(payload for name, payload in events if name == "run")
    assert run["workflow"] == "comic.production.v1"
    assert run["state"]["confirmed"] is False
    assert run["id"] in {
        item.id for item in app.runtime_store.list_runs(
            interaction_mode=InteractionMode.AUTONOMOUS,
        )
    }
    assert run["id"] not in {item["id"] for item in app.list_core_runs()}
    assert any("当前聊天" in payload["content"] for name, payload in events
               if name == "message" and payload["role"] == "assistant")


def test_fast_domain_http_switch_and_guided_rejection(
    server: int, app: web_studio.StudioApplication,
) -> None:
    home = app.create_conversation({"interaction_mode": "autonomous"})
    guided = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        for domain in ("comic", "commerce", "studio", None):
            connection.request(
                "PATCH", f"/api/conversations/{home['id']}",
                body=json.dumps({"fast_domain": domain}), headers=headers,
            )
            response = connection.getresponse()
            assert response.status == 200
            assert json.loads(response.read())["domain"] == domain
        connection.request(
            "PATCH", f"/api/conversations/{guided['id']}",
            body=json.dumps({"fast_domain": "commerce"}), headers=headers,
        )
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close()

def test_port_guard_blocks_second_instance() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(5)  #  backlog 太小会让第二次探测被拒，绕过守卫
    port = blocker.getsockname()[1]
    try:
        assert web_studio._port_already_serving(port) is True
        assert web_studio.serve(port=port, open_browser=False) == 2
    finally:
        blocker.close()
    assert web_studio._port_already_serving(port) is False


def test_startup_logs_loaded_config_and_image_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    base_url = "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    settings = SimpleNamespace(
        app=SimpleNamespace(name="Kantoku"),
        llm=SimpleNamespace(
            chat_api_key=lambda: "configured", model_chat="deepseek-chat",
            vision_api_key=lambda: "configured", model_vision="qwen3-vl-plus",
        ),
        image=SimpleNamespace(
            provider="alibaba-qwen-image", base_url=base_url, model="qwen-image-3.0",
            api_key=lambda: "configured",
        ),
    )
    messages: list[str] = []

    class CaptureLogger:
        def bind(self, **_extra: str) -> "CaptureLogger":
            return self

        def info(self, template: str, *args: object) -> None:
            messages.append(template.format(*args))

    monkeypatch.setattr(web_studio.os, "chdir", lambda _path: None)
    monkeypatch.setattr(web_studio, "_port_already_serving", lambda _port: False)
    monkeypatch.setattr(web_studio, "get_settings", lambda: settings)
    monkeypatch.setattr(web_studio, "StudioApplication", lambda: SimpleNamespace(
        runtime_store=SimpleNamespace(path=tmp_path / "db.sqlite"),
        runner=SimpleNamespace(close=lambda: None),
    ))
    monkeypatch.setattr(web_studio, "make_server", lambda _app, _port: SimpleNamespace(
        server_port=8001, serve_forever=lambda: None, server_close=lambda: None,
    ))
    monkeypatch.setattr(web_studio, "logger", CaptureLogger())

    assert web_studio.serve(port=8001, open_browser=False) == 0
    assert "config_path=" in messages[0]
    assert str(web_studio.CONFIG_PATH.resolve()) in messages[0]
    assert "image_provider=alibaba-qwen-image" in messages[0]
    assert f"image_base_url={base_url}" in messages[0]
    assert "image_model=qwen-image-3.0" in messages[0]
    assert "Qwen Image" in capsys.readouterr().out
