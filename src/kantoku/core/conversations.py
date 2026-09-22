"""Persistent conversation records and a conservative execution intent planner."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class InteractionMode(StrEnum):
    AUTONOMOUS = "autonomous"
    GUIDED = "guided"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageType(StrEnum):
    TEXT = "text"
    PLAN = "plan"
    STATUS = "status"
    APPROVAL = "approval"
    ARTIFACT = "artifact"
    ERROR = "error"


class ConversationRecord(BaseModel):
    id: str
    title: str
    interaction_mode: InteractionMode
    domain: str | None = None
    active_run_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ConversationMessageRecord(BaseModel):
    id: str
    conversation_id: str
    role: MessageRole
    type: MessageType
    content: str
    run_id: str | None = None
    event_id: str | None = None
    created_at: datetime


class IntentPlan(BaseModel):
    intent: str
    needs_execution: bool
    suggested_domain: str | None = None
    suggested_skills: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    clarification: str | None = None


class IntentPlanner:
    """Route only explicit production requests; uncertain requests stay in chat."""

    _commerce = ("ozon", "listing", "sku", "选品", "商品", "电商", "定价", "上架")
    _comic = ("漫剧", "漫画", "分镜", "镜头", "脚本转", "故事板")
    _image = ("生图", "生成图片", "主图", "立绘", "海报", "概念图")
    _execute = ("生成", "制作", "创建", "开始", "执行", "跑一下", "帮我做")

    def plan(self, content: str, *, domain_hint: str | None = None) -> IntentPlan:
        text = content.strip().lower()
        if not text:
            return IntentPlan(
                intent="clarify", needs_execution=False, confidence=0,
                clarification="请先描述你想讨论或制作的内容。",
            )
        explicit = any(word in text for word in self._execute)
        if any(word in text for word in self._commerce):
            return IntentPlan(
                intent="commerce_production", needs_execution=explicit,
                suggested_domain="commerce", suggested_skills=["commerce"],
                confidence=0.94 if explicit else 0.72,
            )
        if any(word in text for word in self._comic):
            return IntentPlan(
                intent="comic_production", needs_execution=explicit,
                suggested_domain="comic", suggested_skills=["comic"],
                confidence=0.94 if explicit else 0.72,
            )
        if any(word in text for word in self._image):
            return IntentPlan(
                intent="image_production", needs_execution=explicit,
                suggested_domain="studio", suggested_skills=["image-generation"],
                confidence=0.92 if explicit else 0.7,
            )
        if domain_hint in {"commerce", "comic", "studio"} and explicit:
            return IntentPlan(
                intent=f"{domain_hint}_production", needs_execution=True,
                suggested_domain=domain_hint, suggested_skills=[domain_hint], confidence=0.78,
            )
        if explicit:
            return IntentPlan(
                intent="clarify", needs_execution=False, confidence=0.4,
                clarification="你希望产出图片、漫剧分镜，还是电商 Listing？",
            )
        return IntentPlan(intent="chat", needs_execution=False, confidence=0.9)
