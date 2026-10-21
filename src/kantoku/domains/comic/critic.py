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

from .cinematography import (
    EXECUTION_FIELDS,
    PLAN_EXECUTION_FIELDS,
    require_execution_choices,
    unresolved_execution,
)
from .director import _tracked_model
from .director_provenance import provenance_context, record_changes
from .models import (
    CinematographyPlan,
    ComicAsset,
    ComicShot,
    ComicStoryboard,
    CreativeBriefInput,
    CreativeDecision,
    DirectorCriticFinding,
    DirectorCriticPatch,
    DirectorCriticResult,
    DirectorEvidence,
    DirectorPlan,
    DirectorSpecDraft,
)


def public_critic_feedback(review: Any) -> list[dict[str, str]]:
    """公开反馈是原审核的解释投影，不改变 DirectorSpec 或审核证据。"""
    if review is None:
        return []
    data = review.model_dump() if hasattr(review, "model_dump") else review
    labels = {
        "shot_size": "景别", "camera_angle": "机位", "camera_movement": "摄影机运动",
        "light_source": "主光来源", "light_direction": "主光方向", "lighting": "光线设计",
        "color_relationship": "色彩方案", "color_strategy": "色彩设计",
        "composition_strategy": "构图", "composition": "构图", "character_focus": "人物",
        "character_concept": "人物设定", "world_concept": "世界设定",
        "environment": "场景", "scene_relationship": "场景关系",
        "constraints": "约束", "hard_constraints": "用户硬约束",
        "asset_versions": "资产来源", "cinematography": "摄影方案",
        "creative_choices": "导演选择依据", "creative_decision": "故事理解",
        "character_presence": "人物设定", "character_pose": "人物姿态与视角",
        "style_boundary": "风格规范", "subject_environment_relation": "人物与环境关系",
        "narrative_context": "人物与世界设定",
    }
    feedback = []
    for finding in data.get("findings", []):
        code = finding.get("code", "")
        path = finding.get("field_path") or "director_review"
        label = labels.get(path.split(".")[-1], "当前导演方案")
        level = "blocking" if finding.get("severity") == "error" or code in {
            "CINEMATOGRAPHY_INCOMPLETE", "REVIEW_EXECUTION_FAILED",
        } else "warning"
        issue_type = "logical_conflict" if level == "blocking" else "photography_advice"
        message = f"{label}需要调整" if level == "blocking" else f"{label}优化建议"
        evidence = finding.get("evidence") or f"{label}尚未提供明确依据。"
        expected = finding.get("expected")
        target = "审核要求" if level == "blocking" else "优化目标"
        reason = f"当前内容：{evidence}；{target}：{expected}。" if expected else evidence
        suggestion = finding.get("suggested_action") or f"请补充{label}并重新审核。"
        if code == "EXECUTION_DECISION_UNRESOLVED":
            issue_type = "required_parameter"
            message = f"{label}尚未确定唯一执行方案"
            reason = f"当前{label}为“{evidence}”，仍包含候选项，后续镜头无法确定执行参数。"
            suggestion = f"请为{label}选择并填写一个最终值，再重新审核；未提供时可让导演选择。"
        elif code == "CINEMATOGRAPHY_INCOMPLETE":
            issue_type = "required_parameter"
            message = "摄影方案缺少必要参数"
            reason = "当前摄影方案尚不完整，无法确定后续镜头如何拍摄。"
            suggestion = "请补充景别、机位、摄影机运动、主光与色彩方案，再重新审核。"
        elif code in {"HARD_CONSTRAINT_MISSING", "DIRECTOR_REASON_MISSING"}:
            issue_type = "required_parameter"
            reason = f"{label}缺少必要内容：{finding.get('expected') or '尚未填写'}。"
            suggestion = f"请在{label}中补齐这些内容后重新审核。"
        elif level == "blocking":
            leaf = path.split(".")[-1]
            has_evidence = bool(str(finding.get("evidence", "")).strip())
            character_fields = {
                "character_concept", "character_focus", "character_presence", "subject",
            }
            scene_fields = {
                "world_concept", "environment", "scene_relationship",
                "subject_environment_relation",
            }
            missing_character = code in {
                "CHARACTER_MISSING", "CHARACTER_CONTEXT_MISSING",
            } or (code == "CONTEXT_MISSING" and leaf in character_fields) \
                or (not has_evidence and leaf in character_fields)
            missing_scene = code in {
                "WORLD_MISSING", "SCENE_MISSING", "WORLD_CONTEXT_MISSING",
            } or (code == "CONTEXT_MISSING" and leaf in scene_fields) \
                or (not has_evidence and leaf in scene_fields)
            if missing_character:
                issue_type, message = "missing_character", "缺少人物设定"
                suggestion = "请说明当前角色身份和固定外貌，或选择已有角色资产，再重新审核。"
            elif missing_scene:
                issue_type, message = "missing_scene", "缺少场景设定"
                suggestion = "请补充地点、时间与环境，或选择已有场景资产，再重新审核。"
        elif level == "warning":
            issue_type = "composition_advice" if "composition" in path else \
                "color_advice" if "color" in path else \
                "atmosphere_advice" if any(word in path for word in ("emotion", "atmosphere")) \
                else "photography_advice"
        impacts = {
            "missing_character": "外部图片模型缺少固定人物依据，无法保持角色身份、外貌与服装一致。",
            "missing_scene": "外部图片模型无法确定地点、时间与环境，镜头之间可能出现世界设定漂移。",
            "logical_conflict": "外部图片模型会收到相互矛盾的视觉要求，人物与环境可能不一致。",
            "required_parameter": f"外部图片模型无法确定{label}，可能随机选择或混合多个方案。",
        }
        feedback.append({"level": level, "issue_type": issue_type, "message": message,
                         "reason": str(reason), "suggestion": str(suggestion),
                         "affected_field": path, "affected_field_label": label,
                         "impact": impacts[issue_type] + "需修改后确认。" if level == "blocking"
                         else f"可改善{label}，不阻止最终确认。"})
    return feedback


REVIEW_VERSION = "director-critic-1"
# 读范围来自现有公开 Schema；写范围独立且显式，不能把“可审核”误当“可修改”。
CRITIC_ALLOWED_FIELD_PATHS = frozenset(
    f"{section}.{name}"
    for section, model in (
        ("creative_decision", CreativeDecision),
        ("director_plan", DirectorPlan),
        ("cinematography", CinematographyPlan),
    )
    for name in model.model_fields
    if name not in {"hard_constraints", "soft_preferences", "creative_freedom", "status",
                    "public_decision", "unresolved_decisions"}
)
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
    "cinematography.light_direction",
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
    "light_direction": "cinematography.light_direction",
    "cinematography.light_source_direction": "cinematography.light_direction",
    "director_plan.color_language": "director_plan.color_strategy",
    "creative_decision.emotion": "creative_decision.emotional_target",
    "cinematography.camera": "cinematography.camera_language",
}
ReviewModel = Callable[[list[dict[str, str]]], str]
EventSink = Callable[[str, dict[str, Any]], None]
QUALITY_WORDS = re.compile(r"\b(cinematic|masterpiece|beautiful|epic)\b", re.IGNORECASE)


def director_hash(spec: DirectorSpecDraft) -> str:
    payload = spec.model_dump(include=set(DirectorSpecDraft.model_fields) - {"critic_result"})
    if payload.get("critic_status") is None:
        payload.pop("critic_status", None)
    if not payload.get("field_provenance"):
        payload.pop("field_provenance", None)
    if payload.get("execution_policy") is None:
        payload.pop("execution_policy", None)
    if not payload.get("reference_context"):
        payload.pop("reference_context", None)
    # 新增的可选表现字段为空时不改变历史审核指纹。
    plan = payload.get("director_plan")
    if isinstance(plan, dict):
        for key in (
            "style_boundary", "character_expression", "character_pose", "character_presence",
            "characters_present",
        ):
            if plan.get(key) is None:
                plan.pop(key, None)
    camera = payload.get("cinematography")
    if isinstance(camera, dict):
        # 已有完整 v2 的审核指纹保持兼容；不完整状态始终计入指纹。
        if camera.get("status") == "complete":
            camera.pop("status", None)
        for key in ("public_decision", "creative_reason"):
            if camera.get(key) is None:
                camera.pop(key, None)
        if not camera.get("unresolved_decisions"):
            camera.pop("unresolved_decisions", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def require_approved_director(
    spec: DirectorSpecDraft, *, allow_advisory: bool = False, allow_unavailable: bool = False,
) -> None:
    """旧 v1 保持兼容；v2 必须有绑定当前方案的实际审核凭据。"""
    if spec.schema_version == 1:
        return
    if spec.execution_policy == "single_image" and _execution_issues(spec):
        raise ToolError("最终摄影决策待修订，不能进入下一步")
    if spec.cinematography is None or spec.cinematography.status != "complete":
        raise ToolError("摄影方案待修订，不能进入下一步")
    review = spec.critic_result
    if allow_unavailable and spec.critic_status == "unavailable" and review is None:
        return
    if (
        review is None
        or review.review_version != REVIEW_VERSION
        or review.reviewed_spec_hash != director_hash(spec)
    ):
        raise ToolError("DirectorSpec v2 尚未通过当前方案的导演审核")
    if review.verdict == "pass":
        return
    # Production policy may accept advice, never manufacture a Critic pass.
    # Missing/failed review, immutable-input conflicts and incomplete plans still stop.
    if (allow_advisory and review.verdict == "needs_revision"
            and not any(item.severity == "error" or item.code in {
                "REVIEW_EXECUTION_FAILED", "CINEMATOGRAPHY_INCOMPLETE",
            } for item in review.findings)):
        return
    raise ToolError("DirectorSpec v2 尚未通过当前方案的导演审核：存在阻断问题或需要人工审核")


def _execution_issues(spec: DirectorSpecDraft) -> list[str]:
    return ([f"cinematography.{name}" for name in unresolved_execution(
        spec.cinematography.model_dump(), EXECUTION_FIELDS,
    ) if getattr(spec.cinematography, name) is not None]
        + [f"director_plan.{name}" for name in unresolved_execution(
        spec.director_plan.model_dump(), PLAN_EXECUTION_FIELDS,
    )])


def critic_spec_context(spec: DirectorSpecDraft) -> dict[str, Any]:
    """只发送一份分层决策；兼容别名仍由服务器解释，不重复送入模型。"""
    data = spec.model_dump(mode="json", include={
        "schema_version", "creative_decision", "director_plan", "cinematography",
        "asset_versions", "storyboard_version", "shot_version", "knowledge_refs",
        "execution_policy",
    })
    data["cinematography"].pop("public_decision", None)
    data.update(provenance_context(spec))
    return data


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
    critic_result: DirectorCriticResult | None
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
        "critic_warning received_field={} action={} trace_id={} run_id={}",
        redact_secrets(received_field),
        action,
        current_trace_id() or "-",
        current_run_id() or "-",
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
        # 排版空白不是事实差异；不做语义猜测或模糊匹配。
        fragment = re.sub(r"\s+", "", evidence)
        return bool(fragment) and fragment in re.sub(r"\s+", "", value)
    if isinstance(value, dict):
        return any(_has_evidence(item, evidence) for item in value.values())
    if isinstance(value, list):
        return any(_has_evidence(item, evidence) for item in value)
    return False




def _invalid_patch(field: str, reason: str) -> DirectorCriticFinding:
    # 仅记录字段标识与固定诊断，不透传无效 Patch 的值、reasoning 或原始响应。
    path = field if re.fullmatch(r"[a-z_][a-z0-9_.]{0,199}", field) else ""
    _critic_warning(path or "<invalid>", action="invalid_patch")
    return DirectorCriticFinding(
        code="INVALID_PATCH",
        severity="info",
        field_path=path,
        expected="Patch 必须符合 DirectorSpec Schema、写白名单和当前字段预期值",
        suggested_action=reason,
    )


class DirectorCriticEngine:
    """复用共享文本调用；不解释图片、不绑定模型、不保存私有推理。"""

    def __init__(self, model_call: ReviewModel | None = None) -> None:
        self.model_call = model_call or _shared_review

    def review(
        self,
        spec: DirectorSpecDraft,
        brief: CreativeBriefInput,
        *,
        assets: list[ComicAsset] | None = None,
        storyboard: ComicStoryboard | None = None,
        shot: ComicShot | None = None,
        current_shot_instruction: str | None = None,
        context_diff: dict[str, Any] | None = None,
    ) -> DirectorCriticResult:
        try:
            return self._review(spec, brief, assets or [], storyboard, shot,
                                current_shot_instruction, context_diff)
        except Exception as error:
            # Run 内由审核闭环或 Coordinator 生成唯一 error_id；独立调用复用日志入口。
            if current_run_id() is None:
                error._kantoku_public_failure = public_error(
                    error,
                    trace_id=current_trace_id(),
                    component="comic.director_critic",
                    project_id=getattr(spec, "project_id", None)
                    or getattr(brief, "project_id", None),
                    skill_id="comic.director_critic",
                )
            raise

    def _review(
        self,
        spec: DirectorSpecDraft,
        brief: CreativeBriefInput,
        assets: list[ComicAsset],
        storyboard: ComicStoryboard | None,
        shot: ComicShot | None,
        current_shot_instruction: str | None = None,
        context_diff: dict[str, Any] | None = None,
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
        camera = spec.cinematography
        if spec.execution_policy == "single_image":
            for path in _execution_issues(spec):
                finding("EXECUTION_DECISION_UNRESOLVED", "error", path, _value(data, path),
                        "仅确定一个最终参数，未确定的创作选择保留待修订")
            if any(item.code == "EXECUTION_DECISION_UNRESOLVED" for item in findings):
                return self._result(spec, findings, [], "最终执行参数尚未确定，草稿已保留。", 1.0)
        if camera is None or camera.status != "complete":
            finding("CINEMATOGRAPHY_INCOMPLETE", "warning", "cinematography",
                    camera.status if camera else "missing",
                    "补充当前摄影方案缺失字段：" + ",".join(
                        camera.missing_fields if camera else []))
            # 仍执行了上面的约束与资产审核；缺失摄影不是整个导演链路失败。
            # 不让语义模型或 Patch 替用户填入一套默认摄影方案。
            return self._result(spec, findings, [], "导演草案已保留，摄影方案需要修订。", 1.0)
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
            "director_spec": critic_spec_context(spec), "creative_brief": brief.model_dump(
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
        if current_shot_instruction:
            context["current_shot_instruction"] = current_shot_instruction
        if context_diff is not None:
            context["context_diff"] = context_diff
            # 用户本轮主动改动的字段是“新要求”，不是与旧值自相矛盾。
            # 显式列出，审核器才不会把被替换的旧描述当成不可动的硬约束。
            changed = [
                item.get("field_path") for item in context_diff.get("changes", [])
                if isinstance(item, dict) and item.get("field_path")
            ]
            # 同语义簇由本轮状态指令牵动，同样属于用户请求的变更范围。
            changed.extend(context_diff.get("action_semantic_cluster", []) or [])
            if changed:
                context["user_requested_changes"] = changed
        messages = [
            {"role": "system", "content": (
                "审核当前导演方案的视觉因果、用户约束语义、角色/场景/风格一致性、"
                "构图/色彩/光影/人物关系的公开创作理由以及空泛模板词。"
                "结合具体故事判断；孤独也可在战斗中表达，禁止情绪到固定镜头的映射。"
                "不要创造新方案或改写用户约束。"
                "若有 current_shot_instruction，这是同一创作的下一镜头要求："
                "Brief 继续约束角色、世界和风格，本次动作与构图以当前镜头要求为准，"
                "不要因动作不同于上一镜头而报告冲突；显式硬约束和固定资产仍不可改写。"
                "主体或其他设定是否替换，依据本轮实际来源及有效约束判断。"
                "不能因为创作动词或新主体出现，就丢弃仍有效的用户硬约束或固定资产事实。"
                "user_requested_changes 列出的是用户本轮明确要求改动的字段。"
                "这些字段的旧值已经被用户替换，方案的当前值就是用户要的最终结果。"
                "禁止把它们与被替换的旧值比对后判为矛盾或冲突，"
                "也禁止建议用户撤回、删除或忽略本轮镜头指令。"
                "只有当当前值与未被本次改动触及的用户硬约束或固定资产矛盾时才算冲突。"
                "field_provenance 的引用指向 provenance_sources，知识为方法依据，"
                "model_choice 不能作为用户或资产事实；检查各层最终决定是否一致。"
                "只返回 JSON：public_summary、confidence、"
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
                "输出须严格符合下述 Schema，字段名和类型不得自行改写："
                + json.dumps(SemanticReview.model_json_schema(), ensure_ascii=False)
            )},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        tracked, requests = _tracked_model(self.model_call, {
            "component": "comic.director_critic", "trace_id": current_trace_id(),
            "run_id": current_run_id(),
        })
        raw = tracked(messages)
        semantic, raw_patches = self._parse_review(raw, messages, model_call=tracked)
        invalid_patches: list[DirectorCriticFinding] = []
        for patch_data in raw_patches:
            try:
                semantic.suggested_patches.append(DirectorCriticPatch.model_validate(patch_data))
            except ValidationError:
                field = patch_data.get("field", "") if isinstance(patch_data, dict) else ""
                invalid_patches.append(
                    _invalid_patch(
                        field if isinstance(field, str) else "",
                        "忽略不符合 Patch Schema 的建议",
                    )
                )
        evidence_context = {
            key: value for key, value in context.items()
            if key not in {"patch_fields", "allowed_field_paths"}
        }
        semantic_findings = [
            normalized for item in semantic.findings
            if (normalized := _normalize_finding(item)) is not None
        ]
        verified: list[PublicFinding] = []
        unverified: list[DirectorCriticFinding] = []
        for item in semantic_findings:
            _value(data, item.field_path)
            if (
                item.code == "LEGACY_FINDING" or not item.evidence or not item.expected
                or not item.suggested_action or not _has_evidence(evidence_context, item.evidence)
            ):
                _critic_warning(item.field_path, action="needs_review_unverified_evidence")
                unverified.append(DirectorCriticFinding(
                    code="UNVERIFIED_REVIEW_EVIDENCE", severity="warning",
                    field_path=item.field_path, evidence=item.evidence,
                    expected="审核必须引用当前输入中可核验的公开片段",
                    suggested_action="人工查看该项或重新执行审核，不应用未核验的建议",
                ))
                continue
            verified.append(item)
        findings.extend(DirectorCriticFinding.model_validate(item.model_dump())
                        for item in verified)
        findings.extend(unverified)
        findings.extend(invalid_patches)
        logger.info(
            "Director Critic Debug received_findings={} verified_findings={} "
            "ignored_findings={} unverified_findings={} spec_hash={}",
            len(semantic.findings), len(verified),
            len(semantic.findings) - len(semantic_findings), len(unverified),
            director_hash(spec),
        )
        return self._result(
            spec, findings, semantic.suggested_patches if not unverified else [],
            semantic.public_summary, semantic.confidence,
        ).model_copy(update={"model_request_id": requests[-1]})

    def _parse_review(
        self, raw: str, messages: list[dict[str, str]], *, repair: bool = True,
        model_call: ReviewModel | None = None,
    ) -> tuple[SemanticReview, list[Any]]:
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("invalid review object")
            raw_patches = payload.get("suggested_patches")
            if not isinstance(raw_patches, list) or len(raw_patches) > 20:
                raise ValueError("invalid patch collection")
            semantic = SemanticReview.model_validate({**payload, "suggested_patches": []})
        except (ValueError, ValidationError) as error:
            if isinstance(error, ValidationError):
                issues = [{"path": redact_secrets(".".join(map(str, item["loc"])))[:160],
                           "type": item["type"]}
                          for item in error.errors(include_input=False, include_context=False,
                                                   include_url=False)]
            elif isinstance(error, json.JSONDecodeError):
                issues = [{"path": "$", "type": "json_invalid",
                           "line": error.lineno, "column": error.colno}]
            else:
                issues = [{"path": "$", "type": "review_collection_invalid"}]
            logger.warning(
                "Director Critic Schema validation failed issues={} output_chars={} "
                "output_sha256={} trace_id={} run_id={}", issues, len(raw),
                hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                current_trace_id(), current_run_id(),
            )
            if repair:
                # Re-request only the public structure from the same bounded context.
                # Never echo an invalid response that could contain private reasoning.
                repaired = (model_call or self.model_call)([*messages, {"role": "user", "content": (
                    "上次公开审核结构无效。只修复一次结构，依据原输入返回完整公开审核 JSON；"
                    "不得改写导演方案或输出私有推理。Schema 问题："
                    + json.dumps(issues, ensure_ascii=False)
                )}])
                return self._parse_review(repaired, messages, repair=False, model_call=model_call)
            # ValidationError 会包含供应商原始值；不能把可能的 CoT 带入 traceback。
            raise ToolError("导演审核模型未返回有效的公开审核结构") from None
        return semantic, raw_patches

    @staticmethod
    def _result(
        spec: DirectorSpecDraft,
        findings: list[DirectorCriticFinding],
        patches: list[DirectorCriticPatch],
        summary: str,
        confidence: float,
    ) -> DirectorCriticResult:
        errors = any(item.severity == "error" for item in findings)
        # 只有 error 级才是硬阻断。warning 级是审核自己给出的“建议”判断，
        # 代码不得把它升级成阻断：否则方案在各节点显示已完成，
        # 却永远无法确认进入分镜（公开反馈与结论互相矛盾）。
        blocked = any(item.severity == "error" and item.code in {
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
                findings.append(_invalid_patch(patch.field, "忽略不存在或禁止写入的字段"))
                continue
            normalized_patches.append(normalized)
        allowed = sorted({
            item.field_path for item in findings
            if (item.severity == "warning" and item.field_path in PATCH_FIELDS
                and item.code != "UNVERIFIED_REVIEW_EVIDENCE")
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
                patch.field not in allowed
                or patch.value is None
                or patch.expected_value != actual
                or patch.field in seen
            ):
                findings.append(
                    _invalid_patch(
                        patch.field,
                        "忽略未关联问题、预期值不符或重复的建议",
                    )
                )
                continue
            parent, key = patch.field.split(".")
            trial = {**data, parent: {**data[parent], key: patch.value}}
            try:
                DirectorSpecDraft.model_validate(trial)
            except ValidationError:
                findings.append(
                    _invalid_patch(
                        patch.field,
                        "替换值未通过 DirectorSpec Schema 校验",
                    )
                )
                continue
            seen.add(patch.field)
            usable_patches.append(patch)
        if len(findings) > 50:
            # 阻断/修订项优先，非法 Patch 诊断不能挤掉关键证据。
            rank = {"error": 0, "warning": 1, "info": 2}
            ordered = sorted(findings, key=lambda item: rank[item.severity])
            incomplete = any(item.severity != "info" for item in ordered[49:])
            findings = [*ordered[:49], DirectorCriticFinding(
                code="REVIEW_FINDINGS_TRUNCATED",
                severity="warning" if incomplete else "info",
                expected="审核诊断最多保存 50 项",
                suggested_action="部分诊断已合并；查看 Trace，必要时显式重新审核",
            )]
            if incomplete and verdict == "pass":
                verdict = "needs_revision"
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
        revised = DirectorSpecDraft.model_validate(data)
        if spec.execution_policy == "single_image":
            revised = revised.model_copy(update={
                "cinematography": require_execution_choices(revised.cinematography),
            })
        # 模型建议经验证后才改值；禁止沿用被替换字段的用户/资产事实标记。
        if result.model_request_id:
            revised = record_changes(
                spec, revised, source="model_choice", evidence=DirectorEvidence(
                    source_type="model_choice", reference=result.model_request_id,
                ),
            )
        return revised

    def review_and_revise(
        self,
        spec: DirectorSpecDraft,
        brief: CreativeBriefInput,
        *,
        assets: list[ComicAsset] | None = None,
        storyboard: ComicStoryboard | None = None,
        shot: ComicShot | None = None,
        emit: EventSink | None = None,
        allow_patches: bool = True,
        current_shot_instruction: str | None = None,
        context_diff: dict[str, Any] | None = None,
    ) -> DirectorReviewOutcome:
        current = spec.model_copy(update={"critic_status": None, "critic_result": None})
        revision_count = 0
        result: DirectorCriticResult | None = None

        def failed_review(error: Exception, event: str) -> DirectorCriticResult | None:
            failure = getattr(error, "_kantoku_public_failure", None) or public_error(
                error,
                component="comic.director_critic",
                project_id=getattr(brief, "project_id", None),
                run_id=current_run_id(),
                skill_id="comic.director_critic",
            )
            error._kantoku_public_failure = failure
            if emit:
                emit(
                    event,
                    {**failure, "error_type": type(error).__name__, "review_attempt": attempt,
                     "exception_module": type(error).__module__},
                )
            if event == "director_critic_failed":
                return None
            retained = list(result.findings) if result is not None else []
            retained.append(
                DirectorCriticFinding(
                    code="REVIEW_EXECUTION_FAILED"
                    if event == "director_critic_failed"
                    else "INVALID_PATCH",
                    severity="warning",
                    suggested_action="保留导演草案；查看错误详情或显式重新审核，不自动继续制作",
                    expected=failure["safe_message"],
                )
            )
            return self._result(
                current, retained, [], "导演草案已生成，但审核未完成；原方案已保留。", 0.0
            )

        for attempt in range(2):
            if emit:
                emit("director_critic_started", {"review_attempt": attempt})
            try:
                result = self.review(
                    current,
                    brief,
                    assets=assets,
                    storyboard=storyboard,
                    shot=shot,
                    current_shot_instruction=current_shot_instruction,
                    context_diff=context_diff,
                )
            except Exception as error:
                # No valid review exists: retain the candidate, never fabricate Critic content.
                failed_review(error, "director_critic_failed")
                current = current.model_copy(update={
                    "critic_status": "unavailable", "critic_result": None,
                })
                if emit:
                    emit("critic_unavailable", {"critic_status": "unavailable",
                                              "critic_result": None, "warning": True})
                return DirectorReviewOutcome(
                    director_spec=current, critic_result=None,
                    revision_count=revision_count, needs_review=True,
                )
            if emit:
                emit("director_critic_completed", {
                    "review_attempt": attempt, "critic_result": result.model_dump(),
                })
            if result.verdict == "pass":
                break
            if emit:
                emit("director_revision_requested", {"verdict": result.verdict})
            if (allow_patches and attempt == 0 and result.allowed_patches
                    and result.suggested_patches):
                try:
                    revised = self.apply_patches(current, result)
                except Exception as error:
                    result = failed_review(error, "director_patch_invalid")
                    break
                current = revised
                revision_count = 1
                if emit:
                    emit("director_patch_applied", {
                        "revision_count": revision_count,
                        "patches": [item.model_dump() for item in result.suggested_patches],
                        "director_candidate": current.model_dump(mode="json"),
                    })
                continue
            break
        assert result is not None
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
