"""Homepage selections dispatch real shared workflows, not prompt labels."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest
from test_comic_director_coordinator import SKILLS, _parts
from test_web_studio import app as app_fixture
from test_web_studio import server as server_fixture

from kantoku.capabilities import creative
from kantoku.config import ToolError
from kantoku.config.settings import TextTokenPricing
from kantoku.core.conversations import InteractionMode
from kantoku.core.runtime.graph import RuntimeContext
from kantoku.core.runtime.models import ArtifactType, ExecutionStatus
from kantoku.core.runtime.runner import TaskRunner
from kantoku.domains.comic import services
from kantoku.domains.comic import workflow as comic_workflow
from kantoku.domains.comic.models import ComicState
from kantoku.schemas.qc import QcResult
from kantoku.shells import web_studio

app = app_fixture
server = server_fixture


@pytest.fixture
def production(
    app: web_studio.StudioApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[str]]:
    settings = web_studio.get_settings()
    settings.image.model = "qwen-image-test"
    settings.image.provider = "explicit-offline-provider"
    settings.budget.autonomous_image_auto_cny = Decimal("20")
    settings.llm.pricing_cny_per_million_by_model = {
        model: TextTokenPricing(input_cny=Decimal("0.01"), output_cny=Decimal("0.01"))
        for model in ("text-test", "vision-test")
    }
    monkeypatch.setattr(comic_workflow, "get_settings", lambda: settings)
    calls: list[str] = []

    def director(messages: list[dict[str, str]]) -> str:
        instruction = messages[0]["content"]
        if "根据用户修改指令编辑当前导演草稿" in instruction:
            calls.append("revision")
            payload = json.loads(messages[1]["content"])
            draft = payload["current_draft"]
            draft["director_plan"]["composition_strategy"] = payload["revision_instruction"]
            return json.dumps(draft, ensure_ascii=False)
        for skill, name in zip(
            SKILLS[:3],
            (
                "CreativeDecision",
                "DirectorPlan",
                "CinematographyPlan",
            ),
            strict=True,
        ):
            if f'"title": "{name}"' in instruction:
                calls.append(skill)
                payload = json.loads(messages[1]["content"])
                request = payload["context"]["current_task"]
                part = next(iter(_parts()[skill].values()))
                if skill == SKILLS[0]:
                    part.update(
                        intent_summary=request,
                        narrative_context=request,
                        narrative_focus=request,
                        hard_constraints=[],
                    )
                if skill == SKILLS[1]:
                    part.update(
                        visual_strategy=f"保持本次主体与场景关系：{request}", visual_focus=request
                    )
                return json.dumps(part, ensure_ascii=False)
        calls.append("comic.director_critic")
        return json.dumps(
            {
                "public_summary": "公开方案有视觉依据。",
                "confidence": 0.9,
                "findings": [],
                "suggested_patches": [],
            },
            ensure_ascii=False,
        )

    def planning(messages: list[dict[str, str]]) -> str:
        instruction = messages[0]["content"]
        if "positive_prompt" in instruction:
            calls.append("prompt")
            return json.dumps(
                {
                    "director_summary": "使用当前导演方案",
                    "positive_prompt": "当前主体与环境，保留导演规划的视觉关系",
                    "negative_prompt": "不改变主体",
                },
                ensure_ascii=False,
            )
        calls.append("storyboard")
        return json.dumps(
            {
                "title": "关键画面",
                "description": "当前需求单镜头",
                "shots": [
                    {
                        "purpose": "表达当前创意",
                        "subject": "当前角色",
                        "action": "打电话",
                        "environment": "室内",
                    }
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(app, "_comic_director_model", director)
    monkeypatch.setattr(
        web_studio,
        "chat",
        lambda *_args, **_kwargs: pytest.fail(
            "Offline acceptance must never call a paid text model"
        ),
    )
    monkeypatch.setattr(
        "kantoku.core.llm._build_client",
        lambda **_kwargs: pytest.fail(
            "Offline acceptance must never construct a real provider client"
        ),
    )
    monkeypatch.setattr(app, "_comic_storyboard_model", planning)
    monkeypatch.setattr(app.runner, "submit", lambda execute: execute())
    monkeypatch.setattr(
        services,
        "qc_image",
        lambda *_args, **_kwargs: QcResult(
            broken_hands=False,
            watermark=False,
            composition_ok=True,
            persona_consistency=5,
            confidence=0.9,
            reason="Explicit offline QC fixture",
        ),
    )
    monkeypatch.setattr(
        web_studio,
        "plan_creative_turn",
        lambda *_args, **_kwargs: pytest.fail(
            "Explicit comic/commerce selection must not run the image/chat planner"
        ),
    )
    try:
        yield calls
    finally:
        # Join before monkeypatch teardown; no worker may outlive its offline models.
        app.runner.close()


def _send(app: web_studio.StudioApplication, conversation_id: str, **overrides: Any) -> list:
    return list(
        app.stream_conversation(
            conversation_id,
            {
                "content": "做一张 JOJO 风格的大耳朵图图牛爷爷打电话",
                "selected_domain": "comic",
                "execution_mode": "fast",
                "_trace_id": "trace-quick-domain-test",
                **overrides,
            },
        )
    )


def _run(events: list) -> dict[str, Any]:
    return next(data for event, data in events if event == "run")


def _confirm(app: web_studio.StudioApplication, run: dict[str, Any]) -> dict[str, Any]:
    approval = app.runtime_store.approval_for_node(run["id"], "director_gate")
    assert approval and approval.request["kind"] == "director_review"
    app.decide_core_approval(approval.id, "approve", {})
    return app._run_payload(run["id"])


def test_comic_selection_enters_coordinator_and_real_shared_production(
    app: web_studio.StudioApplication,
    production: list[str],
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    events = _send(app, cid)
    waiting = _run(events)
    assert waiting["status"] == "waiting"
    assert production == SKILLS[:4]
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)
    run = _confirm(app, waiting)
    intent = next(data for event, data in events if event == "intent")
    assert (intent["domain"], intent["execution_mode"], intent["tool"]) == (
        "comic",
        "fast",
        "workflow.start",
    )
    run = app.runtime_store.get_run(run["id"])
    assert run.status is ExecutionStatus.COMPLETED
    assert run.workflow == "comic.production.v1"
    assert run.state["conversation_id"] == cid
    assert run.state["message_id"] == intent["user_message_id"]
    assert run.state["trace_id"] == "trace-quick-domain-test"
    creation = run.state["quick_creation"]
    director = app.runtime_store.get_run(creation["director_run_id"])
    assert director.workflow == "comic.director"
    assert director.id in {
        item.id
        for item in app.runtime_store.list_runs(
            interaction_mode=InteractionMode.AUTONOMOUS,
        )
    }
    assert director.state["input_brief_id"] == creation["input_brief_id"]
    spec = app.comic_projects.get_director(creation["project_id"])
    assert spec.schema_version == 2 and spec.critic_result.verdict == "pass"
    assert production == SKILLS[:4] + ["storyboard", "prompt"]
    artifact = app.runtime_store.get_artifact(run.state["image_artifact_id"])
    assert artifact.conversation_id == cid and Path(artifact.location).is_file()
    reservation = web_studio.budget.get_reservation(run.state["request_id"])
    assert reservation.conversation_id == cid and reservation.run_id == run.id
    prompt_artifact = app.runtime_store.get_artifact(creation["prompt_artifact_id"])
    assert prompt_artifact.type is ArtifactType.PROMPT
    assert app.conversation(cid)["domain"] is None
    assert any(item["run_id"] == artifact.run_id for item in app.conversation(cid)["messages"])
    assert not app.runtime_store.list_approvals(pending_only=True)
    assert not any("只是对话" in data.get("content", "") for _, data in events)
    event = next(
        event
        for event in app.runtime_store.list_events(run.id)
        if event.payload.get("kind") == "domain_dispatch"
    )
    assert event.payload["selected_coordinator"] == "ComicDirectorCoordinator"


def test_explicit_null_does_not_inherit_pending_selection(
    app: web_studio.StudioApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.runner, "submit", lambda _execute: None)
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    app.set_fast_domain(cid, "comic")
    first = _send(app, cid)
    monkeypatch.setattr(
        web_studio,
        "plan_creative_turn",
        lambda content, _prior, **_kwargs: creative.CreativeDecision(
            action="chat", request=content
        ),
    )
    monkeypatch.setattr(web_studio, "stream_chat", lambda *_args, **_kwargs: iter(["你好。"]))
    second = _send(app, cid, content="你好", selected_domain=None, execution_mode="normal")
    assert _run(first)["state"]["conversation_id"] == cid
    assert not any(event == "run" for event, _ in second)
    assert next(data for event, data in second if event == "intent")["domain"] is None


def test_legacy_waiting_run_cannot_lock_quick_menu_after_reload(
    app: web_studio.StudioApplication, production: list[str],
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    waiting = _run(_send(app, cid))
    app.runtime_store.set_conversation_domain(cid, "comic")
    app.runtime_store.bind_fast_domain_task(cid, waiting["id"])
    detail = app.conversation(cid)
    assert detail["domain"] is None and detail["fast_domain_task_id"] is None
    assert app.runtime_store.get_run(waiting["id"]).status is ExecutionStatus.WAITING
    assert app.set_fast_domain(cid, "studio")["domain"] == "studio"
    assert app.runtime_store.approval_for_node(waiting["id"], "director_gate").decision == "pending"


def test_http_new_conversation_stays_empty_and_old_chat_artifact_remains_accessible(
    app: web_studio.StudioApplication, production: list[str], server: int,
) -> None:
    connection = HTTPConnection("127.0.0.1", server, timeout=10)
    headers = {"X-Studio-Token": app.token, "Content-Type": "application/json"}

    def request(method, path, body=None):
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status in {200, 201}, payload
        return payload

    try:
        old = request("POST", "/api/conversations", {"interaction_mode": "autonomous"})
        run = _run(_send(app, old["id"]))
        completed = _confirm(app, run)
        original = request("GET", f"/api/conversations/{old['id']}")
        assert any(message["run_id"] == completed["id"] for message in original["messages"])
        artifact = request("GET", f"/api/artifacts/{completed['state']['image_artifact_id']}")
        assert artifact["conversation_id"] == old["id"] and artifact["type"] == "image"
        connection.request("GET", f"/api/artifacts/{artifact['id']}/content", headers=headers)
        response = connection.getresponse()
        assert response.status == 200 and response.read().startswith(b"\x89PNG")
        new = request("POST", "/api/conversations", {"interaction_mode": "autonomous"})
        assert new["id"] != old["id"]
        empty = request("GET", f"/api/conversations/{new['id']}")
        assert empty["messages"] == [] and empty["active_run_id"] is None
        assert empty["domain"] is None and empty["media_jobs"] == []
        _send(app, new["id"], content="中式修仙少女站在竹林", generation_request_id="fresh-http")
        updated = request("GET", f"/api/conversations/{new['id']}")
        assert updated["active_run_id"] != original["active_run_id"]
        assert updated["messages"][0]["content"] == "中式修仙少女站在竹林"
        restored = request("GET", f"/api/conversations/{old['id']}")
        assert restored["messages"] == original["messages"]
        assert request("GET", f"/api/conversations/{new['id']}")["messages"] == updated["messages"]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "domain,mode", [("unknown", "fast"), ("comic", "normal"), ("comic", "professional")]
)
def test_invalid_fast_context_never_creates_paid_work(
    app: web_studio.StudioApplication,
    domain: str,
    mode: str,
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    with pytest.raises(ToolError):
        _send(app, cid, selected_domain=domain, execution_mode=mode)
    assert not app.runtime_store.list_runs()


@pytest.mark.parametrize(
    "user_request",
    [
        "山海经穷奇站在悬崖边看远方村庄",
        "中式修仙少女站在竹林",
        "少年雨夜坐在屋檐下思念故乡",
        "未来城市机器人咖啡师",
    ],
)
def test_each_selected_task_binds_only_its_fresh_brief(
    app: web_studio.StudioApplication,
    production: list[str],
    user_request: str,
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    old = _confirm(app, _run(_send(app, cid, content="旧创意：穷奇悬崖")))
    new = _confirm(app, _run(_send(app, cid, content=user_request)))
    assert old["status"] == new["status"] == "completed"
    previous, current = old["state"]["quick_creation"], new["state"]["quick_creation"]
    assert previous["project_id"] != current["project_id"]
    director = app.runtime_store.get_run(current["director_run_id"])
    assert director.state["director_debug"]["brief_request"] == user_request
    assert director.state["director_debug"]["current_task"] == user_request
    assert director.state["director_debug"]["memory_used"] == []
    assert director.state["previous_run_id"] is None


def test_high_cost_waits_before_director_then_resumes_same_run(
    app: web_studio.StudioApplication,
    production: list[str],
) -> None:
    web_studio.get_settings().budget.autonomous_image_auto_cny = Decimal("0.10")
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    assert run["status"] == "waiting" and production == []
    snapshot = app.comic_projects.get(run["state"]["quick_creation"]["project_id"])
    assert snapshot.project.director_id is None
    approval = app.runtime_store.approval_for_node(run["id"], "cost_approval")
    assert approval.request["total_fen"] > 10
    assert approval.request["estimate_fen"] == approval.request["total_fen"]
    app.decide_core_approval(approval.id, "approve", {})
    _confirm(app, run)
    assert app.runtime_store.get_run(run["id"]).status is ExecutionStatus.COMPLETED
    assert production == SKILLS[:4] + ["storyboard", "prompt"]


def test_missing_price_requires_honest_cost_confirmation(
    app: web_studio.StudioApplication,
    production: list[str],
) -> None:
    web_studio.get_settings().llm.pricing_cny_per_million_by_model = {}
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    assert run["status"] == "waiting" and production == []
    approval = app.runtime_store.approval_for_node(run["id"], "cost_approval")
    assert set(approval.request["unpriced_models"]) == {"text-test", "vision-test"}


def test_critic_revision_keeps_draft_and_never_calls_image(
    app: web_studio.StudioApplication,
    production: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = app._comic_director_model

    def needs_review(messages: list[dict[str, str]]) -> str:
        if "findings 每项" in messages[0]["content"]:
            return json.dumps(
                {
                    "public_summary": "需要人工调整构图。",
                    "confidence": 0.9,
                    "findings": [
                        {
                            "code": "VISUAL_CONFLICT",
                            "severity": "error",
                            "field_path": "director_plan.visual_focus",
                            "evidence": "保留本次主体",
                            "expected": "修订视觉方向",
                            "suggested_action": "修改后重新审核",
                        }
                    ],
                    "suggested_patches": [],
                },
                ensure_ascii=False,
            )
        return original(messages)

    monkeypatch.setattr(app, "_comic_director_model", needs_review)
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    assert run["status"] == "waiting"
    assert "storyboard" not in production
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)
    director = app.runtime_store.get_run(run["state"]["quick_creation"]["director_run_id"])
    assert director.state["director_candidate"]
    approval = app.runtime_store.approval_for_node(run["id"], "director_gate")
    assert approval.request["ready"] is False
    with pytest.raises(ToolError, match="尚需修订"):
        app.decide_core_approval(approval.id, "approve", {})
    assert app.runtime_store.get_approval(approval.id).decision.value == "pending"


def test_selected_commerce_runs_existing_graph_not_normal_chat(
    app: web_studio.StudioApplication,
    production: list[str],
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    events = _send(app, cid, selected_domain="commerce", content="做一个便携台灯商品介绍")
    run = _run(events)
    assert run["domain"] == "commerce" and run["status"] == "completed"
    assert run["state"]["conversation_id"] == cid
    assert run["state"]["marketplace_draft"]["mock"] is True
    assert production == []
    assert app.conversation(cid)["domain"] is None


def test_visual_alias_dispatches_shared_image_capability(
    app: web_studio.StudioApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    monkeypatch.setattr(
        web_studio,
        "plan_creative_turn",
        lambda content, _prior, **_kwargs: creative.CreativeDecision(
            action="image.generate", request=content, subject="竹林"
        ),
    )
    events = _send(app, cid, selected_domain="visual", content="做一张竹林画面")
    assert next(data for event, data in events if event == "intent")["domain"] == "studio"
    assert any(event == "image_ready" for event, _ in events)
    assert app.runtime_store.list_media_jobs(cid)[0].status.value == "completed"
    assert app.conversation(cid)["domain"] is None


def test_stream_http_payload_reaches_director_with_real_run_and_trace(
    app: web_studio.StudioApplication,
    production: list[str],
    server: int,
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    connection = HTTPConnection("127.0.0.1", server, timeout=15)
    try:
        connection.request(
            "POST",
            f"/api/conversations/{cid}/messages/stream",
            json.dumps(
                {
                    "content": "做一张牛爷爷打电话",
                    "selected_domain": "comic",
                    "execution_mode": "fast",
                }
            ),
            headers={"Content-Type": "application/json", "X-Studio-Token": app.token},
        )
        response = connection.getresponse()
        packets = response.read().decode("utf-8").split("\n\n")
        assert response.status == 200
        runs = [
            json.loads(line.removeprefix("data: "))
            for packet in packets
            if "event: run\n" in packet
            for line in packet.splitlines()
            if line.startswith("data: ")
        ]
        assert len(runs) == 1
        stored = app.runtime_store.get_run(runs[0]["id"])
        assert stored.status is ExecutionStatus.WAITING
        _confirm(app, {"id": stored.id})
        assert app.runtime_store.get_run(stored.id).status is ExecutionStatus.COMPLETED
        director = app.runtime_store.get_run(stored.state["quick_creation"]["director_run_id"])
        assert director.state["conversation_id"] == cid
        assert director.state["trace_id"] == response.getheader("X-Trace-ID")
        assert any(
            event.event_type.value == "node_completed"
            for event in app.runtime_store.list_events(director.id)
        )
    finally:
        connection.close()


def test_comic_failure_is_persisted_and_not_replaced_by_normal_chat(
    app: web_studio.StudioApplication,
    production: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "_comic_director_model", lambda _messages: "not a valid decision")
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    events = _send(app, cid)
    run = app.runtime_store.get_run(_run(events)["id"])
    assert run.status is ExecutionStatus.FAILED
    director = app.runtime_store.get_run(run.state["quick_creation"]["director_run_id"])
    assert director.state["stage_failures"]
    assert director.state["trace_id"] == run.state["trace_id"]
    assert not any("只是对话" in data.get("content", "") for _, data in events)
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)


@pytest.mark.parametrize("total,automatic", [(2000, True), (2001, False)])
def test_home_twenty_yuan_boundary(
    app: web_studio.StudioApplication,
    production: list[str],
    monkeypatch: pytest.MonkeyPatch,
    total: int,
    automatic: bool,
) -> None:
    monkeypatch.setattr(app, "_quick_creation_cost", lambda _count: (total, []))
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    assert (run["current_node"] == "director_gate") is automatic
    approval = app.runtime_store.approval_for_node(run["id"], "cost_approval")
    assert (approval is None) is automatic


def test_other_conversation_and_normal_chat_are_not_blocked_by_director(
    app: web_studio.StudioApplication,
    production: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = app._comic_director_model

    def blocking(messages: list[dict[str, str]]) -> str:
        if (
            "等待中的牛爷爷" in messages[1]["content"]
            and "CreativeDecision" in messages[0]["content"]
        ):
            entered.set()
            assert release.wait(10)
        return original(messages)

    monkeypatch.setattr(app, "_comic_director_model", blocking)
    monkeypatch.setattr(app.runner, "submit", TaskRunner.submit.__get__(app.runner))
    monkeypatch.setattr(
        web_studio,
        "plan_creative_turn",
        lambda content, _prior, **_kwargs: creative.CreativeDecision(
            action="chat", request=content
        ),
    )
    monkeypatch.setattr(web_studio, "stream_chat", lambda *_args, **_kwargs: iter(["立即正常回答"]))
    a = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    b = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    try:
        first = _run(_send(app, a, content="等待中的牛爷爷打电话"))
        assert entered.wait(5)
        second = _run(_send(app, b, content="另一个机器人咖啡师"))
        plain = _send(app, b, content="你好", selected_domain=None, execution_mode="normal")
        assert any(event == "delta" and data["content"] == "立即正常回答" for event, data in plain)
        assert first["id"] != second["id"]
        assert app.runtime_store.get_run(first["id"]).status is ExecutionStatus.RUNNING
    finally:
        release.set()
        app.runner.close()
    assert app.runtime_store.get_run(first["id"]).status is ExecutionStatus.WAITING
    assert app.runtime_store.get_run(second["id"]).status is ExecutionStatus.WAITING


def test_restarted_parent_reuses_director_run_without_resubmitting_models(
    app: web_studio.StudioApplication,
    production: list[str],
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    before = list(production)
    restarted = web_studio.StudioApplication()
    try:
        result = restarted._comic_fast_creation_step(
            "director",
            ComicState.model_validate(run["state"]),
            RuntimeContext(
                run_id=run["id"], node_id="director", store=restarted.runtime_store, services={}
            ),
        )
        assert (
            result["quick_creation"]["director_run_id"]
            == run["state"]["quick_creation"]["director_run_id"]
        )
        assert production == before
    finally:
        restarted.runner.close()


def test_retried_stream_request_reuses_production_run_and_never_regenerates(
    app: web_studio.StudioApplication,
    production: list[str],
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    first = _run(_send(app, cid, generation_request_id="quick-once"))
    before = list(production)
    second = _run(_send(app, cid, generation_request_id="quick-once"))
    assert second["id"] == first["id"]
    assert production == before
    assert (
        second["state"]["quick_creation"]["project_id"]
        == first["state"]["quick_creation"]["project_id"]
    )
    assert app.conversation(cid)["domain"] is None
    with pytest.raises(ToolError):
        _send(app, cid, generation_request_id="quick-once", content="不同创意")
    assert production == before


def test_simultaneous_retries_allocate_one_production_run(
    app: web_studio.StudioApplication,
    production: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submissions: list[Any] = []
    monkeypatch.setattr(app.runner, "submit", lambda execute: submissions.append(execute))
    conversation = app.runtime_store.get_conversation(
        app.create_conversation({"interaction_mode": "autonomous"})["id"],
    )
    ready = threading.Barrier(2)

    def allocate() -> dict[str, Any]:
        ready.wait(5)
        return app._start_conversation_workflow(
            "comic", "牛爷爷打电话", conversation, "one-user-message", {},
            trace_id="trace-same-request", selected_domain="comic",
        )

    with ThreadPoolExecutor(max_workers=2) as requests:
        a, b = requests.submit(allocate), requests.submit(allocate)
        assert a.result(10)["id"] == b.result(10)["id"]
    assert len(submissions) == 1
    assert len(app.runtime_store.list_runs(conversation_id=conversation.id)) == 1
    assert production == []


def test_director_rejection_never_submits_image(
    app: web_studio.StudioApplication, production: list[str],
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    approval = app.runtime_store.approval_for_node(run["id"], "director_gate")
    app.decide_core_approval(approval.id, "reject", {})
    assert production == SKILLS[:4]
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)
    assert app.runtime_store.get_run(run["id"]).state["quick_creation"][
        "director_decision"
    ] == "reject"


def test_revision_saves_new_draft_then_requires_fresh_confirmation(
    app: web_studio.StudioApplication, production: list[str],
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    approval = app.runtime_store.approval_for_node(run["id"], "director_gate")
    with pytest.raises(ToolError, match="填写"):
        app.decide_core_approval(approval.id, "revise", {})
    app.decide_core_approval(approval.id, "revise", {
        "revision_instruction": "让人物与背景分开，保留电话动作",
    })
    revised = app.runtime_store.get_run(run["id"])
    assert revised.status is ExecutionStatus.WAITING
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)
    current = app.runtime_store.approval_for_node(run["id"], "director_gate")
    assert current.id != approval.id
    assert current.request["director_version"] > approval.request["director_version"]
    assert "revision" in production
    messages = app.conversation(cid)["messages"]
    drafts = [item for item in messages if (item.get("event_id") or "").startswith(
        "quick-director:"
    )]
    assert len(drafts) == 2
    assert json.loads(drafts[-1]["content"])["director_spec"]["director_plan"][
        "composition_strategy"
    ] == "让人物与背景分开，保留电话动作"
    _confirm(app, run)
    assert app.runtime_store.get_run(run["id"]).status is ExecutionStatus.COMPLETED
    assert len(app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)) == 1


def test_restart_preserves_confirmation_and_continues_same_image_task(
    app: web_studio.StudioApplication, production: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    restarted = web_studio.StudioApplication()
    monkeypatch.setattr(restarted, "_comic_director_model", app._comic_director_model)
    monkeypatch.setattr(restarted, "_comic_storyboard_model", app._comic_storyboard_model)
    monkeypatch.setattr(restarted.runner, "submit", lambda execute: execute())
    try:
        before = list(production)
        _confirm(restarted, run)
        assert production[:len(before)] == before
        assert production[len(before):] == ["storyboard", "prompt"]
        assert restarted.runtime_store.get_run(run["id"]).status is ExecutionStatus.COMPLETED
        assert len(restarted.runtime_store.list_artifacts(type=ArtifactType.IMAGE)) == 1
        assert all(item["run_id"] == run["id"] for item in restarted.conversation(cid)[
            "messages"
        ] if (item.get("event_id") or "").startswith("quick-director:"))
    finally:
        restarted.runner.close()


def test_outdated_confirmation_cannot_generate_a_changed_director(
    app: web_studio.StudioApplication, production: list[str],
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    project_id = run["state"]["quick_creation"]["project_id"]
    snapshot = app.comic_projects.get(project_id)
    spec = app.get_comic_director(project_id)
    from kantoku.domains.comic.models import DirectorSpecDraft

    draft = {key: spec[key] for key in DirectorSpecDraft.model_fields if key in spec}
    draft["critic_result"] = None
    app.create_comic_director(project_id, {
        "expected_project_version": snapshot.project.current_version, "draft": draft,
    })
    approval = app.runtime_store.approval_for_node(run["id"], "director_gate")
    with pytest.raises(ToolError, match="版本已变化"):
        app.decide_core_approval(approval.id, "approve", {})
    assert app.runtime_store.get_approval(approval.id).decision.value == "pending"
    assert "storyboard" not in production
    assert not app.runtime_store.list_artifacts(type=ArtifactType.IMAGE)


def test_older_checkpoint_restores_bound_director_before_confirmation(
    app: web_studio.StudioApplication, production: list[str],
) -> None:
    cid = app.create_conversation({"interaction_mode": "autonomous"})["id"]
    run = _run(_send(app, cid))
    state = ComicState.model_validate(run["state"])
    state.quick_creation.pop("director_spec")
    state.quick_creation.pop("director_spec_version")
    before = list(production)
    request = app._comic_fast_director_review(state)
    assert request["ready"] is True
    assert request["director_version"] == run["state"]["quick_creation"]["director_spec_version"]
    assert production == before  # read-only, no model resubmission
