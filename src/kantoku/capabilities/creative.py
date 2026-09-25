"""Lightweight creative planning for autonomous conversations.

The planner interprets a user turn against the most recent completed image.
The compiler always carries the user's literal request into the provider prompt;
model suggestions may add context, but cannot silently replace that request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from loguru import logger

from kantoku.core.conversations import IntentPlanner
from kantoku.core.llm import chat


@dataclass(frozen=True)
class CreativeContext:
    subject: str = ""
    style: str = ""
    composition: str = ""
    background: str = ""
    artifact_id: str | None = None


@dataclass(frozen=True)
class CreativeDecision:
    action: str
    request: str
    subject: str = ""
    style: str = ""
    composition: str = ""
    background: str = ""
    use_reference: bool = False

    def context(self) -> dict[str, str]:
        return {
            "subject": self.subject,
            "style": self.style,
            "composition": self.composition,
            "background": self.background,
        }


_FOLLOWUP = (
    "再来", "再画", "再生成", "重新生成", "换成", "改成", "换个", "换一", "保持",
    "不变", "一样", "类似", "参考上一", "沿用", "把", "做成", "重做",
    "我是说", "我说的是", "应该是",
)
_REFERENCE = (
    "一样", "类似", "保持", "不变", "参考", "沿用", "改", "换", "把",
    "我是说", "我说的是", "应该是",
)
_QUESTION = ("哪里", "在哪", "为什么", "是什么", "什么意思", "怎么", "如何", "吗？", "吗?")
_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _fallback_action(request: str, previous: CreativeContext | None) -> str:
    text = request.strip()
    if any(cue in text for cue in _QUESTION) and not any(
        cue in text for cue in ("画", "生成", "制作", "设计")
    ):
        return "chat"
    if (previous is not None and text.startswith(("生成", "再生成", "再帮我生成"))
            and not any(cue in text for cue in ("文案", "文章", "文字", "报告", "视频"))):
        return "image.generate"
    if previous is not None and any(cue in text for cue in _FOLLOWUP):
        return "image.edit" if any(cue in text for cue in _REFERENCE) else "image.generate"
    plan = IntentPlanner().plan(text, image_context=previous is not None)
    return "image.generate" if plan.intent == "image.generate" and plan.needs_execution else "chat"


def _text(value: Any) -> str:
    return value.strip()[:180] if isinstance(value, str) else ""


def _initial_subject(request: str) -> str:
    subject = re.sub(
        r"^(?:(?:我是说|我说的是|应该是)|(?:请|麻烦)?(?:再|重新)?(?:帮我|给我|为我)?)"
        r"(?:生成|画|绘制|制作|来|给)?(?:一张|一个|一幅|张)?",
        "", request.strip(), count=1,
    ).strip("，。！! ")
    return subject or request.strip()


def plan_creative_turn(
    request: str, previous: CreativeContext | None, *, trace_id: str,
    classify: Callable[..., Any] | None = None,
) -> CreativeDecision:
    """Understand a natural-language turn; fall back safely if the text model is unavailable."""
    fallback = _fallback_action(request, previous)
    if request.strip().lower() in {"你好", "您好", "hi", "hello", "谢谢", "好的", "在吗"}:
        return CreativeDecision(action="chat", request=request)
    if fallback == "image.generate" and previous is None:
        style = next((word for word in (
            "卡通", "写实", "动漫", "水彩", "油画", "像素", "赛博朋克", "摄影"
        ) if word in request), "")
        background = re.search(r"背景(?:保持不变|[为是：:]([^，。；;]{2,32}))", request)
        return CreativeDecision(
            action="image.generate", request=request,
            subject=_initial_subject(request),
            style=style,
            composition="头像构图" if "头像" in request else "",
            background=background.group(1) if background and background.lastindex else "",
        )
    if fallback == "chat" and any(cue in request for cue in _QUESTION):
        # Plain factual questions remain in the normal streaming chat path.
        return CreativeDecision(action="chat", request=request)
    options: dict[str, Any] = {}
    try:
        context = previous.__dict__ if previous else {}
        response = (classify or chat)([
            {"role": "system", "content": (
                "你是首页创作意图规划器，不是回复用户的聊天助手。只输出 JSON 对象："
                "action 只能是 chat/image.generate/image.edit；subject、style、composition、"
                "background 是本次画面的简短事实；use_reference 是布尔值。"
                "用户想看到新图或修改上张图时直接选择生图，不索要尺寸、数量、风格或确认。"
                "上一张图仅在用户要求相同、类似、保留或修改时继承；用户明确的新主体优先。"
                "不能编造用户未提及的角色、背景。问图片在哪、解释问题等应选 chat。"
            )},
            {"role": "user", "content": json.dumps({
                "request": request, "last_completed_image": context,
            }, ensure_ascii=False)},
        ], temperature=0, timeout_s=8)
        raw = _JSON.search((response.content or "").strip())
        loaded = json.loads(raw.group()) if raw else {}
        if isinstance(loaded, dict):
            options = loaded
    except Exception as error:
        logger.bind(
            component="creative-planner", trace_id=trace_id,
            error_id=f"ERR-{uuid4().hex[:8].upper()}",
        ).exception(
            "创作意图模型不可用，使用本地保守路由：{}", type(error).__name__,
        )
    suggested = options.get("action")
    action = suggested if suggested in {"chat", "image.generate", "image.edit"} else fallback
    if (fallback == "chat" and previous is None and action.startswith("image.")
            and not any(cue in request for cue in (
                "画", "图", "像", "看", "场景", "风景", "城市", "人物", "封面",
            ))):
        action = "chat"
    # An explicit edit/follow-up must not be demoted to a form-like chat exchange.
    if fallback != "chat" and action == "chat":
        action = fallback
    if fallback == "image.edit":
        # Explicit reference/edit language wins over a nondeterministic model label.
        action = "image.edit"
    if (fallback == "image.generate" and previous is not None
            and not any(cue in request for cue in _REFERENCE)):
        action = "image.generate"
    if action == "image.edit" and previous is None:
        action = "image.generate"
    if action == "chat":
        return CreativeDecision(action="chat", request=request)
    use_reference = bool(previous and (
        action == "image.edit" or (
            options.get("use_reference") is True
            and any(cue in request for cue in _REFERENCE)
        )
    ))
    if previous and any(cue in request for cue in _REFERENCE):
        use_reference = True
    inherited = previous if use_reference else CreativeContext()
    proposed_subject = _text(options.get("subject"))
    explicit_change = re.search(r"(?:换成|改成)([^，。；;]{1,40})", request)
    if not explicit_change:
        explicit_change = re.search(r"把([^，。；;]{1,30}?)(?:的|做成|改成|换成)", request)
    if not explicit_change:
        explicit_change = re.search(r"(?:我是说|我说的是|应该是)([^，。；;]{1,50})", request)
    change_subject = _text(explicit_change.group(1)) if explicit_change else ""
    if any(cue in request for cue in ("换个人物", "换一个人物", "换个人")):
        change_subject = "与上一张不同的新人物"
    if change_subject:
        subject = change_subject
    elif proposed_subject and (
        proposed_subject in request or (previous and proposed_subject == previous.subject)
    ):
        subject = proposed_subject
    else:
        subject = inherited.subject or _initial_subject(request)[:180]
    background_change = re.search(
        r"背景(?:换成|改成|设为|变成|为|是)([^，。；;]{1,32})", request,
    )
    style_change = re.search(
        r"(?:风格|画风)(?:换成|改成|设为|变成|为|是)([^，。；;]{1,32})", request,
    )
    explicit_style = next((word for word in (
        "卡通", "写实", "动漫", "水彩", "油画", "像素", "赛博朋克", "摄影",
    ) if word in request), "")
    if "背景保持不变" in request:
        background = inherited.background
    elif background_change:
        background = _text(background_change.group(1))
    elif any(cue in request for cue in ("换个背景", "换背景")):
        background = ""
    else:
        background = _text(options.get("background")) or inherited.background
    if style_change:
        style = _text(style_change.group(1))
    elif explicit_style:
        style = explicit_style
    elif any(cue in request for cue in ("换个风格", "换风格")):
        style = ""
    else:
        style = _text(options.get("style")) or inherited.style
    # The exact current request is included in the compiled prompt even if this summary is wrong.
    return CreativeDecision(
        action=action, request=request, subject=subject,
        style=style,
        composition=(inherited.composition if "构图保持不变" in request else
                     _text(options.get("composition")) or inherited.composition),
        background=background,
        use_reference=use_reference,
    )


def compile_image_prompt(
    decision: CreativeDecision, previous: CreativeContext | None,
) -> str:
    """Compile a literal user request plus bounded context into one image instruction."""
    if decision.action not in {"image.generate", "image.edit"}:
        raise ValueError("只有图片请求可编译生图提示词")
    lines = [f"当前用户原话（最高优先级，逐字遵守）：{decision.request.strip()}"]
    if decision.use_reference and previous is not None:
        lines.append("输入的参考图是当前聊天上一张真实图片；按用户本轮要求修改它。")
        if previous.subject:
            lines.append(f"参考图此前主体：{previous.subject}")
        lines.append("只继承用户要求保留的属性；用户指定新主体时不得继续使用旧主体。")
    if decision.subject:
        lines.append(f"本次主体：{decision.subject}")
    for label, value in (
        ("画风", decision.style), ("构图", decision.composition),
        ("背景", decision.background),
    ):
        if value:
            lines.append(f"{label}：{value}")
    lines.append("默认生成一张图片。不要添加用户未要求的其他主角。")
    return "\n".join(lines)


def creative_brief(decision: CreativeDecision) -> str:
    """Describe the planned image in user language, without parroting the command."""
    subject = decision.subject.strip() or _initial_subject(decision.request)
    if decision.use_reference:
        opening = f"我会沿用上一张图的画面基础，以{subject}为这次的主体"
    else:
        opening = f"我理解这次要呈现的是{subject}"
    details = []
    if decision.style:
        details.append(f"采用{decision.style}画风")
    elif any(word in subject for word in ("角色", "国王", "动画", "动漫", "卡通")):
        details.append("突出角色辨识度与卡通感")
    else:
        details.append("保持主体清楚、画面自然")
    if decision.composition:
        details.append(f"以{decision.composition}呈现")
    if decision.background:
        details.append(f"背景保留{decision.background}")
    return f"{opening}，{'，'.join(details)}。"
