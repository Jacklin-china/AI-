"""Conversation interaction strategy; no provider or domain workflow lives here."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

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
    conversation: ConversationRecord, plan: IntentPlan, *, confirmed: bool,
) -> ConversationRoute:
    """One domain + mode decision for both homepage and professional entry points."""
    mode = conversation.execution_mode
    domain = conversation.domain
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
            "comic_production": "comic", "commerce_production": "commerce",
        }
        required_domain = production_domains.get(plan.intent)
        if required_domain and domain in {None, required_domain}:
            return ConversationRoute(
                ConversationAction.WORKFLOW_START, required_domain, mode,
            )
        return ConversationRoute(ConversationAction.CHOOSE_DOMAIN, domain, mode)
    if confirmed and conversation.domain in {"comic", "commerce"}:
        return ConversationRoute(ConversationAction.WORKFLOW_START, domain, mode)
    return ConversationRoute(ConversationAction.CHAT, domain, mode)
