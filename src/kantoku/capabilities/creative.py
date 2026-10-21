"""Lightweight creative planning for autonomous conversations.

The planner interprets a user turn with the scoped conversation context.
The compiler always carries the user's literal request into the provider prompt;
model suggestions may add context, but cannot silently replace that request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kantoku.config import ToolError
from kantoku.config.observability import public_error
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
    reference_artifact_id: str | None = None

    def context(self) -> dict[str, str]:
        return {
            "subject": self.subject,
            "style": self.style,
            "composition": self.composition,
            "background": self.background,
        }




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
    context_messages: list[dict[str, str]] | None = None,
) -> CreativeDecision:
    """Interpret references semantically; no keyword can force inheritance or reset."""
    try:
        response = (classify or chat)([
            {"role": "system", "content": (
                "理解用户本轮图片或聊天目标，依据会话记忆选择相关内容。只输出 JSON："
                "action=chat/image.generate/image.edit/video.generate；subject/style/"
                "composition/background 为本次可编辑创作选择，use_reference 为布尔值。"
                "旧图片只是候选来源，不默认继承；引用、纠正、新目标、否定和比较都按语义判断。"
                "不要把模型选择当用户事实。用户要求旧图时可返回 reference_artifact_id，"
                "只能引用当前会话提供的图片 ID。未知信息不编造成已确认事实。")},
            *(context_messages or []),
            {"role": "user", "content": json.dumps({
                "request": request, "last_completed_image": previous.__dict__ if previous else None,
            }, ensure_ascii=False)},
        ], temperature=0, timeout_s=8, response_format={"type": "json_object"})
        options = json.loads(response.content or "")
        if options.get("action") not in {"chat", "image.generate", "image.edit", "video.generate"}:
            raise ValueError("invalid creative action")
        if not isinstance(options.get("use_reference", False), bool):
            raise ValueError("invalid reference decision")
        reference_id = options.get("reference_artifact_id")
        use_reference = options.get("use_reference") is True
        if use_reference and not (reference_id or previous):
            raise ValueError("missing reference source")
        return CreativeDecision(
            action=options["action"], request=request, subject=_text(options.get("subject")),
            style=_text(options.get("style")), composition=_text(options.get("composition")),
            background=_text(options.get("background")), use_reference=use_reference,
            reference_artifact_id=reference_id or (previous.artifact_id if use_reference else None),
        )
    except Exception as error:
        public_error(error, component="creative.context", trace_id=trace_id)
        raise ToolError("创作意图判断失败，原请求保留；请重试") from error


def compile_image_prompt(
    decision: CreativeDecision, previous: CreativeContext | None,
) -> str:
    """Compile a literal user request plus bounded context into one image instruction."""
    if decision.action not in {"image.generate", "image.edit"}:
        raise ValueError("只有图片请求可编译生图提示词")
    lines = [f"当前用户原话（最高优先级，逐字遵守）：{decision.request.strip()}"]
    if decision.use_reference and previous is not None:
        lines.append("输入的是本轮选中的真实参考图；按用户本轮要求修改它。")
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
        opening = f"我会沿用所选参考图的画面基础，以{subject}为这次的主体"
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
