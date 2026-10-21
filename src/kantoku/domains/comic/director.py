"""Comic 导演决策：选择最小上下文，产出可编辑方案而非图片 Prompt。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
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
from .director_provenance import (
    field_values,
    project_decision_fields,
    provenance_context,
    record_changes,
)
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


STAGE_OUTPUT_KEYS = ("creative_decision", "director_plan", "cinematography")


def local_director_stage(skill_id: str, inputs: dict, context: dict) -> dict:
    """模型不可用时的公开草稿；原文与固定设定保留，默认决定明确标为本地选择。

    只使用当前请求及已验证来源，不调用模型，也不生成审核结论或供应商 Prompt。
    该草稿仍须用户确认；本地摄影默认值不能被误标为用户事实。
    """
    brief = context["creative_brief"]
    original = context.get("current_task") or brief["original_request"]
    lock = (context.get("creative_context_lock") or {}).get("bibles", {})
    assets = context.get("relevant_assets", [])

    def asset_text(kind: str) -> str | None:
        values = []
        for asset in assets:
            details = asset.get("details", {})
            if details.get("kind") != kind:
                continue
            for name, value in details.items():
                if name != "kind" and value:
                    values.extend(value if isinstance(value, list) else [value])
        # 完整资产正文仍由原 asset_versions 引用；公开摘要遵守现有字段上限。
        text = "；".join(values)
        return text if text and len(text) <= 4000 else None

    if skill_id == "comic.creative_understanding":
        return {"creative_decision": CreativeDecision(
            intent_summary=original, narrative_context=brief["original_request"],
            emotional_target="忠实呈现本轮原始需求表达的感受",
            audience_experience="清楚辨认原始需求的主体、行为和场景关系",
            narrative_focus="以本轮原始需求为依据完成单张画面",
            hard_constraints=list(brief["hard_constraints"]),
            soft_preferences=list(brief.get("soft_preferences", [])),
            creative_freedom=list(brief.get("creative_freedom", [])),
        ).model_dump()}
    if skill_id == "comic.visual_direction":
        return {"director_plan": DirectorPlan(
            visual_strategy="本地保障草稿：直接执行创意理解中保存的本轮原始需求",
            visual_focus="原始需求指定的主体、动作与视觉重点",
            subject_environment_relation=lock.get("world_context") or asset_text("scene")
            or "主体处于原始需求指定的场景，保持明确的前后空间关系",
            composition_strategy="主体轮廓清晰，完整保留原始需求要求展示的范围",
            color_strategy="保持原始需求指定的颜色，未指定区域使用统一的自然色彩",
            style_boundary=lock.get("style_context") or asset_text("style")
            or "保持原始需求指定的风格，未指定部分采用自然材质表现",
            character_presence=lock.get("character_context") or asset_text("character"),
            character_pose="严格呈现原始需求指定的动作与朝向",
            character_expression="严格呈现原始需求指定的神态",
            continuity_rules=["用户原始需求和固定资产优先于本地默认选择"],
            creative_choices=["模型暂不可用，当前使用可编辑的本地保障方案；默认值不是用户事实"],
            risk_flags=["本地保障草稿，尚无当前内容的模型审核，需人工确认"],
        ).model_dump()}
    if skill_id != "comic.cinematography":
        raise ToolError("不支持的本地导演阶段", detail=skill_id)
    # 仅逐字提取用户明确指定的景别/视角；其他默认选择不按情绪套摄影公式。
    shot = re.search(r"大特写|特写|中近景|近景|中景|远景|全景", original)
    angle = re.search(r"仰拍|仰视|俯拍|俯视|顶视|鸟瞰|平视", original)
    plan = inputs.get("director_plan", {})
    return {"cinematography": CinematographyPlan(
        public_decision="本地保障摄影方案；用户指定参数及固定设定优先",
        creative_reason="为原始需求提供确定的单张画面执行参数，未指定参数属于本地可编辑选择",
        shot_size=shot.group() if shot else "全景",
        camera_angle=angle.group() if angle else "平视",
        camera_distance="以原始需求要求展示的主体范围确定拍摄距离",
        camera_language="静态单张画面，保持用户指定的景别和视角",
        spatial_feel="保持主体、前景与背景的明确空间层次",
        lens_or_spatial_feel="自然透视，主体比例清晰",
        lighting="保留原始需求中的光照，未指定处使用单一柔光塑形",
        light_source="主光：场景柔光", light_direction="前侧方",
        color_relationship=plan.get("color_strategy") or "自然统一色彩",
        depth_strategy="主体清晰，背景保持可辨识的场景层次",
        material_language="保持用户指定材质，未指定处采用自然纹理",
    ).model_dump()}


def _drop_sibling_stage_fields(
    data: Any, model: Any, skill_id: str,
) -> Any:
    """丢弃模型顺带返回的兄弟阶段字段，不触碰本阶段的真实内容。

    模型偶尔会把整份方案一起返回；Schema 是 extra=forbid，直接校验会让
    整个导演流程因多余字段崩溃。只移除“其他阶段”的顶层键，
    本阶段字段与其他未知键保持原样，交给原有校验处理。
    """
    if not isinstance(data, dict):
        return data
    allowed = set(getattr(model, "model_fields", ()))
    extras = [
        key for key in STAGE_OUTPUT_KEYS
        if key not in allowed and key in data
    ]
    if not extras:
        return data
    trimmed = {key: value for key, value in data.items() if key not in extras}
    logger.bind(skill_id=skill_id).warning(
        "director_sibling_stage_fields_dropped fields={}", extras)
    return trimmed


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
    if context.get("context", {}).get("creative_context_lock"):
        instruction += (
            " creative_context_lock 是本轮绑定的当前创作设定，"
            "来源为指定导演修订或 Prompt Artifact。"
            "director_confirmed 表示是否已经人工确认；未确认的草稿不冒充已确认事实。"
            "只沿用 bibles 和 sources 中本轮实际选中的字段，其余设定按当前目标决定。"
            "指代依据本轮 conversation_context.resolution，不固定指向最新方案。"
            "模型补充的摄影、光线和姿态是创作选择，不能自动升级为用户事实。"
        )
    if context.get("context", {}).get("conversation_context", {}).get("independent_plan"):
        instruction += (
            " 本轮生成独立的新画面。旧导演描述可能混有当时的动作、朝向、表情和构图，"
            "整类人物/场景/风格引用仅提取稳定身份、外貌、服装、地点与风格。"
            "旧画面的瞬时姿态和镜头要求不能冻结到新画面；按本轮动作重新安排表情、"
            "视线、朝向、构图和摄影。明确指定的叶子字段及固定资产约束仍须保留。"
            "若 image_references.continuity 存在，则采用其更严格的视觉延续规则："
            "仅 changes 列出的叶子字段可变，其余画面元素和摄影保留；独立方案不表示重新设计。"
        )
    execution_required = context.get("execution_plan_required", False)
    if context.get("context", {}).get("image_references"):
        instruction += (
            " image_references 绑定明确图片版本及 identity/style/composition 用途和目标对象。"
            "只能在选定用途内沿用参考，不把风格参考的人物身份自动继承。"
            "observation_status=not_analyzed 表示尚未视觉读取，不能声称看到了图中细节；"
            "保留来源绑定供后续外部图片模型使用，缺少可见事实时明确说明。"
        )
    if context.get("context", {}).get("generation_feedback"):
        instruction += (
            " generation_feedback 是系统方案质量检查。请在本导演阶段修正相应问题，"
            "补齐执行决策并保留 Brief、固定资产及锁定 Bible；不让用户决定缺失的摄影参数。"
            "generation_candidate 是待优化的同一方案，只改反馈涉及的镜头字段，"
            "保留其中角色身份、服装、世界地点与时间、艺术风格；不得重新创作一套设定。"
        )
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
            "creative_decision.narrative_context 写明角色身份及世界地点、时间，不能只有情绪。"
            "director_plan.character_presence 写固定身份、外貌、年龄、服装和特征；"
            "characters_present 根据本轮故事与要求判断是否有人物，返回布尔值。"
            "若为 false，character_presence、character_pose、character_expression 均为 null；"
            "以环境或物体为主体时，仍在 visual_focus 和构图等字段设计主体，不虚构人物。"
            "style_boundary 写艺术/摄影风格与材质；人物位置、视角和姿态分别在"
            "composition_strategy、character_pose 里确定，不混入固定人物设定。"
            "没有用户指定的摄影参数必须由本阶段依据 Skill 作最终选择，不交给审核决定。"
        )
    output_instruction = (
        " 可以返回自然语言摄影方案，或 public_decision、structured_plan、creative_reason "
        "三个字段的公开对象；也兼容已有摄影字段 JSON。不要为了填满字段编造信息。"
        if skill_id == "comic.cinematography" and not execution_required
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
            ("当前 Creative Context Lock、Brief 和当前任务是本轮创意依据，"
             "当前任务只修改未锁定的内容。" if context.get("context", {}).get(
                 "creative_context_lock") else
             "本轮 Brief、当前任务和显式绑定资产是唯一创意依据，不预设为历史作品的补充。") +
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
            incomplete = list(dict.fromkeys([*plan.missing_fields, *plan.unresolved_decisions]))
            if incomplete:
                resolved = tracked([*messages, {"role": "assistant", "content": json.dumps(
                    plan.model_dump(), ensure_ascii=False)}, {"role": "user", "content": (
                        "仅收敛或补全这些最终参数为一个决定，不改变用户事实或资产："
                        + ",".join(incomplete)
                        + "。camera_angle 的视线角度只能选择平视、仰拍、俯拍或顶视之一，"
                        "可补充机位高度，但不能同时写平视与略仰/略俯。"
                        "shot_size 仅保留一个景别；主光源与方向只留一个；色彩只留一个方案。"
                        "用户未指定时请根据本轮 Skill、人物与场景主动选择，标为模型创作选择，"
                        "不要编造用户要求，也不要继续填 null 或候选。输出完整摄影方案 JSON。"
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
        data = _drop_sibling_stage_fields(data, model, skill_id)
        try:
            output = model.model_validate(data)
        except ValidationError as error:
            # 模型偶尔会把兄弟阶段的字段一并返回。只重试一次结构，
            # 仍不合法才按原路径抛出，避免整个导演流程因多余字段崩溃。
            issues = [{"path": ".".join(map(str, item["loc"])), "type": item["type"]}
                      for item in error.errors(include_input=False, include_context=False,
                                               include_url=False)]
            logger.bind(skill_id=skill_id, trace_id=context.get("trace_id"),
                        run_id=context.get("run_id")).warning(
                "director_schema_repair_started issues={}", issues)
            repaired_raw = tracked([*messages, {"role": "assistant", "content": raw},
                                    {"role": "user", "content": (
                                        "上次返回的结构不符合本阶段 Schema，问题：" +
                                        json.dumps(issues, ensure_ascii=False) +
                                        "。只返回本阶段所属的顶层字段，不要附带其他阶段"
                                        "（如 director_plan、cinematography、creative_decision）"
                                        "的内容，其余内容原样保留。")}])
            repaired = json.loads(repaired_raw)
            if isinstance(repaired, dict) and repaired.get("additionalProperties") is False:
                repaired.pop("additionalProperties")
            output = model.model_validate(
                _drop_sibling_stage_fields(repaired, model, skill_id))
        unresolved = unresolved_execution(output.model_dump(), PLAN_EXECUTION_FIELDS) \
            if execution_required and skill_id == "comic.visual_direction" else []
        if execution_required and skill_id == "comic.visual_direction":
            required_fields = ("style_boundary",) if output.characters_present is False else (
                "style_boundary", "character_presence", "character_pose")
            unresolved.extend(name for name in required_fields if getattr(output, name) is None)
        if unresolved:
            refined = tracked([*messages, {"role": "assistant", "content": json.dumps(
                output.model_dump(), ensure_ascii=False)}, {"role": "user", "content": (
                    "仅收敛或补全这些执行字段为明确决定：" + ",".join(unresolved)
                    + "。可编辑表示用户以后可改，不表示本轮保留候选。服饰、姿态、表情、"
                    "环境、色彩都各选一个设计，例如双手垂于身侧或拢袖必须选其中一个。"
                    "不要用‘或’、‘或者’、‘还是’或‘任选’列出候选；其余用户约束和资产保持。"
                    "固定人物身份、外貌、服装写 character_presence；世界地点/时间写"
                    "subject_environment_relation；风格与材质写 style_boundary；"
                    "人物位置写 composition_strategy，视角与姿态写 character_pose。"
                    "未指定的内容由导演作创作选择，不交给用户补摄影参数。"
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
    reference_context: dict | None = None,
) -> DirectorSpecDraft:
    """仅传 Project、当前 Brief、相关记忆和当前任务；不读取完整历史。"""
    context = ComicContextBuilder.build(snapshot, task=task, assets=assets)
    if reference_context:
        context.stable_context["image_references"] = reference_context
    messages = [
        {
            "role": "system",
            "content": (
                "你是漫剧作品的导演。依据具体故事、角色状态、场景关系和当前任务，"
                "提出可编辑的视觉叙事决策；不要写生图 Prompt、供应商参数或固定情绪公式。"
                "不得改写用户硬约束；未知角色/场景不要假装已有资产。"
                "image_references 按选定用途和目标使用；not_analyzed 的图片尚未视觉读取，"
                "不能声称识别了图中细节，保留绑定供外部图片模型使用。"
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
        "reference_context": reference_context or {},
    })
    return record_changes(None, draft, source="model_choice", evidence=DirectorEvidence(
        source_type="model_choice", reference=requests[-1],
    ))


def revise_director_spec(
    snapshot: ComicProjectSnapshot, draft: DirectorSpecDraft, instruction: str,
    *, assets: list[ComicAsset], model_call: DirectorModel,
    creative_lock: dict | None = None,
    conversation_context: dict | None = None,
) -> DirectorSpecDraft:
    """显式修改当前草稿；只传本轮 Brief、绑定资产与公开方案。"""
    context = ComicContextBuilder.build(
        snapshot, assets=assets, creative_lock=creative_lock).model_dump()
    if conversation_context:
        context["conversation_context"] = conversation_context
    # Only changed public leaves are returned; reprinting the entire plan can truncate it.
    editable = field_values(draft)
    editable.pop("constraints", None)
    editable.pop("creative_decision.hard_constraints", None)
    properties = {}
    for path in editable:
        section, _, name = path.rpartition(".")
        model = getattr(draft, section) if section else draft
        properties[path] = model.model_json_schema()["properties"][name]
    schema = {
        "type": "object", "additionalProperties": False, "required": ["changes"],
        "properties": {"changes": {
            "type": "object", "additionalProperties": False, "minProperties": 1,
            "properties": properties,
        }},
    }
    tracked, requests = _tracked_model(model_call, {"component": "comic.director_revision"})
    raw = tracked([
        {"role": "system", "content": (
            "根据用户修改指令编辑当前导演草稿，不生成新故事或 Prompt。"
            "保留用户硬约束、资产和来源版本，只输出公开决策，不输出思维链。"
            "若有 creative_context_lock，沿用其未释放的 Character/World/Style Bible；"
            "后续动作、姿态与镜头要求只修改 Shot Direction。"
            "分析指令后只返回 changes 对象：键为需要修改的字段路径，值为调整后的完整字段。"
            "同时调整受影响的相关字段，避免表情、动作、光影和摄影描述相互矛盾。"
            "未涉及的字段不要返回；人物身份、外貌、服装、环境与风格保持原样，除非用户明确要求。"
            "不要返回整份草稿、审核结果、来源、版本或解释。每个值简洁明确，列表保持列表类型。"
            "修改后由系统另行审核。只返回符合 Schema 的 JSON："
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
        data = json.loads(raw)
        if isinstance(data, dict) and "changes" in data:
            changes = data["changes"]
            if (set(data) != {"changes"} or not isinstance(changes, dict) or not changes
                    or not set(changes) <= editable.keys()):
                raise ValueError("invalid_revision_fields")
            merged = draft.model_dump(mode="json")
            for path, value in changes.items():
                section, _, name = path.rpartition(".")
                target = merged[section] if section else merged
                target[name] = value
            merged.update(critic_result=None, critic_status=None)
            revised = DirectorSpecDraft.model_validate(merged)
        else:
            # Keep the existing full-draft contract readable for older model adapters.
            revised = DirectorSpecDraft.model_validate(data)
        revised = revised.model_copy(update={"execution_policy": draft.execution_policy,
                                             "reference_context": draft.reference_context})
        if revised.schema_version == 2 and draft.execution_policy == "single_image":
            revised = project_decision_fields(revised, draft)
            revised = revised.model_copy(update={
                "cinematography": require_execution_choices(revised.cinematography),
            })
        return record_changes(draft, revised, source="model_choice", evidence=DirectorEvidence(
            source_type="model_choice", reference=requests[-1],
        ))
    except (ValidationError, ValueError) as error:
        issues = [{"path": ".".join(map(str, item["loc"])), "type": item["type"]}
                  for item in error.errors(include_input=False, include_context=False,
                                           include_url=False)] \
            if isinstance(error, ValidationError) else [{"type": type(error).__name__}]
        logger.bind(component="comic.director_revision").warning(
            "director_revision_invalid_output response_length={} issues={}", len(raw), issues)
        raise ToolError("导演修改未返回有效公开草稿") from None
