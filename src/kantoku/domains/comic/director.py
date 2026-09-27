"""Comic 导演决策：选择最小上下文，产出可编辑方案而非图片 Prompt。"""

from __future__ import annotations

import json
from collections.abc import Callable

from pydantic import ValidationError

from kantoku.config import ToolError

from .models import ComicAsset, ComicProjectSnapshot, DirectorSpecDraft
from .projects import ComicContextBuilder

DirectorModel = Callable[[list[dict[str, str]]], str]


def plan_director_spec(
    snapshot: ComicProjectSnapshot, *, task: str | None, model_call: DirectorModel,
    assets: list[ComicAsset] | None = None,
) -> DirectorSpecDraft:
    """仅传 Project、当前 Brief、相关记忆和当前任务；不读取完整历史。"""
    context = ComicContextBuilder.build(snapshot, task=task, assets=assets)
    messages = [
        {
            "role": "system",
            "content": (
                "你是漫剧作品的导演。依据具体故事、角色状态、场景关系和当前任务，"
                "提出可编辑的视觉叙事决策；不要写生图 Prompt、供应商参数或固定情绪公式。"
                "不得改写用户硬约束；未知角色/场景不要假装已有资产。"
                "只返回 JSON 对象，字段为 visual_direction, storytelling_goal, "
                "camera_language, composition, lighting, color_language, emotion, "
                "character_focus, constraints, creative_choices。前八项均为有意义的文字；"
                "constraints 为字符串列表，creative_choices 为解释设计原因的字符串列表。"
            ),
        },
        {"role": "user", "content": json.dumps(context.model_dump(), ensure_ascii=False)},
    ]
    raw = model_call(messages)
    try:
        draft = DirectorSpecDraft.model_validate_json(raw)
    except (ValueError, ValidationError) as error:
        raise ToolError("导演模型未返回有效的结构化方案", detail=type(error).__name__) from error
    hard_constraints = snapshot.creative_brief.hard_constraints
    constraints = list(dict.fromkeys([*hard_constraints, *draft.constraints]))
    if len(constraints) > 50:
        raise ToolError("导演方案约束过多")
    return draft.model_copy(update={"constraints": constraints})
