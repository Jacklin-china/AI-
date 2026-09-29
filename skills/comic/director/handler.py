"""Director Skill 的契约适配器。

这些 handler 只负责校验 Coordinator 传入的结构化结果并返回同一份数据。
模型调用、上下文召回和版本持久化由后续 ComicDirectorCoordinator 负责，
因此这里不导入任何 Provider，也不生成固定 Prompt。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from kantoku.config import ToolError
from kantoku.domains.comic.cinematography import parse_cinematography
from kantoku.domains.comic.models import (
    CinematographyPlan,
    CreativeDecision,
    DirectorCriticResult,
    DirectorPlan,
    DirectorSpecDraft,
)

ModelT = TypeVar("ModelT")


def _contract_output(inputs: Mapping[str, Any], key: str, model: type[ModelT]) -> dict[str, Any]:
    """验证 Coordinator 提供的输出；缺失时明确提示不能伪造导演结果。"""
    raw = inputs.get("_contract_output")
    if not isinstance(raw, Mapping) or key not in raw:
        raise ToolError(
            "导演 Skill 需要 Coordinator 提供结构化结果",
            detail=f"missing={key}",
        )
    value = model.model_validate(raw[key])  # type: ignore[attr-defined]
    return {key: value.model_dump()}  # type: ignore[attr-defined]


def execute_creative_understanding(
    inputs: Mapping[str, Any], _context: Mapping[str, Any]
) -> Mapping[str, Any]:
    return _contract_output(inputs, "creative_decision", CreativeDecision)


def execute_visual_direction(
    inputs: Mapping[str, Any], _context: Mapping[str, Any]
) -> Mapping[str, Any]:
    return _contract_output(inputs, "director_plan", DirectorPlan)


def execute_cinematography(
    inputs: Mapping[str, Any], _context: Mapping[str, Any]
) -> Mapping[str, Any]:
    raw = inputs.get("_contract_output")
    if not isinstance(raw, Mapping):
        raise ToolError("摄影 Skill 需要公开决策对象")
    value = raw.get("cinematography")
    if isinstance(value, CinematographyPlan):
        plan = value
    else:
        plan, _diagnostics = parse_cinematography(raw)
    return {
        "cinematography": plan.model_dump(),
        "public_decision": {"summary": plan.public_decision} if plan.public_decision else {},
        "structured_plan": plan.model_dump(exclude={"public_decision", "creative_reason"}),
        "creative_reason": plan.creative_reason,
    }


def execute_director_critic(
    inputs: Mapping[str, Any], _context: Mapping[str, Any]
) -> Mapping[str, Any]:
    return _contract_output(inputs, "critic_result", DirectorCriticResult)


def execute_director_assemble(
    inputs: Mapping[str, Any], _context: Mapping[str, Any]
) -> Mapping[str, Any]:
    return _contract_output(inputs, "director_spec", DirectorSpecDraft)
