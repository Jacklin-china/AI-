"""Comic 导演决策：选择最小上下文，产出可编辑方案而非图片 Prompt。"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import uuid4

from loguru import logger
from pydantic import ValidationError

from kantoku.config import ToolError

from .cinematography import (
    PLAN_EXECUTION_FIELDS,
    parse_cinematography,
    require_execution_choices,
    unresolved_execution,
)
from .director_provenance import project_decision_fields, provenance_context, record_changes
from .models import (
    CinematographyPlan,
    ComicAsset,
    ComicProjectSnapshot,
    CreativeDecision,
    DirectorEvidence,
    DirectorPlan,
    DirectorSpecDraft,
)
from .projects import ComicContextBuilder

DirectorModel = Callable[[list[dict[str, str]]], str]


def _tracked_model(model_call: DirectorModel, context: dict) -> tuple[DirectorModel, list[str]]:
    requests: list[str] = []

    def call(messages: list[dict[str, str]]) -> str:
        # 应用请求关联 ID；不冒充 Provider response ID，不保存密钥或私有推理。
        request_id = f"director-request-{uuid4().hex}"
        requests.append(request_id)
        with logger.contextualize(model_request_id=request_id):
            logger.bind(**context).info("director_model_request_started request_id={}", request_id)
            return model_call(messages)

    return call, requests


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
    execution_required = context.get("execution_plan_required", False)
    if execution_required:
        instruction += (
            " 本轮是可执行的单张图片方案，不是候选建议。对未知细节允许作可编辑的模型选择，"
            "但不能声称为用户事实。色彩方案必须确定。摄影层必须各选且只选一个最终景别、"
            "机位、主光源、主光方向及色彩关系；辅助光可在 lighting 说明。"
            "不要在最终参数写‘或者’、任选、多个景别、左右候选、镜头切换或渐进。"
            "director_plan.color_strategy 仅用一个简短句子确定整体色彩方案，不列举"
            "可替换的服装、光源、装饰位置或多个点缀颜色，详细依据写入 creative_choices。"
            "摄影最终字段使用简短明确值：shot_size 只写一个景别名称；camera_angle 只写"
            "最终机位；light_source 只写主光名称；light_direction 只写一个方向；"
            "color_relationship 只写最终整体色彩关系。解释写入 creative_reason 或 lighting。"
            "其他执行字段（构图、人物姿态、表情、服饰、环境关系）也须选择一个明确设计，"
            "不要在同一字段留下互斥候选。未指定的信息可由模型决定，并说明是创作选择。"
        )
    output_instruction = (
        " 可以返回自然语言摄影方案，或 public_decision、structured_plan、creative_reason "
        "三个字段的公开对象；也兼容已有摄影字段 JSON。不要为了填满字段编造信息。"
        if skill_id == "comic.cinematography"
        else " 只返回符合下述 Schema 的公开决策 JSON 对象，"
    )
    knowledge = context.get("director_skill_context")
    knowledge_instruction = (
        "\nDirector Skill Context（专业方法；不代表当前作品事实，不能覆盖用户或资产约束）：\n"
        + knowledge["content"] + "\n请依据本轮需求与资产应用相关方法并给出具体公开决策。\n"
        if knowledge else ""
    )
    tracked, requests = _tracked_model(model_call, {
        "trace_id": context.get("trace_id"), "run_id": context.get("run_id"),
        "skill_id": skill_id,
    })
    messages = [
        {"role": "system", "content": (
            instruction + output_instruction + knowledge_instruction +
            "本轮 Brief、当前任务和显式绑定资产是唯一创意依据，不预设为历史作品的补充。"
            "只依据此次提供的上下文，不沿用其他作品或历史示例的主体。"
            "未知细节只作为可编辑的创作选择，不得冒充用户硬约束。"
            "不输出私有思维链、reasoning 或 CoT："
            + json.dumps(model.model_json_schema(), ensure_ascii=False)
        )},
        {"role": "user", "content": json.dumps({
            "inputs": inputs, "context": context["context"],
        }, ensure_ascii=False)},
    ]
    raw = tracked(messages)
    if skill_id == "comic.cinematography":
        plan, diagnostics = parse_cinematography(raw, model_call=tracked)
        if execution_required:
            plan = require_execution_choices(plan)
            if plan.unresolved_decisions and not plan.missing_fields:
                resolved = tracked([*messages, {"role": "assistant", "content": json.dumps(
                    plan.model_dump(), ensure_ascii=False)}, {"role": "user", "content": (
                        "仅收敛这些最终参数为一个决定，不改变用户事实或资产："
                        + ",".join(plan.unresolved_decisions)
                        + "。camera_angle 的视线角度只能选择平视、仰拍、俯拍或顶视之一，"
                        "可补充机位高度，但不能同时写平视与略仰/略俯。"
                        "shot_size 仅保留一个景别；主光源与方向只留一个；色彩只留一个方案。"
                        "请输出完整摄影方案。尚不能决定的字段保留 null，不编造用户要求。"
                    )}])
                plan, corrected = parse_cinematography(resolved)
                diagnostics = {**diagnostics, "decision_refinement": corrected}
                plan = require_execution_choices(plan)
        return {"cinematography": plan.model_dump(), "parse_diagnostics": diagnostics,
                "_model_requests": requests}
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("additionalProperties") is False:
            # Some JSON-mode models echo the Schema's validation annotation.
            # Discard only this exact non-business marker, never unknown fields,
            # missing decisions, identity changes or private reasoning.
            data.pop("additionalProperties")
            logger.bind(skill_id=skill_id, trace_id=context.get("trace_id"),
                        run_id=context.get("run_id")).warning(
                "director_schema_annotation_ignored field=additionalProperties"
            )
        output = model.model_validate(data)
        if (execution_required and skill_id == "comic.visual_direction"
                and (unresolved := unresolved_execution(
                    output.model_dump(), PLAN_EXECUTION_FIELDS,
                ))):
            refined = tracked([*messages, {"role": "assistant", "content": json.dumps(
                output.model_dump(), ensure_ascii=False)}, {"role": "user", "content": (
                    "仅收敛这些执行字段为明确决定：" + ",".join(unresolved)
                    + "。可编辑表示用户以后可改，不表示本轮保留候选。服饰、姿态、表情、"
                    "环境、色彩都各选一个设计，例如双手垂于身侧或拢袖必须选其中一个。"
                    "不要用‘或’、‘或者’、‘还是’或‘任选’列出候选；其余用户约束和资产保持。"
                    "输出完整 DirectorPlan JSON。"
                )}])
            output = model.model_validate_json(refined)
    except ValidationError as error:
        issues = [{"path": ".".join(map(str, item["loc"])), "type": item["type"]}
                  for item in error.errors(include_input=False, include_context=False,
                                           include_url=False)]
        logger.warning("Director Debug Schema skill_id={} issues={}", skill_id, issues)
        raise ToolError("导演阶段未返回有效的公开决策", detail=skill_id) from None
    except ValueError:
        raise ToolError("导演阶段未返回有效的公开决策", detail=skill_id) from None
    if skill_id == "comic.creative_understanding":
        # These are user-owned input, not a field the model may infer or paraphrase.
        # In a new quick Brief the raw request is authoritative even when its
        # structured constraint list is empty. Critic still reviews that request.
        hard = context["context"]["creative_brief"]["hard_constraints"]
        if output.hard_constraints != hard:
            logger.bind(skill_id=skill_id, trace_id=context.get("trace_id"),
                        run_id=context.get("run_id")).warning(
                "director_constraint_normalized source=creative_brief"
            )
        output = output.model_copy(update={"hard_constraints": list(hard)})
    return {key: output.model_dump(), "_model_requests": requests}


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
    tracked, requests = _tracked_model(model_call, {"component": "comic.director"})
    raw = tracked(messages)
    try:
        draft = DirectorSpecDraft.model_validate_json(raw)
    except (ValueError, ValidationError) as error:
        raise ToolError("导演模型未返回有效的结构化方案", detail=type(error).__name__) from error
    hard_constraints = snapshot.creative_brief.hard_constraints
    constraints = list(dict.fromkeys([*hard_constraints, *draft.constraints]))
    if len(constraints) > 50:
        raise ToolError("导演方案约束过多")
    draft = draft.model_copy(update={
        "constraints": constraints, "field_provenance": {}, "execution_policy": None,
    })
    return record_changes(None, draft, source="model_choice", evidence=DirectorEvidence(
        source_type="model_choice", reference=requests[-1],
    ))


def revise_director_spec(
    snapshot: ComicProjectSnapshot, draft: DirectorSpecDraft, instruction: str,
    *, assets: list[ComicAsset], model_call: DirectorModel,
) -> DirectorSpecDraft:
    """显式修改当前草稿；只传本轮 Brief、绑定资产与公开方案。"""
    context = ComicContextBuilder.build(snapshot, assets=assets).model_dump()
    # 来源由服务端维护；不能要求编辑模型重写数十份依据或自报事实可信度。
    schema = DirectorSpecDraft.model_json_schema()
    schema["properties"].pop("field_provenance", None)
    for name in ("DirectorFieldProvenance", "DirectorEvidence"):
        schema.get("$defs", {}).pop(name, None)
    tracked, requests = _tracked_model(model_call, {"component": "comic.director_revision"})
    raw = tracked([
        {"role": "system", "content": (
            "根据用户修改指令编辑当前导演草稿，不生成新故事或 Prompt。"
            "保留用户硬约束、资产和来源版本，只输出公开决策，不输出思维链。"
            "critic_result 必须为 null，修改后需要重新审核。只返回符合 Schema 的 JSON："
            + json.dumps(schema, ensure_ascii=False)
        )},
        {"role": "user", "content": json.dumps({
            "context": context, "current_draft": draft.model_dump(
                mode="json", exclude={"field_provenance"}),
            "field_sources": provenance_context(draft),
            "revision_instruction": instruction,
        }, ensure_ascii=False)},
    ])
    try:
        revised = DirectorSpecDraft.model_validate_json(raw)
        revised = revised.model_copy(update={"execution_policy": draft.execution_policy})
        if revised.schema_version == 2 and draft.execution_policy == "single_image":
            revised = project_decision_fields(revised, draft)
            revised = revised.model_copy(update={
                "cinematography": require_execution_choices(revised.cinematography),
            })
        return record_changes(draft, revised, source="model_choice", evidence=DirectorEvidence(
            source_type="model_choice", reference=requests[-1],
        ))
    except (ValidationError, ValueError):
        raise ToolError("导演修改未返回有效公开草稿") from None
