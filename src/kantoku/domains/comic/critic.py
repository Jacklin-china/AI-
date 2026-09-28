"""导演方案生成前审核：确定性约束检查、语义审核与受限修订。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kantoku.config import ToolError
from kantoku.config.observability import (
    current_run_id,
    current_trace_id,
    public_error,
    redact_secrets,
)

from .models import (
    ComicAsset,
    ComicShot,
    ComicStoryboard,
    CreativeBriefInput,
    DirectorCriticFinding,
    DirectorCriticPatch,
    DirectorCriticResult,
    DirectorSpecDraft,
)

REVIEW_VERSION = "director-critic-1"
CRITIC_ALLOWED_FIELD_PATHS = frozenset({
    "creative_decision.intent_summary",
    "creative_decision.emotional_target",
    "creative_decision.narrative_focus",
    "creative_decision.audience_experience",
    "director_plan.visual_focus",
    "director_plan.composition_strategy",
    "director_plan.color_strategy",
    "director_plan.subject_environment_relation",
    "director_plan.style_boundary",
    "director_plan.character_expression",
    "director_plan.character_pose",
    "director_plan.character_presence",
    "cinematography.shot_size",
    "cinematography.camera_angle",
    "cinematography.camera_distance",
    "cinematography.camera_language",
    "cinematography.lighting",
    "cinematography.depth_strategy",
})
PATCH_FIELDS = frozenset({
    "creative_decision.emotional_target",
    "creative_decision.narrative_focus",
    "creative_decision.audience_experience",
    "director_plan.visual_focus",
    "director_plan.composition_strategy",
    "director_plan.color_strategy",
    "director_plan.style_boundary",
    "director_plan.character_expression",
    "director_plan.character_pose",
    "director_plan.character_presence",
    "cinematography.camera_angle",
    "cinematography.shot_size",
    "cinematography.camera_language",
    "cinematography.lighting",
    "cinematography.depth_strategy",
})
FIELD_PATH_ALIASES = {
    "creative_brief.original_request": "creative_decision.intent_summary",
    "emotion": "creative_decision.emotional_target",
    "emotional_target": "creative_decision.emotional_target",
    "narrative_focus": "creative_decision.narrative_focus",
    "audience_experience": "creative_decision.audience_experience",
    "visual_direction": "director_plan.visual_focus",
    "visual_focus": "director_plan.visual_focus",
    "composition_strategy": "director_plan.composition_strategy",
    "composition": "director_plan.composition_strategy",
    "color_direction": "director_plan.color_strategy",
    "color_language": "director_plan.color_strategy",
    "color_strategy": "director_plan.color_strategy",
    "director_plan.color_direction": "director_plan.color_strategy",
    "style_boundary": "director_plan.style_boundary",
    "character_expression": "director_plan.character_expression",
    "character_pose": "director_plan.character_pose",
    "character_presence": "director_plan.character_presence",
    "director_plan.subject_environment_relationship": (
        "director_plan.subject_environment_relation"
    ),
    "camera": "cinematography.camera_language",
    "camera_language": "cinematography.camera_language",
    "camera_angle": "cinematography.camera_angle",
    "shot_size": "cinematography.shot_size",
    "lighting": "cinematography.lighting",
    "depth_strategy": "cinematography.depth_strategy",
}
ReviewModel = Callable[[list[dict[str, str]]], str]
EventSink = Callable[[str, dict[str, Any]], None]
QUALITY_WORDS = re.compile(r"\b(cinematic|masterpiece|beautiful|epic)\b", re.IGNORECASE)


def director_hash(spec: DirectorSpecDraft) -> str:
    payload = spec.model_dump(include=set(DirectorSpecDraft.model_fields) - {"critic_result"})
    # 新增的可选表现字段为空时不改变历史审核指纹。
    plan = payload.get("director_plan")
    if isinstance(plan, dict):
        for key in (
            "style_boundary", "character_expression", "character_pose", "character_presence",
        ):
            if plan.get(key) is None:
                plan.pop(key, None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def require_approved_director(spec: DirectorSpecDraft) -> None:
    """旧 v1 保持兼容；v2 必须有绑定当前方案的实际审核凭据。"""
    if spec.schema_version == 1:
        return
    review = spec.critic_result
    if (
        review is None or review.verdict != "pass"
        or review.review_version != REVIEW_VERSION
        or review.reviewed_spec_hash != director_hash(spec)
    ):
        raise ToolError("DirectorSpec v2 尚未通过当前方案的导演审核")


class PublicFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=300)
    severity: Literal["info", "warning", "error"]
    field_path: str = Field(min_length=1, max_length=200)
    evidence: str = Field(min_length=1, max_length=4000)
    expected: str = Field(min_length=1, max_length=4000)
    suggested_action: str = Field(min_length=1, max_length=2000)


class SemanticReview(BaseModel):
    """只接受公开结论和局部建议，禁止透传 reasoning/CoT 或整个新方案。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    public_summary: str = Field(min_length=1, max_length=4000)
    findings: list[PublicFinding] = Field(max_length=45)
    suggested_patches: list[DirectorCriticPatch] = Field(max_length=20)
    confidence: float = Field(ge=0, le=1)


class DirectorReviewOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    director_spec: DirectorSpecDraft
    critic_result: DirectorCriticResult
    revision_count: int = Field(ge=0, le=1)
    needs_review: bool


def _shared_review(messages: list[dict[str, str]]) -> str:
    from kantoku.core.llm import chat

    response = chat(messages, response_format={"type": "json_object"})
    if not response.content:
        raise ToolError("导演审核模型没有返回公开结论")
    return response.content


def _value(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise ToolError("导演审核字段路径无效", detail=path)
        value = value[key]
    return value


def _normalize_field_path(path: str) -> str:
    """把模型字段名收口到 DirectorSpec v2 的公开叶子字段。"""
    normalized = path.strip()
    if normalized.startswith("director_spec."):
        normalized = normalized.removeprefix("director_spec.")
    return FIELD_PATH_ALIASES.get(normalized, normalized)


def _critic_warning(received_field: str, *, action: str = "ignored") -> None:
    logger.warning(
        "critic_warning received_field={} action={} trace_id={}",
        redact_secrets(received_field),
        action,
        current_trace_id() or "-",
    )


def _normalize_finding(item: PublicFinding) -> PublicFinding | None:
    normalized = _normalize_field_path(item.field_path)
    if normalized not in CRITIC_ALLOWED_FIELD_PATHS:
        _critic_warning(item.field_path)
        return None
    return item.model_copy(update={"field_path": normalized})


def _normalize_patch(
    spec: DirectorSpecDraft, patch: DirectorCriticPatch,
) -> DirectorCriticPatch:
    raw_path = patch.field.strip()
    if raw_path.startswith("director_spec."):
        raw_path = raw_path.removeprefix("director_spec.")
    normalized_path = _normalize_field_path(raw_path)
    expected = patch.expected_value
    if normalized_path in PATCH_FIELDS and normalized_path != raw_path and expected is not None:
        data = spec.model_dump(include=set(DirectorSpecDraft.model_fields))
        target_value = _value(data, normalized_path)
        # 兼容模型基于旧扁平投影返回 expected_value，同时仍绑定当前真实目标值。
        try:
            source_value = _value(data, raw_path)
        except ToolError:
            source_value = None
        if source_value is not None and expected == source_value:
            expected = target_value
    return patch.model_copy(update={"field": normalized_path, "expected_value": expected})


def _has_evidence(value: Any, evidence: str) -> bool:
    if isinstance(value, str):
        return evidence in value
    if isinstance(value, dict):
        return any(_has_evidence(item, evidence) for item in value.values())
    if isinstance(value, list):
        return any(_has_evidence(item, evidence) for item in value)
    return False


class DirectorCriticEngine:
    """复用共享文本调用；不解释图片、不绑定模型、不保存私有推理。"""

    def __init__(self, model_call: ReviewModel | None = None) -> None:
        self.model_call = model_call or _shared_review

    def review(
        self, spec: DirectorSpecDraft, brief: CreativeBriefInput, *,
        assets: list[ComicAsset] | None = None,
        storyboard: ComicStoryboard | None = None, shot: ComicShot | None = None,
    ) -> DirectorCriticResult:
        try:
            return self._review(spec, brief, assets or [], storyboard, shot)
        except Exception as error:
            # 在 Run 内由 Coordinator 生成唯一 error_id；独立调用复用同一错误日志入口。
            if current_run_id() is None:
                public_error(
                    error, trace_id=current_trace_id(), component="comic.director_critic",
                    project_id=getattr(spec, "project_id", None)
                    or getattr(brief, "project_id", None), skill_id="comic.director_critic",
                )
            raise

    def _review(
        self, spec: DirectorSpecDraft, brief: CreativeBriefInput,
        assets: list[ComicAsset], storyboard: ComicStoryboard | None, shot: ComicShot | None,
    ) -> DirectorCriticResult:
        if spec.schema_version != 2:
            raise ToolError("Director Critic 需要 DirectorSpec v2")
        if len(assets) > 8:
            raise ToolError("导演审核资产上下文过大")
        data = spec.model_dump(include=set(DirectorSpecDraft.model_fields) - {"critic_result"})
        findings: list[DirectorCriticFinding] = []

        def finding(code: str, severity: str, path: str, evidence: str, expected: str) -> None:
            findings.append(DirectorCriticFinding.model_validate({
                "code": code, "severity": severity, "field_path": path,
                "evidence": evidence[:4000], "expected": expected[:4000],
                "suggested_action": "依据当前 Brief 和固定资产人工修订该项后重新审核",
            }))

        decision = spec.creative_decision
        assert decision is not None and spec.director_plan is not None
        for path, values in (
            ("constraints", spec.constraints),
            ("creative_decision.hard_constraints", decision.hard_constraints),
        ):
            missing = [item for item in brief.hard_constraints if item not in values]
            if missing:
                finding("HARD_CONSTRAINT_MISSING", "error", path,
                        json.dumps(values, ensure_ascii=False),
                        json.dumps(missing, ensure_ascii=False))
        if set(decision.hard_constraints) - set(brief.hard_constraints):
            finding("HARD_CONSTRAINT_CONFLICT", "error", "creative_decision.hard_constraints",
                    str(decision.hard_constraints), "不得自行新增或改写用户硬约束")
        expected_assets = {f"asset:{item.asset_id}": item.version for item in assets}
        if spec.asset_versions != expected_assets:
            finding("ASSET_VERSION_MISMATCH", "error", "asset_versions",
                    str(spec.asset_versions), str(expected_assets))
        if len(expected_assets) != len(assets) or any(item.state != "active" for item in assets):
            finding("ASSET_CONTEXT_INVALID", "error", "asset_versions",
                    str(expected_assets), "只引用有效且不重复的资产")
        project_id = getattr(spec, "project_id", None) or getattr(brief, "project_id", None)
        if (
            getattr(spec, "creative_brief_version", None) is not None
            and spec.creative_brief_version != getattr(brief, "version", None)
        ):
            finding("CONTEXT_VERSION_MISMATCH", "error", "creative_decision",
                    str(spec.creative_brief_version), "使用关联的 CreativeBrief 版本")
        context_ids = {item.project_id for item in assets}
        if storyboard is not None:
            context_ids.add(storyboard.project_id)
        if shot is not None:
            context_ids.add(shot.project_id)
        if len(context_ids) > 1 or (project_id and context_ids - {project_id}):
            finding("CONTEXT_SCOPE_CONFLICT", "error", "asset_versions",
                    str(sorted(context_ids)), "全部上下文属于当前作品")
        for name, entity in (("storyboard", storyboard), ("shot", shot)):
            if entity is not None and getattr(spec, f"{name}_version") != entity.version:
                finding("CONTEXT_VERSION_MISMATCH", "error", f"{name}_version",
                        str(getattr(spec, f"{name}_version")), str(entity.version))
        if shot is not None and (
            storyboard is None or shot.storyboard_id != storyboard.storyboard_id
        ):
            finding("SHOT_SCOPE_CONFLICT", "error", "shot_version",
                    shot.storyboard_id, "镜头必须属于当前分镜")
        if shot is not None:
            refs = [*shot.character_asset_versions, *shot.scene_asset_versions]
            if shot.style_version is not None:
                refs.append(shot.style_version)
            for ref in refs:
                if expected_assets.get(f"asset:{ref.asset_id}") != ref.version:
                    finding("SHOT_ASSET_MISMATCH", "error", "asset_versions",
                            f"{ref.asset_id}:{ref.version}", "资产版本与当前 Shot 引用一致")
        if not spec.director_plan.creative_choices:
            finding("DIRECTOR_REASON_MISSING", "error", "director_plan.creative_choices",
                    "[]", "解释构图、色彩、光影和人物关系如何服务叙事")
        for path in (
            "director_plan.visual_strategy", "director_plan.composition_strategy",
            "director_plan.color_strategy", "cinematography.lighting",
        ):
            text = _value(data, path)
            if len(QUALITY_WORDS.findall(text)) >= 2:
                concrete = re.sub(r"[\W_]", "", QUALITY_WORDS.sub("", text))
                if len(concrete) < 8:
                    finding("EMPTY_QUALITY_WORDS", "warning", path, text,
                            "用具体视觉关系及叙事理由替代质量形容词")
        # 硬约束和来源不一致时不花费模型调用，也不允许自动修改身份/约束。
        fatal_codes = {
            "HARD_CONSTRAINT_MISSING", "HARD_CONSTRAINT_CONFLICT",
            "ASSET_VERSION_MISMATCH", "ASSET_CONTEXT_INVALID",
            "CONTEXT_SCOPE_CONFLICT", "CONTEXT_VERSION_MISMATCH", "SHOT_SCOPE_CONFLICT",
            "SHOT_ASSET_MISMATCH",
        }
        if any(item.code in fatal_codes for item in findings):
            return self._result(spec, findings, [], "约束或来源版本冲突，需要人工处理。", 1.0)

        context = {
            "director_spec": data, "creative_brief": brief.model_dump(
                mode="json", include=set(CreativeBriefInput.model_fields),
            ),
            "relevant_assets": [item.model_dump(mode="json", include={
                "asset_id", "version", "details", "fixed_constraints",
            }) for item in assets],
            "storyboard": storyboard.model_dump(mode="json", include={
                "title", "description", "version",
            }) if storyboard else None,
            "shot": shot.model_dump(mode="json", include={
                "purpose", "subject", "action", "environment", "emotion", "version",
            }) if shot else None,
            "allowed_field_paths": sorted(CRITIC_ALLOWED_FIELD_PATHS),
            "patch_fields": sorted(PATCH_FIELDS),
        }
        raw = self.model_call([
            {"role": "system", "content": (
                "审核当前导演方案的视觉因果、用户约束语义、角色/场景/风格一致性、"
                "构图/色彩/光影/人物关系的公开创作理由以及空泛模板词。"
                "结合具体故事判断；孤独也可在战斗中表达，禁止情绪到固定镜头的映射。"
                "不要创造新方案或改写用户约束。只返回 JSON：public_summary、confidence、"
                "findings、suggested_patches。不要输出思维链、reasoning 或 CoT。"
                "findings 每项必须含 code、severity(info/warning/error)、field_path、"
                "evidence(输入中的逐字公开片段)、expected、suggested_action。"
                "field_path 只能来自 allowed_field_paths，不要引用 creative_brief、asset、"
                "project 或 StyleBible 身份字段。"
                "用户硬约束语义冲突使用 HARD_CONSTRAINT_CONFLICT，固定资产语义冲突使用"
                " ASSET_CONSTRAINT_CONFLICT，视觉因果不匹配使用 VISUAL_CAUSALITY_MISMATCH。"
                "error 表示重要创作冲突需人工；warning 为可局部修订的小问题。"
                "Patch 只针对 patch_fields，每项含 field、reason、value、expected_value，"
                "value 只能是替换该字段的文字。没有问题返回空 findings 和空 patches。"
            )},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ])
        try:
            semantic = SemanticReview.model_validate_json(raw)
        except (ValueError, ValidationError):
            # ValidationError 会包含供应商原始值；不能把可能的 CoT 带入 traceback。
            raise ToolError("导演审核模型未返回有效的公开审核结构") from None
        evidence_context = {
            key: value for key, value in context.items()
            if key not in {"patch_fields", "allowed_field_paths"}
        }
        semantic_findings = [
            normalized for item in semantic.findings
            if (normalized := _normalize_finding(item)) is not None
        ]
        for item in semantic_findings:
            _value(data, item.field_path)
            if (
                item.code == "LEGACY_FINDING" or not item.evidence or not item.expected
                or not item.suggested_action or not _has_evidence(evidence_context, item.evidence)
            ):
                raise ToolError("导演审核缺少可核验的公开证据")
        findings.extend(DirectorCriticFinding.model_validate(item.model_dump())
                        for item in semantic_findings)
        return self._result(
            spec, findings, semantic.suggested_patches,
            semantic.public_summary, semantic.confidence,
        )

    @staticmethod
    def _result(
        spec: DirectorSpecDraft, findings: list[DirectorCriticFinding],
        patches: list[DirectorCriticPatch], summary: str, confidence: float,
    ) -> DirectorCriticResult:
        errors = any(item.severity == "error" for item in findings)
        blocked = any(item.code in {
            "HARD_CONSTRAINT_MISSING", "HARD_CONSTRAINT_CONFLICT", "ASSET_VERSION_MISMATCH",
            "ASSET_CONTEXT_INVALID", "CONTEXT_SCOPE_CONFLICT", "CONTEXT_VERSION_MISMATCH",
            "SHOT_SCOPE_CONFLICT", "SHOT_ASSET_MISMATCH", "ASSET_CONSTRAINT_CONFLICT",
        } for item in findings)
        verdict = "blocked" if blocked else (
            "needs_revision" if errors or any(item.severity == "warning" for item in findings)
            else "pass"
        )
        normalized_patches: list[DirectorCriticPatch] = []
        for patch in patches:
            normalized = _normalize_patch(spec, patch)
            if normalized.field not in PATCH_FIELDS:
                _critic_warning(patch.field)
                continue
            normalized_patches.append(normalized)
        allowed = sorted({
            item.field_path for item in findings
            if item.severity == "warning" and item.field_path in PATCH_FIELDS
        }) if verdict == "needs_revision" and not errors else []
        if normalized_patches and verdict == "pass":
            for patch in normalized_patches:
                _critic_warning(patch.field)
            normalized_patches = []
        if not allowed:
            normalized_patches = []
        data = spec.model_dump(include=set(DirectorSpecDraft.model_fields))
        usable_patches: list[DirectorCriticPatch] = []
        seen: set[str] = set()
        for patch in normalized_patches:
            actual = _value(data, patch.field)
            if (
                patch.field not in allowed or patch.value is None
                or patch.expected_value != actual or patch.field in seen
            ):
                _critic_warning(patch.field)
                continue
            seen.add(patch.field)
            usable_patches.append(patch)
        return DirectorCriticResult(
            verdict=verdict, public_summary=summary, findings=findings,
            suggested_patches=usable_patches, confidence=confidence,
            allowed_patches=allowed,
            review_version=REVIEW_VERSION, reviewed_spec_hash=director_hash(spec),
        )

    @staticmethod
    def apply_patches(
        spec: DirectorSpecDraft, result: DirectorCriticResult,
    ) -> DirectorSpecDraft:
        if (
            result.verdict != "needs_revision" or result.review_version != REVIEW_VERSION
            or result.reviewed_spec_hash != director_hash(spec)
            or any(item.severity == "error" for item in result.findings)
        ):
            raise ToolError("Patch 与当前待修订方案不一致")
        data = spec.model_dump(include=set(DirectorSpecDraft.model_fields))
        seen: set[str] = set()
        for patch in result.suggested_patches:
            if patch.field not in PATCH_FIELDS or patch.field not in result.allowed_patches:
                raise ToolError("导演 Patch 禁止修改用户意图、约束或来源版本")
            if patch.field in seen or patch.value is None:
                raise ToolError("导演 Patch 重复或缺少替换值")
            seen.add(patch.field)
            if _value(data, patch.field) != patch.expected_value:
                raise ToolError("导演 Patch 的预期值已经变化")
            parent, key = patch.field.split(".")
            data[parent][key] = patch.value
        # 兼容字段从分层方案投影；Patch 不能另改一份互相冲突的平面参数。
        data.update({
            "emotion": data["creative_decision"]["emotional_target"],
            "visual_direction": data["director_plan"]["visual_focus"],
            "composition": data["director_plan"]["composition_strategy"],
            "color_language": data["director_plan"]["color_strategy"],
            "lighting": data["cinematography"]["lighting"],
            "character_focus": (
                data["director_plan"].get("character_presence")
                or data["director_plan"]["visual_focus"]
            ),
            "camera_language": data["cinematography"].get("camera_language") or "；".join(
                data["cinematography"][key] for key in (
                    "shot_size", "camera_angle", "spatial_feel",
                )
            ),
            "critic_result": None,
        })
        return DirectorSpecDraft.model_validate(data)

    def review_and_revise(
        self, spec: DirectorSpecDraft, brief: CreativeBriefInput, *,
        assets: list[ComicAsset] | None = None, storyboard: ComicStoryboard | None = None,
        shot: ComicShot | None = None, emit: EventSink | None = None,
    ) -> DirectorReviewOutcome:
        current = spec
        revision_count = 0
        for attempt in range(2):
            if emit:
                emit("director_critic_started", {"review_attempt": attempt})
            result = self.review(current, brief, assets=assets, storyboard=storyboard, shot=shot)
            if emit:
                emit("director_critic_completed", {
                    "review_attempt": attempt, "critic_result": result.model_dump(),
                })
            if result.verdict == "pass":
                break
            if emit:
                emit("director_revision_requested", {"verdict": result.verdict})
            if attempt == 0 and result.allowed_patches and result.suggested_patches:
                current = self.apply_patches(current, result)
                revision_count = 1
                if emit:
                    emit("director_patch_applied", {
                        "revision_count": revision_count,
                        "patches": [item.model_dump() for item in result.suggested_patches],
                    })
                continue
            break
        needs_review = result.verdict != "pass"
        if needs_review and emit:
            emit("director_review_blocked", {"verdict": result.verdict, "needs_review": True})
        current = DirectorSpecDraft.model_validate({
            **current.model_dump(), "critic_result": result.model_dump(),
        })
        return DirectorReviewOutcome(
            director_spec=current, critic_result=result,
            revision_count=revision_count, needs_review=needs_review,
        )
