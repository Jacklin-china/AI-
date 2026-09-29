"""Comic 导演决策：选择最小上下文，产出可编辑方案而非图片 Prompt。"""

from __future__ import annotations

import json
from collections.abc import Callable

from loguru import logger
from pydantic import ValidationError

from kantoku.config import ToolError

from .models import (
    CinematographyPlan,
    ComicAsset,
    ComicProjectSnapshot,
    CreativeDecision,
    DirectorPlan,
    DirectorSpecDraft,
)
from .projects import ComicContextBuilder

DirectorModel = Callable[[list[dict[str, str]]], str]


def execute_director_stage(
    skill_id: str, inputs: dict, context: dict, *, model_call: DirectorModel,
) -> dict:
    """现有共享文本能力的阶段适配，不创建 Agent/Provider 或另一套导演流程。"""
    contracts = {
        "comic.creative_understanding": (
            "creative_decision", CreativeDecision,
            "理解故事、人物处境、观众感受与叙事目标，不决定摄影参数、不输出 Prompt。"
            "hard_constraints 必须逐字保留 CreativeBrief 的硬约束，不能自行新增。",
        ),
        "comic.visual_direction": (
            "director_plan", DirectorPlan,
            "依据具体故事与人物关系提出视觉策略，并公开说明视觉选择的因果理由。"
            "不得把情绪关键词固定映射为镜头公式，不得改变用户意图或固定资产。",
        ),
        "comic.cinematography": (
            "cinematography", CinematographyPlan,
            "把导演策略转成具体摄影语言，说明空间、光源、色彩和材质如何服务叙事。"
            "当前为图片方案，movement 应为 null。不要输出供应商参数或生图 Prompt。",
        ),
    }
    if skill_id not in contracts:
        raise ToolError("不支持的导演模型阶段", detail=skill_id)
    key, model, instruction = contracts[skill_id]
    raw = model_call([
        {"role": "system", "content": (
            instruction + " 只返回符合下述 Schema 的公开决策 JSON 对象，"
            "当前任务是对当前作品的补充；原始 Brief 和固定资产仍是约束。"
            "只依据此次提供的上下文，不沿用其他作品或历史示例的主体。"
            "未知细节只作为可编辑的创作选择，不得冒充用户硬约束。"
            "不输出私有思维链、reasoning 或 CoT："
            + json.dumps(model.model_json_schema(), ensure_ascii=False)
        )},
        {"role": "user", "content": json.dumps({
            "inputs": inputs, "context": context["context"],
        }, ensure_ascii=False)},
    ])
    try:
        output = model.model_validate_json(raw)
    except ValidationError as error:
        issues = [{"path": ".".join(map(str, item["loc"])), "type": item["type"]}
                  for item in error.errors(include_input=False, include_context=False,
                                           include_url=False)]
        logger.warning("Director Debug Schema skill_id={} issues={}", skill_id, issues)
        raise ToolError("导演阶段未返回有效的公开决策", detail=skill_id) from None
    except ValueError:
        raise ToolError("导演阶段未返回有效的公开决策", detail=skill_id) from None
    return {key: output.model_dump()}


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
