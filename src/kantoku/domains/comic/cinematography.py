"""摄影输出适配：只整理真实公开决策，不用情绪词推导摄影公式。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from kantoku.config.observability import public_error, redact_secrets

from .models import CinematographyPlan

ALIASES = {"lighting_direction": "light_direction", "depth_of_field": "depth_strategy",
           "lens": "lens_or_spatial_feel", "spatial_relationship": "spatial_feel"}
PUBLIC_KEYS = {"summary", "composition_strategy", "creative_reason"}
EXECUTION_FIELDS = ("shot_size", "camera_angle", "light_source", "light_direction",
                    "color_relationship")
PLAN_EXECUTION_FIELDS = (
    "visual_focus", "subject_environment_relation", "composition_strategy", "color_strategy",
    "style_boundary", "character_expression", "character_pose", "character_presence",
)
ALTERNATIVES = re.compile(
    r"或者|或是|还是|抑或|二选一|任选|可选|视情况|(?:\b(?:or|either|alternatively)\b)|"
    r"(?<!不)或|渐进|切换|先.{0,15}(?:后|再)|(?:远景|全景|中景|近景|特写)\s*[/／→至到]",
    re.IGNORECASE,
)
UNDECIDED = re.compile(r"待定|未定|待确认|任选|取决于|按当前|依据当前|后续确定|视情况")
SHOT_SIZE = re.compile(
    r"远景|全景|中景|近景|特写|全身|半身|胸像|腰部|"
    r"\b(?:wide|long|full|medium|close[ -]?up|extreme close[ -]?up|CU|MS|LS|WS)\b", re.I,
)
CAMERA_POSITION = re.compile(
    r"平视|俯|仰|低机位|高机位|略低|略高|水平|正面|侧面|鸟瞰|顶视|倾斜|眼睛高度|"
    r"\b(?:eye[ -]?level|low|high|level|overhead|side|front|dutch|top)\b", re.I,
)


def _decision_text(value: str) -> str:
    # “不使用仰拍或俯拍”是排除说明，不能算两个被选中的机位。
    negative = re.compile(
        r"^(?:全图|画面|人物|环境)?(?:不(?:采用|使用|表现|出现|新增|引入|依靠|设|做|含|打开|"
        r"保留|切|夸张|直视|靠|奔跑|跳跃|施法|挥剑|移动)|不要|禁止|避免|而非)"
    )
    return "，".join(part for part in re.split(r"[，,。；;\n]", value)
                    if not negative.search(part.strip()))


def unresolved_execution(values: Mapping[str, Any], fields: tuple[str, ...]) -> list[str]:
    """候选表达不能提升为最终执行参数；只识别冲突，不替导演选择。"""
    unresolved = []
    for name in fields:
        value = values.get(name)
        if value is None and name in PLAN_EXECUTION_FIELDS:
            continue  # 可选人物表现字段未设计，不能伪造一份默认设定。
        if isinstance(value, str):
            value = _decision_text(value)
        if (not isinstance(value, str) or not value.strip() or ALTERNATIVES.search(value)
                or UNDECIDED.search(value)):
            unresolved.append(name)
            continue
        if name == "shot_size":
            if (not SHOT_SIZE.search(value)
                    or len(set(re.findall("远景|全景|中景|近景|特写", value))) > 1):
                unresolved.append(name)
        elif name == "camera_angle":
            # 俯拍/俯视同义，只要一个机位即可。
            # 机位高度与视线角度是两件事：低机位平视可以是同一个确定机位。
            groups = (r"低角度|仰拍|仰视|略仰|\blow[ -]?angle\b",
                      r"平视|水平机位|水平视角|眼睛高度|\beye[ -]?level\b",
                      r"俯拍|俯视|略俯|鸟瞰|顶视|\b(?:high[ -]?angle|overhead)\b")
            if (not CAMERA_POSITION.search(value)
                    or sum(bool(re.search(group, value, re.I)) for group in groups) > 1):
                unresolved.append(name)
        elif (name == "light_direction" and re.search("左右|两侧|四面|前后", value)
              or name == "light_source" and re.search("与|和|、|及", value) and "主" not in value):
            unresolved.append(name)
    return unresolved


def require_execution_choices(plan: CinematographyPlan) -> CinematographyPlan:
    unresolved = [name for name in unresolved_execution(plan.model_dump(), EXECUTION_FIELDS)
                  if getattr(plan, name) is not None]
    return CinematographyPlan.model_validate({
        **plan.model_dump(), "unresolved_decisions": unresolved,
        "status": "needs_revision" if unresolved else plan.status,
    })


def _issues(error: Exception) -> dict[str, Any]:
    return {"type": type(error).__name__, "fields": [
        {"path": ".".join(map(str, item["loc"])), "type": item["type"]}
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    ] if isinstance(error, ValidationError) else []}


def _json(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("```json") or text.startswith("```\n"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def public_output(raw: Any) -> str:
    """有界脱敏公开快照；私有推理和未知 JSON 键不进入 Run/Event。"""
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False, default=str)
    text = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", raw,
                  flags=re.DOTALL | re.IGNORECASE)
    try:
        data = _json(text)
    except (ValueError, TypeError):
        # 含私有字段的损坏 JSON 不能作为公开原文保存。
        if re.search(r'\b(reasoning|chain_of_thought|cot|analysis)\b', text, re.I):
            return ""
        return redact_secrets(text)[:4000]
    if isinstance(data, dict):
        allowed = set(CinematographyPlan.model_fields) | set(ALIASES) | PUBLIC_KEYS
        safe = {key: value for key, value in data.items() if key in allowed}
        for key in ("cinematography", "structured_plan", "public_decision"):
            value = data.get(key)
            if isinstance(value, dict):
                safe[key] = {name: item for name, item in value.items() if name in allowed}
            elif key == "public_decision" and isinstance(value, str):
                safe[key] = value
        text = json.dumps(safe, ensure_ascii=False)
    else:
        text = ""
    return redact_secrets(text)[:4000]


def _normalize(value: Any) -> CinematographyPlan:
    if not isinstance(value, Mapping):
        raise ValueError("摄影输出不是公开方案对象")
    data = dict(value)
    if "cinematography" in data:
        data = dict(data["cinematography"])
    elif "structured_plan" in data:
        envelope = data
        data = dict(envelope["structured_plan"])
        public = envelope.get("public_decision")
        if isinstance(public, Mapping):
            if set(public) - PUBLIC_KEYS:
                raise ValueError("公开决策含非公开字段")
            if any(item is not None and not isinstance(item, str) for item in public.values()):
                raise ValueError("公开决策必须是文字说明")
            public = "；".join(item for item in public.values() if item) or None
        data["public_decision"] = public
        data["creative_reason"] = envelope.get("creative_reason")
        if set(envelope) - {"structured_plan", "public_decision", "creative_reason"}:
            raise ValueError("摄影输出含非公开字段")
    for alias, name in ALIASES.items():
        if alias in data:
            data.setdefault(name, data.pop(alias))
    for name, value in data.items():
        if name != "status" and isinstance(value, str) and not value.strip():
            data[name] = None
    # 构图归导演层；这里只保存模型实际给出的公开说明，不改写 DirectorPlan。
    composition = data.pop("composition_strategy", None)
    if composition:
        data["public_decision"] = "；".join(filter(None, (
            data.get("public_decision"), composition,
        )))
    if data.get("status") != "missing":
        data["status"] = "needs_revision"
        partial = CinematographyPlan.model_validate(data)
        data["status"] = "needs_revision" if partial.missing_fields else "complete"
        if len(partial.missing_fields) == 11:
            data["status"] = "missing"
    return CinematographyPlan.model_validate(data)


def parse_cinematography(
    raw: Any, *, model_call: Callable[[list[dict[str, str]]], str] | None = None,
) -> tuple[CinematographyPlan, dict[str, Any]]:
    """旧 JSON/新 envelope 直接解析；公开文字只允许一次语义提取，不重新导演。"""
    public = public_output(raw)
    diagnostics = {"raw_output": public, "raw_sha256": hashlib.sha256(
        str(raw).encode()).hexdigest(), "errors": [], "extraction_used": False}
    try:
        return _normalize(_json(raw) if isinstance(raw, str) else raw), diagnostics
    except (ValidationError, ValueError, TypeError) as error:
        diagnostics["errors"].append(_issues(error))
    if model_call is not None and public:
        diagnostics["extraction_used"] = True
        try:
            extracted = model_call([
                {"role": "system", "content": (
                    "你是公开摄影方案解析器，不是导演。只提取原文明确表达的摄影决策。"
                    "不补充缺失信息，不使用情绪到镜头的固定映射，不输出私有推理。"
                    "未提及的字段填 null，status 填 needs_revision。只返回 JSON："
                    + json.dumps(CinematographyPlan.model_json_schema(), ensure_ascii=False)
                )},
                {"role": "user", "content": public},
            ])
            diagnostics["extracted_output"] = public_output(extracted)
            plan = _normalize(_json(extracted))
            plan = CinematographyPlan.model_validate({
                **plan.model_dump(), "public_decision": public,
            })
            return plan, diagnostics
        except (ValidationError, ValueError, TypeError) as error:
            diagnostics["errors"].append(_issues(error))
        except Exception as error:
            diagnostics["failure"] = public_error(
                error, component="comic.cinematography_parser", skill_id="comic.cinematography",
            )
    # 没有结构化字段时也不造默认镜头；原公开文字可供用户/下一次显式修订查看。
    return CinematographyPlan(status="missing", public_decision=public or None), diagnostics
