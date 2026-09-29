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
