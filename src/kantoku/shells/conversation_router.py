"""Conversation interaction strategy; no provider or domain workflow lives here."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from kantoku.config import ToolError
from kantoku.core.conversations import (
    ConversationRecord,
    ExecutionMode,
    IntentPlan,
    InteractionMode,
)


class ConversationAction(StrEnum):
    CHAT = "chat"
    IMAGE_GENERATE = "image.generate"
    IMAGE_EDIT = "image.edit"
    VIDEO_GENERATE = "video.generate"
    WORKFLOW_START = "workflow.start"
    CHOOSE_DOMAIN = "choose_domain"


@dataclass(frozen=True)
class ConversationRoute:
    action: ConversationAction
    domain: str | None
    execution_mode: ExecutionMode


def route_conversation(
    conversation: ConversationRecord,
    plan: IntentPlan,
    *,
    confirmed: bool,
    selected_domain: str | None = None,
) -> ConversationRoute:
    """One domain + mode decision for both homepage and professional entry points."""
    mode = conversation.execution_mode
    domain = conversation.domain
    if conversation.interaction_mode is InteractionMode.AUTONOMOUS and selected_domain:
        action = (
            ConversationAction.IMAGE_GENERATE
            if selected_domain == "studio"
            else ConversationAction.WORKFLOW_START
        )
        if selected_domain == "studio" and plan.intent in {"image.edit", "video.generate"}:
            action = ConversationAction(plan.intent)
        return ConversationRoute(action, selected_domain, mode)
    if not plan.needs_execution:
        return ConversationRoute(ConversationAction.CHAT, domain, mode)
    if conversation.interaction_mode is InteractionMode.AUTONOMOUS:
        if plan.intent == "image.generate":
            return ConversationRoute(ConversationAction.IMAGE_GENERATE, domain, mode)
        if plan.intent == "image.edit":
            return ConversationRoute(ConversationAction.IMAGE_EDIT, domain, mode)
        if plan.intent == "video.generate":
            return ConversationRoute(ConversationAction.VIDEO_GENERATE, domain, mode)
        production_domains = {
            "comic_production": "comic",
            "commerce_production": "commerce",
        }
        required_domain = production_domains.get(plan.intent)
        if required_domain and domain in {None, required_domain}:
            return ConversationRoute(
                ConversationAction.WORKFLOW_START,
                required_domain,
                mode,
            )
        return ConversationRoute(ConversationAction.CHOOSE_DOMAIN, domain, mode)
    if confirmed and conversation.domain in {"comic", "commerce"}:
        return ConversationRoute(ConversationAction.WORKFLOW_START, domain, mode)
    return ConversationRoute(ConversationAction.CHAT, domain, mode)


def selected_fast_domain(conversation: ConversationRecord, data: dict[str, Any]) -> str | None:
    """Explicit per-message context wins; old clients may use the unconsumed selection."""
    if conversation.interaction_mode is not InteractionMode.AUTONOMOUS:
        if data.get("selected_domain") is not None or data.get("execution_mode") in {
            "fast",
            "normal",
        }:
            raise ToolError("首页快捷模式不能用于专业会话")
        return None
    domain = (
        data.get("selected_domain")
        if "selected_domain" in data
        else (conversation.domain if conversation.fast_domain_task_id is None else None)
    )
    if domain is not None and not isinstance(domain, str):
        raise ToolError("快捷创作域无效")
    if domain == "visual":
        domain = "studio"
    if domain is not None and domain not in {"comic", "commerce", "studio"}:
        raise ToolError("快捷创作域无效")
    mode = data.get("execution_mode")
    if mode is not None and not isinstance(mode, str):
        raise ToolError("首页执行模式无效")
    if mode not in {None, "fast", "normal"} or (domain and mode == "normal"):
        raise ToolError("首页执行模式无效")
    return domain
