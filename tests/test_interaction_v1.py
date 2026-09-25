"""Interaction v1: intent, conversations, cost approval, and safe logging."""

from __future__ import annotations

from pathlib import Path

from test_domain_workflows import FakeComicServices

from kantoku.config.logging_setup import redact_secrets
from kantoku.core.conversations import (
    IntentPlanner,
    InteractionMode,
    MessageRole,
    MessageType,
)
from kantoku.core.runtime.graph import GraphRuntime
from kantoku.core.runtime.models import ApprovalDecision, ExecutionStatus
from kantoku.core.runtime.store import RuntimeStore
from kantoku.domains.comic import ComicState, build_comic_workflow


def test_normal_chat_does_not_request_execution() -> None:
    plan = IntentPlanner().plan("解释一下商品定价通常要考虑什么")
    assert plan.needs_execution is False
    assert plan.suggested_domain == "commerce"


def test_explicit_production_request_selects_existing_domain() -> None:
    plan = IntentPlanner().plan("帮我生成一张银发角色立绘")
    assert plan.needs_execution is True
    assert plan.suggested_domain == "studio"
    assert plan.confidence > 0.8


def test_home_image_request_with_character_name_starts_visual_work() -> None:
    planner = IntentPlanner()
    for request in (
        "帮我生成一个写实版的大耳朵图图",
        "帮我生成一个写实的蜡笔小新照片",
        "画一张雨夜里的猫",
        "请给我一张蜡笔小新中正男的卡通图片",
        "生成蜡笔小新头像",
        "画一个赛博朋克城市",
    ):
        plan = planner.plan(request)
        assert plan.needs_execution is True
        assert plan.intent == "image.generate"
        assert plan.suggested_domain == "studio"
    assert planner.plan("如何生成图片").needs_execution is False
    assert planner.plan("图片生成有哪些技巧").needs_execution is False
    assert planner.plan("给我介绍一下生成图片的方法").needs_execution is False


def test_image_followups_use_chat_context_without_affecting_new_chats() -> None:
    planner = IntentPlanner()
    for request in ("再帮我生成小埋", "再生成一个小埋", "换成小埋", "改成海老名"):
        assert planner.plan(request, image_context=True).intent == "image.generate"
    assert planner.plan("再帮我生成小埋").intent != "image.generate"
    assert planner.plan("换成视频", image_context=True).intent != "image.generate"
    assert planner.plan("如何生成小埋图片", image_context=True).needs_execution is False


def test_video_request_has_distinct_intent_without_pretending_provider_is_ready() -> None:
    plan = IntentPlanner().plan("帮我制作一段猫咪奔跑的视频")
    assert plan.intent == "video.generate"
    assert plan.needs_execution is True


def test_domain_hint_is_optional_and_only_routes_explicit_work() -> None:
    planner = IntentPlanner()
    assert planner.plan("你好", domain_hint="comic").needs_execution is False
    assert planner.plan("请开始制作这个方案", domain_hint="comic").suggested_domain == "comic"
    assert planner.plan("帮我做一下").intent == "clarify"


def test_conversation_and_messages_survive_store_restart(tmp_path: Path) -> None:
    database = tmp_path / "conversation.db"
    first = RuntimeStore(database)
    conversation = first.create_conversation(interaction_mode=InteractionMode.AUTONOMOUS)
    message = first.add_conversation_message(
        conversation.id, role=MessageRole.USER, type=MessageType.TEXT, content="你好",
    )
    second = RuntimeStore(database)
    restored = second.get_conversation(conversation.id)
    restored_messages = second.list_conversation_messages(conversation.id)
    assert restored.interaction_mode is InteractionMode.AUTONOMOUS
    assert restored_messages[0].id == message.id
    assert restored_messages[0].content == "你好"


def test_unconfirmed_comic_waits_for_cost_before_provider(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "cost.db")
    services = FakeComicServices()
    runtime = GraphRuntime(store)
    workflow = build_comic_workflow(services)
    runtime.register(workflow)
    waiting = runtime.start(workflow.id, ComicState(
        project="chat", prompt="image", shot_no=1, estimate_fen=30, confirmed=False,
    ))
    assert waiting.status is ExecutionStatus.WAITING
    assert waiting.current_node == "cost_approval"
    assert services.generated == 0
    approval = store.list_approvals(pending_only=True)[0]
    assert approval.request["kind"] == "cost_approval"
    assert approval.request["unit_fen"] == 30
    assert approval.request["image_count"] == 1
    assert approval.request["total_fen"] == 30


def test_cost_approval_breakdown_scales_with_image_count(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "cost-count.db")
    services = FakeComicServices()
    runtime = GraphRuntime(store)
    workflow = build_comic_workflow(services)
    runtime.register(workflow)
    waiting = runtime.start(workflow.id, ComicState(
        project="chat", prompt="image", shot_no=1, estimate_fen=20,
        image_count=4, confirmed=False,
    ))
    assert waiting.current_node == "cost_approval"
    approval = store.list_approvals(pending_only=True)[0]
    assert approval.request["estimate_fen"] == 20
    assert approval.request["image_count"] == 4
    assert approval.request["total_fen"] == 80


def test_run_can_be_persisted_before_background_execution(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "background.db")
    services = FakeComicServices()
    runtime = GraphRuntime(store)
    workflow = build_comic_workflow(services)
    runtime.register(workflow)
    pending = runtime.create(workflow.id, ComicState(
        project="chat", prompt="image", shot_no=1, estimate_fen=30, confirmed=False,
    ))
    assert pending.status is ExecutionStatus.PENDING
    assert services.generated == 0
    waiting = runtime.resume(pending.id)
    assert waiting.status is ExecutionStatus.WAITING
    assert waiting.current_node == "cost_approval"


def test_cost_reject_ends_without_paid_provider(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "cost-reject.db")
    services = FakeComicServices()
    runtime = GraphRuntime(store)
    workflow = build_comic_workflow(services)
    runtime.register(workflow)
    waiting = runtime.start(workflow.id, ComicState(
        project="chat", prompt="image", shot_no=1, estimate_fen=30, confirmed=False,
    ))
    approval = store.list_approvals(pending_only=True)[0]
    store.decide_approval(approval.id, ApprovalDecision.REJECT)
    finished = runtime.resume(waiting.id)
    assert finished.status is ExecutionStatus.COMPLETED
    assert services.generated == 0


def test_cost_approve_resumes_existing_workflow(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "cost-approve.db")
    services = FakeComicServices()
    runtime = GraphRuntime(store)
    workflow = build_comic_workflow(services)
    runtime.register(workflow)
    waiting = runtime.start(workflow.id, ComicState(
        project="chat", prompt="image", shot_no=1, estimate_fen=30, confirmed=False,
    ))
    approval = store.list_approvals(pending_only=True)[0]
    store.decide_approval(approval.id, ApprovalDecision.APPROVE)
    review_wait = runtime.resume(waiting.id)
    assert review_wait.status is ExecutionStatus.WAITING
    assert review_wait.current_node == "human_review"
    assert services.generated == 1


def test_log_redaction_covers_common_credentials() -> None:
    value = redact_secrets(
        "Authorization: Bearer token-value api_key=abc123 secret_key=qwerty sk-example123"
    )
    assert "token-value" not in value
    assert "abc123" not in value
    assert "qwerty" not in value
    assert "sk-example123" not in value
