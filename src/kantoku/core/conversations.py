"""Persistent conversation records and a conservative execution intent planner."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field


class InteractionMode(StrEnum):
    AUTONOMOUS = "autonomous"
    GUIDED = "guided"


class ExecutionMode(StrEnum):
    FAST = "fast"
    PROFESSIONAL = "professional"


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

    @computed_field
    @property
    def execution_mode(self) -> ExecutionMode:
        """Public product mode; reuse the existing durable interaction mode."""
        return (ExecutionMode.FAST if self.interaction_mode is InteractionMode.AUTONOMOUS
                else ExecutionMode.PROFESSIONAL)


class ConversationMessageRecord(BaseModel):
    id: str
    conversation_id: str
    role: MessageRole
    type: MessageType
    content: str
    run_id: str | None = None
    event_id: str | None = None
    artifact_id: str | None = None
    created_at: datetime


class MediaJobStatus(StrEnum):
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class MediaJobRecord(BaseModel):
    generation_request_id: str
    conversation_id: str
    user_message_id: str
    media_type: str
    status: MediaJobStatus
    artifact_id: str | None = None
    error_id: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class IntentPlan(BaseModel):
    intent: str
    needs_execution: bool
    suggested_domain: str | None = None
    suggested_skills: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    clarification: str | None = None


class IntentPlanner:
    """Recognize requested deliverables without requiring a command like「生成图片」."""

    _commerce = ("ozon", "listing", "sku", "选品", "商品", "电商", "定价", "上架")
    _comic = ("漫剧", "漫画", "分镜", "镜头", "脚本转", "故事板")
    _image = (
        "生图", "生成图片", "主图", "立绘", "海报", "概念图", "图片", "照片",
        "插画", "肖像", "头像", "画面", "写实", "写真", "绘图", "画图", "壁纸",
        "漫画", "logo", "标志", "封面", "贴纸", "表情包",
    )
    _video = ("视频", "短片", "影片", "动画片", "动图", "影像片段")
    _execute = (
        "生成", "制作", "创建", "开始", "执行", "跑一下", "帮我做", "绘制",
        "画一", "画个", "画张", "请给我", "给我一", "给我做", "来一", "要一", "出一",
        "设计",
    )
    _question = ("如何", "怎么", "为什么", "解释", "介绍", "教程", "原理", "请问")
    _information_cues = ("有哪些", "是什么", "什么意思", "的方法", "的步骤", "介绍一下")
    _direct_requests = ("请给我", "给我一", "给我做", "帮我生成", "帮我画", "画一", "画个")
    _complex = ("漫剧", "分镜", "多镜头", "故事板", "脚本转")

    def plan(
        self, content: str, *, domain_hint: str | None = None, image_context: bool = False,
    ) -> IntentPlan:
        text = content.strip().lower()
        if not text:
            return IntentPlan(
                intent="clarify", needs_execution=False, confidence=0,
                clarification="请先描述你想讨论或制作的内容。",
            )
        starts_question = any(
            text.startswith(word) or text.startswith(f"请{word}") for word in self._question
        )
        asks_question = starts_question or (
            any(word in text for word in self._information_cues)
            and not any(word in text for word in self._direct_requests)
        )
        explicit = any(word in text for word in self._execute) and not asks_question
        if explicit and any(word in text for word in self._complex):
            return IntentPlan(
                intent="comic_production", needs_execution=True,
                suggested_domain="comic", suggested_skills=["comic"], confidence=0.94,
            )
        if explicit and any(word in text for word in self._video):
            return IntentPlan(
                intent="video.generate", needs_execution=True,
                suggested_domain="studio", suggested_skills=["video-generation"], confidence=0.92,
            )
        image_followup = image_context and not asks_question and text.startswith((
            "再帮我生成", "再生成", "重新生成", "再来一张", "再画", "换成", "改成",
        ))
        if image_followup and not any(
            word in text for word in (*self._commerce, *self._video, "文章", "文案", "文字")
        ):
            return IntentPlan(
                intent="image.generate", needs_execution=True,
                suggested_domain="studio", suggested_skills=["image-generation"], confidence=0.9,
            )
        visual_request = any(word in text for word in self._image) or (
            explicit and "图" in text and "图表" not in text
        ) or (explicit and any(word in text for word in ("一张", "两张", "三张", "张图"))) or (
            explicit and ("画一" in text or "画个" in text or "画张" in text)
        )
        if visual_request and explicit:
            return IntentPlan(
                intent="image.generate", needs_execution=True,
                suggested_domain="studio", suggested_skills=["image-generation"], confidence=0.92,
            )
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
        if visual_request:
            return IntentPlan(
                intent="image.generate", needs_execution=False,
                suggested_domain="studio", suggested_skills=["image-generation"],
                confidence=0.7,
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
