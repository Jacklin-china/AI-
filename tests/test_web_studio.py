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
from urllib.parse import quote

import pytest
from loguru import logger
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
from kantoku.core.runtime.models import ArtifactType, ExecutionStatus
from kantoku.core.runtime.store import RuntimeStore
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
    settings.search = SimpleNamespace(enabled=True, timeout_s=2, max_results=4)
    settings.budget.autonomous_image_auto_cny = Decimal("0.30")
    settings.budget.image_conversation_cny = Decimal("5.00")
    for module in (web_studio, studio, budget):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    for module in (review, report, archive):
        monkeypatch.setattr(module, "_database_path", lambda: settings.storage.sqlite_path)
    monkeypatch.setattr(archive, "_archive_root", lambda: tmp_path / "archive")
    provider = LocalFakeImageProvider(tmp_path / "output", model_id="test", actual_fen=30)
    monkeypatch.setattr(web_studio, "_provider", lambda: provider)
    monkeypatch.setattr(web_studio, "plan_web_search", lambda _content, **_kwargs: None)
    monkeypatch.setattr(web_studio, "_brief_deltas", lambda brief: iter([brief]))
    # Guided replies are part of these routing tests, not paid model acceptance.
    # Individual streaming tests replace this stub with their own public deltas.
    monkeypatch.setattr(web_studio, "stream_chat", lambda *_args, **_kwargs: iter(["离线引导回复"]))
    monkeypatch.setattr(
        web_studio, "_image_result_summary",
        lambda requirement, _prompt, _trace: f"已按你的要求生成一张{requirement}。",
    )
    application = web_studio.StudioApplication()
    # 离线测试显式替身；涉及创意隔离的测试单独提供语义边界结果。
    monkeypatch.setattr(application, "_comic_intent_model", lambda _messages: json.dumps({
        "new_creative_direction": False, "reason": "offline continuation fixture",
    }))
    return application


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
    web_studio.get_settings().budget.autonomous_image_auto_cny = Decimal("0.10")
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    events = list(app.stream_conversation(conversation["id"], {
        "content": "生成两张蜡笔小新头像",
        "generation_request_id": "generation-over-auto-test",
    }))
    assert not any(name in {"run", "activity"} for name, _ in events)
    assert next(payload for name, payload in events if name == "message")["type"] == "text"
    assert app.image_service.provider.submit_count == 0
    job = app.runtime_store.get_media_job("generation-over-auto-test")
    assert job.approval_status == "pending"
    assert job.estimate_fen == 30
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
    assert detail["domain"] is None
    assert detail["fast_domain_task_id"] is None
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
    assert app.conversation(conversation_id)["domain"] is None


def test_home_cost_confirmation_resumes_same_media_job(
    app: web_studio.StudioApplication,
) -> None:
    web_studio.get_settings().budget.autonomous_image_auto_cny = Decimal("0.10")
    conversation = app.create_conversation({"interaction_mode": "autonomous"})
    conversation_id = conversation["id"]
    app.set_fast_domain(conversation_id, "studio")
    request_id = "generation-cost-confirmation"
    events = list(app.stream_conversation(conversation_id, {
        "content": "画一只森林中的卡通小熊", "generation_request_id": request_id,
    }))
    assert any(name == "cost_approval" for name, _ in events)
    assert app.image_service.provider.submit_count == 0
    waiting = app.conversation(conversation_id)
    assert waiting["domain"] is None
    assert waiting["fast_domain_task_id"] is None
    assert waiting["media_jobs"][0]["approval_status"] == "pending"
    # A consumed skill cannot disable the menu while its own approval is pending.
    assert app.set_fast_domain(conversation_id, "comic")["domain"] == "comic"

    app.decide_media_cost(conversation_id, request_id, True)
    app._media_futures[request_id].result(timeout=10)
    complete = app.conversation(conversation_id)
    assert app.image_service.provider.submit_count == 1
    assert complete["domain"] == "comic"  # The older job must not clear a newer selection.
    assert complete["fast_domain_task_id"] is None
    assert complete["media_jobs"][0]["status"] == "completed"
    assert any(message["artifact_id"] for message in complete["messages"])
    with pytest.raises(ToolError):
        app.decide_media_cost(conversation_id, request_id, True)
    assert app.image_service.provider.submit_count == 1


def test_home_cost_rejection_never_submits(
    app: web_studio.StudioApplication,
) -> None:
    web_studio.get_settings().budget.autonomous_image_auto_cny = Decimal("0.10")
    conversation_id = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    list(app.stream_conversation(conversation_id, {
        "content": "画一只卡通小熊", "generation_request_id": "generation-cost-rejected",
    }))
    app.decide_media_cost(conversation_id, "generation-cost-rejected", False)
    assert app.image_service.provider.submit_count == 0
    assert app.conversation(conversation_id)["media_jobs"][0]["status"] == "failed"


def test_pending_cost_confirmation_survives_restart_without_submit(
    app: web_studio.StudioApplication,
) -> None:
    web_studio.get_settings().budget.autonomous_image_auto_cny = Decimal("0.10")
    conversation_id = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    request_id = "generation-approval-restart"
    list(app.stream_conversation(conversation_id, {
        "content": "画一只森林中的卡通小熊", "generation_request_id": request_id,
    }))
    restarted = web_studio.StudioApplication()
    assert restarted.image_service.provider.submit_count == 0
    assert restarted.conversation(conversation_id)["media_jobs"][0]["approval_status"] == "pending"
    restarted.decide_media_cost(conversation_id, request_id, True)
    restarted._media_futures[request_id].result(timeout=10)
    assert restarted.image_service.provider.submit_count == 1
    assert restarted.conversation(conversation_id)["media_jobs"][0]["status"] == "completed"


def test_home_over_twenty_requires_confirmation_then_executes(
    app: web_studio.StudioApplication,
) -> None:
    settings = web_studio.get_settings()
    settings.budget.autonomous_image_auto_cny = Decimal("20")
    settings.budget.autonomous_image_daily_cny = Decimal("100")
    settings.budget.image_conversation_cny = Decimal("100")
    settings.budget.image_estimated_cny_per_call = Decimal("20.01")
    conversation_id = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    request_id = "generation-over-twenty"
    events = list(app.stream_conversation(conversation_id, {
        "content": "画一只竹林里的卡通小熊", "generation_request_id": request_id,
    }))
    assert next(payload for name, payload in events if name == "cost_approval") == {
        "generation_request_id": request_id, "estimate_fen": 2001,
    }
    assert app.image_service.provider.submit_count == 0
    app.decide_media_cost(conversation_id, request_id, True)
    app._media_futures[request_id].result(timeout=10)
    assert app.image_service.provider.submit_count == 1
    assert app.conversation(conversation_id)["media_jobs"][0]["status"] == "completed"


def test_home_cost_confirmation_http_api(
    server: int, app: web_studio.StudioApplication,
) -> None:
    web_studio.get_settings().budget.autonomous_image_auto_cny = Decimal("0.10")
    conversation_id = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    request_id = "generation-http-cost"
    list(app.stream_conversation(conversation_id, {
        "content": "画一只卡通小熊", "generation_request_id": request_id,
    }))
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        connection.request(
            "POST", f"/api/conversations/{conversation_id}/media-jobs/{request_id}/approval",
            body=json.dumps({"decision": "reject"}),
            headers={"X-Studio-Token": app.token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["status"] == "failed"
        assert app.image_service.provider.submit_count == 0
    finally:
        connection.close()


def test_selected_fast_domain_does_not_leak_to_next_message(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.runner, "submit", lambda _execute: None)
    monkeypatch.setattr(web_studio, "stream_chat", lambda *_args, **_kwargs: iter(["你好。"]))
    monkeypatch.setattr(
        web_studio, "plan_creative_turn",
        lambda content, _prior, **_kwargs: creative.CreativeDecision(
            action="chat", request=content,
        ),
    )
    conversation_id = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    app.set_fast_domain(conversation_id, "comic")
    first = list(app.stream_conversation(conversation_id, {
        "content": "帮我制作三镜头漫剧",
    }))
    assert any(
        name == "public_activity" and payload["detail"] == "技能：漫剧创作"
        for name, payload in first
    )
    run = next(payload for name, payload in first if name == "run")
    # Selection is consumed at dispatch; the running task retains its own domain.
    assert app.conversation(conversation_id)["domain"] is None
    second = list(app.stream_conversation(conversation_id, {"content": "你好"}))
    assert next(payload for name, payload in second if name == "intent")["domain"] is None
    app.runtime_store.update_run(
        run["id"], status=ExecutionStatus.COMPLETED, state=run["state"],
        current_node="__end__",
    )
    assert app.conversation(conversation_id)["domain"] is None


def test_home_chat_reports_only_actual_web_search_sources(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kantoku.capabilities.web_search import SearchResult

    monkeypatch.setattr(web_studio, "plan_web_search", lambda _content, **_kwargs: "北京热点")
    monkeypatch.setattr(web_studio, "search_web", lambda _query, _settings: [
        SearchResult("北京新闻", "https://example.org/story", "公开摘要", "example.org"),
        SearchResult("未访问", "https://unvisited.example/story", "不得引用", "unvisited.example"),
    ])
    def visit(result: SearchResult, _settings: object) -> SearchResult:
        if result.domain == "unvisited.example":
            raise OSError("不可访问")
        return SearchResult(result.title, result.url, "实际网页内容", result.domain)

    monkeypatch.setattr(web_studio, "visit_search_result", visit)
    captured: list[list[dict[str, str]]] = []

    def answer(messages: list[dict[str, str]], **_kwargs: object) -> Iterator[str]:
        captured.append(messages)
        return iter(["已参考公开来源。"])

    monkeypatch.setattr(web_studio, "stream_chat", answer)
    conversation_id = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    events = list(app.stream_conversation(conversation_id, {"content": "北京今天有什么热点新闻？"}))
    intent = next(payload for name, payload in events if name == "intent")
    assert intent["user_message_id"] == next(
        message.id for message in app.runtime_store.list_conversation_messages(conversation_id)
        if message.role == MessageRole.USER
    )
    activities = [payload for name, payload in events if name == "public_activity"]
    assert [item["kind"] for item in activities] == [
        "search", "site_visited", "search_ready", "organizing", "search_complete",
    ]
    assert activities[0]["detail"] == "搜索：北京热点"
    assert activities[1]["detail"] == "访问：example.org"
    assert activities[-1]["label"] == "已搜索 1 个来源"
    assert "https://example.org/story" in captured[0][1]["content"]
    assert "unvisited.example" not in captured[0][1]["content"]
    kinds = [payload["kind"] if name == "public_activity" else name for name, payload in events]
    assert kinds.index("search") < kinds.index("site_visited")
    assert kinds.index("site_visited") < kinds.index("search_ready")
    assert kinds.index("organizing") < kinds.index("delta")
    assert kinds.index("delta") < kinds.index("search_complete")


def test_home_search_without_visited_sources_never_calls_answer_model(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kantoku.capabilities.web_search import SearchResult

    monkeypatch.setattr(web_studio, "plan_web_search", lambda _content, **_kwargs: "佛山天气")
    monkeypatch.setattr(web_studio, "search_web", lambda _query, _settings: [
        SearchResult("无法访问的网页", "https://example.org/weather", "摘要", "example.org"),
    ])
    def unavailable(_result: SearchResult, _settings: object) -> SearchResult:
        raise OSError("页面不可访问")

    monkeypatch.setattr(web_studio, "visit_search_result", unavailable)
    def must_not_answer(*_args: object, **_kwargs: object) -> Iterator[str]:
        raise AssertionError("没有真实来源时不得让模型凭记忆回答实时问题")

    monkeypatch.setattr(web_studio, "stream_chat", must_not_answer)
    conversation_id = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    events = list(app.stream_conversation(conversation_id, {"content": "佛山今天天气如何？"}))
    activity = [payload for name, payload in events if name == "public_activity"]
    assert activity[-2]["kind"] == "search_error"
    assert activity[-1]["kind"] == "search_complete"
    answer = next(payload["content"] for name, payload in events if name == "delta")
    assert "无法可靠回答" in answer
    assert "example.org" not in answer
    assert any(name == "message" and payload["content"] == answer for name, payload in events)


def test_home_plain_chat_has_no_search_activity(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_studio, "stream_chat", lambda *_args, **_kwargs: iter(["你好。"]))
    conversation_id = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    events = list(app.stream_conversation(conversation_id, {"content": "你好"}))
    assert not any(name == "public_activity" for name, _payload in events)
    assert any(name == "delta" and payload["content"] == "你好。" for name, payload in events)


def test_home_fast_comic_complex_request_uses_existing_graph_without_start_confirmation(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.runner, "submit", lambda _execute: None)
    # Execution routing is independent of a quote; this test explicitly supplies
    # a complete below-threshold estimate. Missing prices are tested separately.
    monkeypatch.setattr(app, "_quick_creation_cost", lambda _count, **_kwargs: (30, []))
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
    assert run["state"]["execution_mode"] == "fast"
    assert run["state"]["confirmed"] is True
    assert run["id"] in {
        item.id for item in app.runtime_store.list_runs(
            interaction_mode=InteractionMode.AUTONOMOUS,
        )
    }
    assert run["id"] not in {item["id"] for item in app.list_core_runs()}
    assert any("当前聊天" in payload["content"] for name, payload in events
               if name == "message" and payload["role"] == "assistant")


def test_home_fast_mode_infers_domain_without_opening_professional_workflow_page(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.runner, "submit", lambda _execute: None)
    monkeypatch.setattr(
        web_studio, "plan_creative_turn",
        lambda content, _prior, **_kwargs: creative.CreativeDecision(
            action="chat", request=content,
        ),
    )
    home = app.create_conversation({"interaction_mode": "autonomous"})
    assert home["execution_mode"] == "fast"
    events = list(app.stream_conversation(home["id"], {
        "content": "帮我制作三镜头漫剧",
    }))
    intent = next(payload for name, payload in events if name == "intent")
    assert intent["tool"] == "workflow.start"
    assert intent["domain"] == "comic"
    assert intent["execution_mode"] == "fast"
    assert app.conversation(home["id"])["domain"] is None
    assert any(name == "run" for name, _ in events)

    guided = app.create_conversation({"interaction_mode": "guided", "domain": "comic"})
    assert guided["execution_mode"] == "professional"
    guided_events = list(app.stream_conversation(guided["id"], {
        "content": "帮我制作三镜头漫剧",
    }))
    assert not any(name == "run" for name, _ in guided_events)


def test_home_explicit_image_request_still_generates_when_creative_model_misses_intent(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_studio, "plan_creative_turn",
        lambda content, _prior, **_kwargs: creative.CreativeDecision(
            action="chat", request=content,
        ),
    )
    home = app.create_conversation({"interaction_mode": "autonomous"})
    events = list(app.stream_conversation(home["id"], {
        "content": "画一个竹林里的卡通剑士",
    }))
    assert next(payload for name, payload in events if name == "intent")["tool"] == "image.generate"
    assert any(name == "image_ready" for name, _ in events)
    assert not any(name == "run" for name, _ in events)
    assert app.conversation(home["id"])["domain"] is None


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
    loaded_config = web_studio.CONFIG_PATH if web_studio.CONFIG_PATH.exists() \
        else web_studio.EXAMPLE_PATH
    assert str(loaded_config.resolve()) in messages[0]
    assert "image_provider=alibaba-qwen-image" in messages[0]
    assert f"image_base_url={base_url}" in messages[0]
    assert "image_model=qwen-image-3.0" in messages[0]
    assert "Qwen Image" in capsys.readouterr().out


def test_comic_project_api_persists_brief_versions_and_context(
    server: int, app: web_studio.StudioApplication,
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}

    def request(
        method: str, path: str, payload: dict[str, object] | None = None,
        *, authorized: bool = True,
    ) -> tuple[int, dict[str, object]]:
        connection.request(
            method, path, body=None if payload is None else json.dumps(payload),
            headers=headers if authorized else {},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    try:
        status, _ = request("GET", "/comic/projects/missing", authorized=False)
        assert status == 403
        status, created = request("POST", "/comic/projects", {
            "title": "东方仙侠少女雨夜战斗漫画",
            "brief": {
                "original_request": "东方仙侠少女雨夜战斗漫画",
                "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
                "soft_preferences": ["电影感", "冷色调"],
                "creative_freedom": ["构图优化", "光影优化", "镜头设计"],
            },
        })
        assert status == 201
        project = created["project"]
        assert isinstance(project, dict)
        project_id = project["project_id"]
        assert isinstance(project_id, str)
        assert project["current_version"] == 1
        assert created["creative_brief"]["hard_constraints"] == [
            "东方仙侠", "少女", "雨夜", "战斗",
        ]

        status, context = request(
            "GET", f"/api/comic/projects/{project_id}/context?task=plan",
        )
        assert status == 200
        assert context["current_task"] == "plan"
        assert context["source_versions"] == {"project": 1, "creative_brief": 1}
        assert context["relevant_memory"] == []

        status, updated = request("PUT", f"/comic/projects/{project_id}/brief", {
            "expected_version": 1,
            "original_request": "增加白鹤的雨夜战斗",
            "hard_constraints": ["少女", "雨夜", "白鹤"],
            "soft_preferences": ["冷色调"],
            "creative_freedom": ["镜头设计"],
        })
        assert status == 200
        assert updated["project"]["current_version"] == 2
        status, old = request("GET", f"/api/comic/projects/{project_id}?version=1")
        assert status == 200
        assert old == created
        status, current = request("GET", f"/comic/projects/{project_id}")
        assert status == 200
        assert current == updated

        status, conflict = request("PUT", f"/api/comic/projects/{project_id}/brief", {
            "expected_version": 1,
            "original_request": "过期修改",
        })
        assert status == 400
        assert "刷新" in conflict["error"]
        assert app.runtime_store.list_runs() == []
    finally:
        connection.close()


def test_comic_director_api_generates_edits_lists_and_restores(
    server: int, app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}

    def request(
        method: str, path: str, payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        connection.request(
            method, path, body=None if payload is None else json.dumps(payload), headers=headers,
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    draft: dict[str, object] = {
        "visual_direction": "东方仙侠电影感，突出人物在雨夜的孤独",
        "storytelling_goal": "展现少女独自迎战的决心",
        "camera_language": "远景建立环境，再靠近人物情绪",
        "composition": "让环境空间强化人物与世界关系",
        "lighting": "冷色雨光与人物局部暖光",
        "color_language": "冷色背景，少量暖色引导视线",
        "emotion": "孤独与战斗张力",
        "character_focus": "少女的仙侠身份和行动决心",
        "constraints": ["东方仙侠"],
        "creative_choices": ["先交代环境，再靠近人物以突出情绪转折。"],
    }
    seen: list[list[dict[str, str]]] = []

    def model_call(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return json.dumps(draft, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_director_model", model_call)
    try:
        status, created = request("POST", "/api/comic/projects", {
            "title": "东方仙侠少女雨夜战斗场景",
            "brief": {
                "original_request": "制作一个东方仙侠少女雨夜战斗场景",
                "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
            },
        })
        assert status == 201
        project_id = created["project"]["project_id"]
        status, first = request(
            "POST", f"/api/comic/projects/{project_id}/director-spec",
            {"expected_project_version": 1, "task": "设计雨夜战斗"},
        )
        assert status == 201
        assert first["source"] == "model"
        assert first["creative_brief_version"] == 1
        assert first["constraints"] == ["东方仙侠", "少女", "雨夜", "战斗"]
        assert "设计雨夜战斗" in seen[0][1]["content"]

        edited = {**first, "lighting": "更柔和的雨夜侧光"}
        edited = {key: value for key, value in edited.items() if key in draft}
        status, second = request(
            "POST", f"/comic/projects/{project_id}/director-spec",
            {"expected_project_version": 2, "draft": edited},
        )
        assert status == 201
        assert second["version"] == 2
        assert second["source"] == "manual"
        status, current = request("GET", f"/api/comic/projects/{project_id}/director-spec")
        assert status == 200
        assert current == second
        status, versions = request(
            "GET", f"/api/comic/projects/{project_id}/director-spec/versions",
        )
        assert status == 200
        assert [item["version"] for item in versions["versions"]] == [2, 1]
        status, restored = request(
            "POST", f"/api/comic/projects/{project_id}/director-spec/restore",
            {"expected_project_version": 3, "version": 1},
        )
        assert status == 201
        assert restored["version"] == 3
        assert restored["restored_from_version"] == 1
        assert restored["lighting"] == first["lighting"]
        tasks = app.list_comic_project_tasks(project_id)["tasks"]
        assert len(tasks) == 2
        assert all(item["status"] == "completed" for item in tasks)
        assert all(item["state"]["last_completed_step"] == "director_spec_saved"
                   for item in tasks)
        assert app.list_core_runs() == []  # lightweight director tracking is not a production Run
    finally:
        connection.close()


def test_comic_director_failure_logs_and_survives_restart(
    server: int, app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {
        "X-Studio-Token": app.token, "Content-Type": "application/json",
        "X-Trace-ID": "trace-director-failure",
    }
    records: list[dict[str, object]] = []
    sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
    model_calls: list[object] = []

    def invalid_model(messages: object) -> str:
        model_calls.append(messages)
        return "not json"

    monkeypatch.setattr(app, "_comic_director_model", invalid_model)
    try:
        connection.request("POST", "/api/comic/projects", body=json.dumps({
            "title": "雨夜战斗", "brief": {"original_request": "少女在雨夜战斗"},
        }), headers=headers)
        response = connection.getresponse()
        project_id = json.loads(response.read())["project"]["project_id"]
        connection.request(
            "POST", f"/api/comic/projects/{project_id}/director-spec",
            body=json.dumps({"expected_project_version": 1, "task": "设计战斗镜头"}),
            headers=headers,
        )
        response = connection.getresponse()
        failure = json.loads(response.read())
        assert response.status == 400
        assert failure["error_id"].startswith("ERR-")
        assert failure["trace_id"] == "trace-director-failure"
        runs = [
            run for run in RuntimeStore(app.runtime_store.path).list_runs(domain="comic")
            if run.workflow == "comic.director-spec"
        ]
        assert len(runs) == 1
        run = runs[0]
        assert run.status is ExecutionStatus.FAILED
        assert run.state["project_id"] == project_id
        assert run.state["task_id"].startswith("task-")
        assert run.state["creative_brief_version"] == 1
        assert run.state["last_completed_step"] == "assets_selected"
        assert run.state["error_id"] == failure["error_id"]
        assert RuntimeStore(app.runtime_store.path).list_events(run.id)[-1].event_type.value \
            == "run_failed"
        error_records = [item for item in records if item["extra"].get("error_id")
                         == failure["error_id"]]
        assert len(error_records) == 1
        assert error_records[0]["extra"]["project_id"] == project_id
        assert error_records[0]["extra"]["run_id"] == run.id
        assert "director.py" in error_records[0]["message"]
        connection.request("GET", f"/api/comic/projects/{project_id}/tasks", headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["tasks"][0]["id"] == run.id
        restarted = web_studio.StudioApplication()
        assert restarted.list_comic_project_tasks(project_id)["tasks"][0]["state"]["error_id"] \
            == failure["error_id"]
        assert len(model_calls) == 1  # querying after restart must not submit again
    finally:
        logger.remove(sink)
        connection.close()


def test_comic_missing_asset_error_identifies_project_and_asset(
    server: int, app: web_studio.StudioApplication,
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}
    records: list[dict[str, object]] = []
    sink = logger.add(lambda message: records.append(message.record), level="ERROR")
    try:
        connection.request("POST", "/api/comic/projects", body=json.dumps({
            "title": "竹林", "brief": {"original_request": "竹林里的角色"},
        }), headers=headers)
        response = connection.getresponse()
        project_id = json.loads(response.read())["project"]["project_id"]
        connection.request(
            "GET", f"/api/comic/projects/{project_id}/assets/missing-asset", headers=headers,
        )
        response = connection.getresponse()
        failure = json.loads(response.read())
        assert response.status == 400
        match = next(item for item in records
                     if item["extra"].get("error_id") == failure["error_id"])
        assert match["extra"]["project_id"] == project_id
        assert match["extra"]["asset_id"] == "missing-asset"
        assert "assets.py" in match["message"]
    finally:
        logger.remove(sink)
        connection.close()


def test_comic_director_interrupted_task_keeps_checkpoint_without_resubmit(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = app.create_comic_project({
        "title": "雨夜镜头", "brief": {"original_request": "雨夜中的少女"},
    })
    project_id = created["project"]["project_id"]
    calls: list[object] = []

    def interrupted(messages: object) -> str:
        calls.append(messages)
        raise SystemExit("simulated process stop")

    monkeypatch.setattr(app, "_comic_director_model", interrupted)
    with pytest.raises(SystemExit):
        app.create_comic_director(project_id, {"expected_project_version": 1})

    restarted = web_studio.StudioApplication()
    task = restarted.list_comic_project_tasks(project_id)["tasks"][0]
    assert task["status"] == "running"
    assert task["state"]["task_status"] == "generating"
    assert task["state"]["last_completed_step"] == "assets_selected"
    assert task["state"]["trace_id"].startswith("trace-")
    assert task["recovery_required"] is True
    assert len(calls) == 1


def _comic_planning_project(app: web_studio.StudioApplication) -> tuple[str, dict[str, str]]:
    created = app.create_comic_project({
        "title": "东方仙侠少女雨夜战斗",
        "brief": {
            "original_request": "制作东方仙侠少女雨夜竹林战斗漫画",
            "hard_constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
        },
    })
    project_id = created["project"]["project_id"]
    app.create_comic_director(project_id, {
        "expected_project_version": 1,
        "draft": {
            "visual_direction": "以雨夜竹林的空间与人物行动建立张力",
            "storytelling_goal": "展示少女从警觉到迎战的转折",
            "camera_language": "镜头距离随着行动节奏变化",
            "composition": "利用竹林深度引导视线",
            "lighting": "雨夜环境光与角色局部反光",
            "color_language": "冷色环境与剑光形成层次",
            "emotion": "警觉而坚定",
            "character_focus": "少女的决心和动作",
            "constraints": ["东方仙侠", "少女", "雨夜", "战斗"],
            "creative_choices": ["环境先建立风险，再通过拔剑表现决断。"],
        },
    })
    assets: dict[str, str] = {}
    for kind, name, details in (
        ("character", "阿青", {"kind": "character", "appearance": "黑发少女剑士"}),
        ("scene", "竹林", {"kind": "scene", "location": "雨夜竹林"}),
        ("style", "电影风格", {"kind": "style", "art_direction": "东方仙侠电影感"}),
    ):
        version = app.comic_projects.get(project_id).project.current_version
        asset = app.create_comic_asset(project_id, {
            "expected_project_version": version,
            "asset": {"name": name, "details": details},
        })
        assets[kind] = asset["asset_id"]
    return project_id, assets


def test_comic_prompt_http_compile_edit_restore_and_history(
    server: int, app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, assets = _comic_planning_project(app)
    web_studio.get_settings().image.model = "qwen-image-3.0"
    board = app.create_comic_storyboard(project_id, {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "draft": {"title": "雨夜交锋"},
    })["storyboard"]
    shot = app.create_comic_shot(board["storyboard_id"], {
        "expected_project_version": app.comic_projects.get(project_id).project.current_version,
        "expected_storyboard_version": 1,
        "shot": {
            "purpose": "展现拔剑决心", "subject": "少女阿青", "action": "拔剑",
            "environment": "雨夜竹林",
            "character_asset_versions": [{"asset_id": assets["character"], "version": 1}],
            "scene_asset_versions": [{"asset_id": assets["scene"], "version": 1}],
            "style_version": {"asset_id": assets["style"], "version": 1},
        },
    })
    calls: list[object] = []

    def model_call(messages: object) -> str:
        calls.append(messages)
        return json.dumps({
            "director_summary": "雨夜拔剑前的克制张力",
            "positive_prompt": "东方仙侠 少女 雨夜 战斗 竹林拔剑，阿青保持黑发剑士形象",
            "negative_prompt": "避免身份漂移",
        }, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_storyboard_model", model_call)
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}

    def request(method: str, path: str, body: dict[str, object] | None = None):
        connection.request(
            method, path, body=None if body is None else json.dumps(body), headers=headers,
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    try:
        prefix = f"/api/comic/shots/{shot['shot_id']}/prompt"
        status, first = request("POST", prefix + "/compile", {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "expected_shot_version": 1,
        })
        assert status == 201
        assert first["version"] == 1
        assert first["model_target"] == "qwen-image-3.0"
        assert len(calls) == 1
        assert app.runtime_store.get_artifact(first["artifact_id"]).type == ArtifactType.PROMPT
        status, fetched = request("GET", prefix)
        assert status == 200 and fetched == first
        status, second = request("PUT", prefix, {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "expected_version": 1,
            "draft": {
                "director_summary": "略加强动作",
                "positive_prompt": first["positive_prompt"] + "，剑尖溅起雨滴",
                "negative_prompt": first["negative_prompt"],
            },
        })
        assert status == 200 and second["version"] == 2
        status, versions = request("GET", prefix + "/versions")
        assert status == 200 and [p["version"] for p in versions["versions"]] == [2, 1]
        status, restored = request("POST", prefix + "/restore", {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "expected_version": 2, "version": 1,
        })
        assert status == 201 and restored["version"] == 3
        assert restored["positive_prompt"] == first["positive_prompt"]
        assert restored["restored_from_version"] == 1
        current_shot = app.comic_storyboards.get_shot(shot["shot_id"])
        revised_shot = app.edit_comic_shot(shot["shot_id"], {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "expected_version": current_shot.version, "status": "planned",
            "shot": {**current_shot.model_dump(include={
                "purpose", "subject", "action", "environment", "emotion",
                "shot_size", "camera_angle", "camera_movement",
                "character_asset_versions", "scene_asset_versions", "style_version",
            }), "action": "拔剑后迎击"},
        })
        status, revised_prompt = request("POST", prefix + "/compile", {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "expected_shot_version": revised_shot["version"],
        })
        assert status == 201 and revised_prompt["version"] == 4
        assert revised_prompt["shot_version"] == revised_shot["version"]
        status, historical = request("GET", prefix + "?version=1")
        assert status == 200 and historical == first
        status, stale = request("POST", prefix + "/restore", {
            "expected_project_version": app.comic_projects.get(project_id).project.current_version,
            "expected_version": 4, "version": 1,
        })
        assert status == 400 and stale["error_id"].startswith("ERR-")
        assert app.runtime_store.list_artifacts(type=ArtifactType.IMAGE) == []
        assert app.list_core_runs() == []
        assert any(task["workflow"] == "comic.prompt.compile" for task in
                   app.list_comic_project_tasks(project_id)["tasks"])
    finally:
        connection.close()


def test_comic_storyboard_ai_plan_versions_assets_and_recovery(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, assets = _comic_planning_project(app)
    seen: list[list[dict[str, str]]] = []
    refs = {
        "character_asset_versions": [{"asset_id": assets["character"], "version": 1}],
        "scene_asset_versions": [{"asset_id": assets["scene"], "version": 1}],
        "style_version": {"asset_id": assets["style"], "version": 1},
    }
    plan = {
        "title": "雨夜竹林交锋", "description": "由警觉过渡到拔剑",
        "shots": [
            {"purpose": "建立危险环境", "subject": "阿青站在竹林里",
             "action": "听见竹叶异动", "environment": "雨夜竹林", "emotion": "警觉",
             "shot_size": "远景", "camera_angle": "略低机位", "camera_movement": "缓推",
             **refs},
            {"purpose": "表现战斗爆发", "subject": "阿青拔剑",
             "action": "拔剑迎敌", "environment": "雨夜竹林", "emotion": "坚定",
             "shot_size": "近景", "camera_angle": "平视", "camera_movement": "跟随",
             **refs},
        ],
    }

    def model_call(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        return json.dumps(plan, ensure_ascii=False)

    monkeypatch.setattr(app, "_comic_storyboard_model", model_call)
    version = app.comic_projects.get(project_id).project.current_version
    result = app.create_comic_storyboard(project_id, {
        "expected_project_version": version, "generate": True,
        "task": "规划阿青在雨夜竹林交锋的节奏",
        "asset_ids": list(assets.values()),
    })
    storyboard = result["storyboard"]
    shots = result["shots"]
    assert len(shots) == 2
    assert storyboard["director_spec_version"] == 1
    assert [shot["sequence_number"] for shot in shots] == [1, 2]
    assert shots[0]["character_asset_versions"] == refs["character_asset_versions"]
    assert shots[1]["purpose"] == "表现战斗爆发"
    assert "director_spec" in seen[0][1]["content"]
    assert len(app.comic_storyboards.list_shots(storyboard["storyboard_id"])) == 2
    runs = app.list_comic_project_tasks(project_id)["tasks"]
    assert any(item["workflow"] == "comic.storyboard.create" and
               item["status"] == "completed" for item in runs)
    assert app.list_core_runs() == []

    asset_version = app.comic_projects.get(project_id).project.current_version
    app.edit_comic_asset(project_id, assets["character"], {
        "expected_project_version": asset_version, "expected_asset_version": 1,
        "asset": {"name": "阿青", "details": {
            "kind": "character", "appearance": "黑发少女剑士", "outfit": "新增披风",
        }},
    })
    assert app.comic_storyboards.get_shot(shots[0]["shot_id"]).character_asset_versions[
        0
    ].version == 1

    shot_id = shots[0]["shot_id"]
    version = app.comic_projects.get(project_id).project.current_version
    revised = app.edit_comic_shot(shot_id, {
        "expected_project_version": version, "expected_version": 1,
        "status": "planned",
        "shot": {**{key: shots[0][key] for key in (
            "purpose", "subject", "action", "environment", "emotion", "shot_size",
            "camera_angle", "camera_movement", "character_asset_versions",
            "scene_asset_versions", "style_version",
        )}, "action": "转身拔剑"},
    })
    assert revised["version"] == 2
    assert app.comic_storyboards.shot_versions(shot_id)[-1].action == "听见竹叶异动"
    version = app.comic_projects.get(project_id).project.current_version
    reordered = app.edit_comic_storyboard(storyboard["storyboard_id"], {
        "expected_project_version": version, "expected_version": 1,
        "draft": {"title": "雨夜竹林交锋", "description": "节奏调整"},
        "status": "planning", "shot_ids": [shots[1]["shot_id"], shot_id],
    })
    assert reordered["shot_ids"] == [shots[1]["shot_id"], shot_id]
    assert [shot.sequence_number for shot in app.comic_storyboards.list_shots(
        storyboard["storyboard_id"]
    )] == [1, 2]
    version = app.comic_projects.get(project_id).project.current_version
    restored = app.restore_comic_storyboard(storyboard["storyboard_id"], {
        "expected_project_version": version, "expected_version": 2, "version": 1,
    })
    assert restored["version"] == 3
    assert restored["shot_ids"] == storyboard["shot_ids"]

    version = app.comic_projects.get(project_id).project.current_version
    current_shot = app.comic_storyboards.get_shot(shot_id)
    deleted = app.change_comic_shot(shot_id, "delete", {
        "expected_project_version": version, "expected_version": current_shot.version,
    })
    assert deleted["status"] == "deleted"
    assert len(app.comic_storyboards.list_shots(storyboard["storyboard_id"])) == 1
    version = app.comic_projects.get(project_id).project.current_version
    revived = app.change_comic_shot(shot_id, "restore", {
        "expected_project_version": version, "expected_version": deleted["version"],
        "version": 2,
    })
    assert revived["shot_id"] == shot_id
    assert revived["source"] == "restored"
    assert len(app.comic_storyboards.list_shots(storyboard["storyboard_id"])) == 2
    restarted = web_studio.StudioApplication()
    assert len(restarted.comic_storyboards.list_shots(storyboard["storyboard_id"])) == 2
    assert any(item["workflow"] == "comic.storyboard.create"
               for item in restarted.list_comic_project_tasks(project_id)["tasks"])


def test_comic_storyboard_http_routes_and_asset_validation(
    server: int, app: web_studio.StudioApplication,
) -> None:
    project_id, assets = _comic_planning_project(app)
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}

    def request(method: str, path: str, body: dict[str, object] | None = None):
        connection.request(method, path, body=json.dumps(body) if body else None, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    try:
        version = app.comic_projects.get(project_id).project.current_version
        status, created = request("POST", f"/api/comic/projects/{project_id}/storyboards", {
            "expected_project_version": version,
            "draft": {"title": "雨夜竹林", "description": "两镜头规划"},
        })
        assert status == 201
        storyboard_id = created["storyboard"]["storyboard_id"]
        assert created["shots"] == []
        status, listed = request("GET", f"/api/comic/projects/{project_id}/storyboards")
        assert status == 200
        assert listed["storyboards"][0]["storyboard_id"] == storyboard_id
        version = app.comic_projects.get(project_id).project.current_version
        status, shot = request("POST", f"/api/comic/storyboards/{storyboard_id}/shots", {
            "expected_project_version": version, "expected_storyboard_version": 1,
            "shot": {"purpose": "建立场景", "subject": "阿青与竹林",
                     "character_asset_versions": [
                         {"asset_id": assets["character"], "version": 1}],
                     "scene_asset_versions": [
                         {"asset_id": assets["scene"], "version": 1}],
                     "style_version": {"asset_id": assets["style"], "version": 1}},
        })
        assert status == 201
        assert shot["sequence_number"] == 1
        status, shots = request("GET", f"/api/comic/storyboards/{storyboard_id}/shots")
        assert status == 200
        assert shots["shots"][0]["shot_id"] == shot["shot_id"]
        status, loaded = request("GET", f"/api/comic/storyboards/{storyboard_id}")
        assert status == 200 and loaded["storyboard"]["version"] == 2
        status, edited_shot = request("PUT", f"/api/comic/shots/{shot['shot_id']}", {
            "expected_project_version": version + 1, "expected_version": 1,
            "status": "planned", "shot": {
                "purpose": "建立场景", "subject": "阿青拔剑",
                "character_asset_versions": [
                    {"asset_id": assets["character"], "version": 1}],
                "scene_asset_versions": [{"asset_id": assets["scene"], "version": 1}],
                "style_version": {"asset_id": assets["style"], "version": 1},
            },
        })
        assert status == 200 and edited_shot["version"] == 2
        status, edited_board = request("PUT", f"/api/comic/storyboards/{storyboard_id}", {
            "expected_project_version": version + 2, "expected_version": 2,
            "draft": {"title": "雨夜竹林新版", "description": "调整叙事"},
            "status": "planning",
        })
        assert status == 200 and edited_board["version"] == 3
        status, restored = request(
            "POST", f"/api/comic/storyboards/{storyboard_id}/restore",
            {"expected_project_version": version + 3, "expected_version": 3,
             "version": 2},
        )
        assert status == 201 and restored["title"] == "雨夜竹林"
        status, board_versions = request(
            "GET", f"/api/comic/storyboards/{storyboard_id}/versions",
        )
        assert status == 200 and len(board_versions["versions"]) == 4
        status, versions = request("GET", f"/api/comic/shots/{shot['shot_id']}/versions")
        assert status == 200 and len(versions["versions"]) == 2
        status, invalid = request("POST", f"/api/comic/storyboards/{storyboard_id}/shots", {
            "expected_project_version": version + 4, "expected_storyboard_version": 4,
            "shot": {"purpose": "非法引用", "subject": "不存在角色",
                     "character_asset_versions": [{"asset_id": "missing", "version": 1}]},
        })
        assert status == 400
        assert invalid["error_id"].startswith("ERR-")
        assert len(app.comic_storyboards.list_shots(storyboard_id)) == 1
        assert any(
            item["state"].get("error_id") == invalid["error_id"]
            for item in app.list_comic_project_tasks(project_id)["tasks"]
        )
    finally:
        connection.close()


def test_comic_storyboard_rejects_invented_assets_and_stale_versions(
    app: web_studio.StudioApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, assets = _comic_planning_project(app)
    original_version = app.comic_projects.get(project_id).project.current_version
    monkeypatch.setattr(app, "_comic_storyboard_model", lambda _messages: json.dumps({
        "title": "无效规划", "shots": [{
            "purpose": "建立场景", "subject": "阿青",
            "character_asset_versions": [{"asset_id": assets["character"], "version": 1}],
            "scene_asset_versions": [{"asset_id": assets["scene"], "version": 1}],
            "style_version": {"asset_id": "invented-style", "version": 1},
        }],
    }, ensure_ascii=False))
    with pytest.raises(ToolError, match="上下文之外"):
        app.create_comic_storyboard(project_id, {
            "expected_project_version": original_version, "generate": True,
            "asset_ids": list(assets.values()),
        })
    assert app.comic_projects.get(project_id).project.current_version == original_version
    assert app.comic_storyboards.list(project_id) == []
    failed = app.list_comic_project_tasks(project_id)["tasks"][0]
    assert failed["status"] == "failed"
    assert failed["state"]["error_id"].startswith("ERR-")

    created = app.create_comic_storyboard(project_id, {
        "expected_project_version": original_version,
        "draft": {"title": "手动规划"},
    })["storyboard"]
    storyboard_id = created["storyboard_id"]
    with pytest.raises(ToolError, match="刷新"):
        app.create_comic_shot(storyboard_id, {
            "expected_project_version": original_version,
            "expected_storyboard_version": 1,
            "shot": {"purpose": "过期镜头", "subject": "阿青"},
        })
    assert app.comic_storyboards.list_shots(storyboard_id) == []

    current = app.comic_projects.get(project_id)
    app.update_comic_brief(project_id, {
        "expected_version": current.project.current_version,
        "original_request": "改成雪夜战斗", "hard_constraints": ["雪夜"],
    })
    with pytest.raises(ToolError, match="导演方案"):
        app.create_comic_shot(storyboard_id, {
            "expected_project_version": current.project.current_version + 1,
            "expected_storyboard_version": 1,
            "shot": {"purpose": "旧方案镜头", "subject": "阿青"},
        })


def test_comic_asset_api_versions_and_bounded_context(
    server: int, app: web_studio.StudioApplication,
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=5)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}

    def request(
        method: str, path: str, payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        connection.request(
            method, path, body=None if payload is None else json.dumps(payload), headers=headers,
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    try:
        status, created = request("POST", "/api/comic/projects", {
            "title": "竹林雨夜", "brief": {"original_request": "阿青在竹林雨夜迎战"},
        })
        assert status == 201
        project_id = created["project"]["project_id"]
        asset = {
            "name": "阿青", "details": {
                "kind": "character", "appearance": "黑发少年剑士", "outfit": "蓝色长袍",
            },
        }
        status, first = request("POST", f"/api/comic/projects/{project_id}/assets", {
            "expected_project_version": 1, "asset": asset,
        })
        assert status == 201
        asset_id = first["asset_id"]
        status, context = request(
            "GET", f"/api/comic/projects/{project_id}/context?task={quote('阿青 行动')}",
        )
        assert status == 200
        assert len(context["relevant_memory"]) == 1
        assert context["source_versions"][f"asset:{asset_id}"] == 1
        status, old_context = request("GET", f"/api/comic/projects/{project_id}/context?version=1")
        assert status == 200
        assert old_context["relevant_memory"] == []

        status, changed = request("PUT", f"/api/comic/projects/{project_id}/assets/{asset_id}", {
            "expected_project_version": 2, "expected_asset_version": 1,
            "asset": {**asset, "details": {**asset["details"], "outfit": "深色披风"}},
        })
        assert status == 200
        assert changed["version"] == 2
        status, locked = request(
            "POST", f"/api/comic/projects/{project_id}/assets/{asset_id}/lock",
            {"expected_project_version": 3, "expected_asset_version": 2, "version": 1},
        )
        assert status == 201
        assert locked["pinned_version"] == 1
        status, context = request(
            "GET", f"/api/comic/projects/{project_id}/context?asset_id={asset_id}",
        )
        assert status == 200
        assert context["source_versions"][f"asset:{asset_id}"] == 1
        status, versions = request(
            "GET", f"/api/comic/projects/{project_id}/assets/{asset_id}/versions",
        )
        assert status == 200
        assert [item["version"] for item in versions["versions"]] == [3, 2, 1]
        status, listed = request("GET", f"/api/comic/projects/{project_id}/assets")
        assert status == 200
        assert [item["asset_id"] for item in listed["assets"]] == [asset_id]
        assert app.runtime_store.list_runs() == []
    finally:
        connection.close()
