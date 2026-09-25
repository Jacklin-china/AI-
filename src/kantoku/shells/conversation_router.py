"""Conversation interaction strategy; no provider or domain workflow lives here."""

from __future__ import annotations

from enum import StrEnum

from kantoku.core.conversations import ConversationRecord, IntentPlan, InteractionMode


class ConversationAction(StrEnum):
    CHAT = "chat"
    IMAGE_GENERATE = "image.generate"
    IMAGE_EDIT = "image.edit"
    VIDEO_GENERATE = "video.generate"
    WORKFLOW_START = "workflow.start"
    CHOOSE_DOMAIN = "choose_domain"


def route_conversation(
    conversation: ConversationRecord, plan: IntentPlan, *, confirmed: bool,
) -> ConversationAction:
    """Only an explicit Guided domain can start a professional Graph Run."""
    if not plan.needs_execution:
        return ConversationAction.CHAT
    if conversation.interaction_mode is InteractionMode.AUTONOMOUS:
        if conversation.domain is None and plan.intent == "image.generate":
            return ConversationAction.IMAGE_GENERATE
        if conversation.domain is None and plan.intent == "image.edit":
            return ConversationAction.IMAGE_EDIT
        if conversation.domain is None and plan.intent == "video.generate":
            return ConversationAction.VIDEO_GENERATE
        return ConversationAction.CHOOSE_DOMAIN
    if confirmed and conversation.domain in {"comic", "commerce"}:
        return ConversationAction.WORKFLOW_START
    return ConversationAction.CHAT
