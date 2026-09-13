"""W5 视觉质检与人工标注的数据契约。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
QcFailureReason = Literal[
    "broken_hands",
    "watermark",
    "garbled_text",
    "composition",
    "persona_drift",
    "cinematography",
    "facial_expression",
    "ai_artifact",
    "physical_plausibility",
    "weak_visual_hook",
    "unclear_message",
    "style_mismatch",
    "audience_mismatch",
    "other",
]
ReworkStatus = Literal["pending", "approved", "cancelled"]
QcSuggestedDecision = Literal["approve", "reject", "undetermined"]
QcReviewPriority = Literal["normal", "high"]
QcBinaryCheck = Literal["broken_hands", "watermark", "composition"]
CreativeDeliverable = Literal[
    "still_image",
    "poster",
    "video_keyframe",
    "promo_keyframe",
    "manga_panel",
    "other",
]


class QcResult(BaseModel):
    """视觉模型和人工复核共同使用的最小结果契约。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    broken_hands: bool
    watermark: bool
    composition_ok: bool
    persona_consistency: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    reason: NonBlank


class QcPrediction(BaseModel):
    """一张图片的视觉模型预筛结果。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: NonBlank
    result: QcResult


class QcBaseline(BaseModel):
    """视觉预筛与人工真值的可复现对比结果。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    sample_count: int = Field(gt=0)
    broken_hands_accuracy: float = Field(ge=0, le=1)
    watermark_accuracy: float = Field(ge=0, le=1)
    hard_defect_accuracy: float = Field(ge=0, le=1)
    composition_accuracy: float = Field(ge=0, le=1)
    persona_mean_absolute_error: float = Field(ge=0, le=4)
    active_binary_checks: list[QcBinaryCheck] = Field(
        default_factory=lambda: ["broken_hands", "watermark", "composition"],
        min_length=1,
        max_length=3,
    )
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    threshold_covered_count: int = Field(ge=0)
    threshold_accuracy: float | None = Field(default=None, ge=0, le=1)
    low_confidence_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_active_checks(self) -> Self:
        if len(set(self.active_binary_checks)) != len(self.active_binary_checks):
            raise ValueError("启用的二元质检项不能重复")
        return self


class QcTriageItem(BaseModel):
    """模型给人工终审的排序建议；绝不表示已经自动通过或报废。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: NonBlank
    suggested_decision: QcSuggestedDecision
    review_priority: QcReviewPriority
    requires_human_review: Literal[True] = True
    confidence: float = Field(ge=0, le=1)
    failure_reasons: list[QcFailureReason] = Field(default_factory=list)
    reason: NonBlank


class QcEconomics(BaseModel):
    """一集图片生成与人工终审的经营指标。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    project: NonBlank
    episode: NonBlank
    expected_shots: int = Field(gt=0)
    attempted_shots: int = Field(ge=0)
    generation_attempts: int = Field(ge=0)
    reviewed_images: int = Field(ge=0)
    human_review_seconds: int = Field(ge=0)
    unmeasured_review_count: int = Field(ge=0)
    first_pass_approved_shots: int = Field(ge=0)
    final_approved_shots: int = Field(ge=0)
    rework_count: int = Field(ge=0)
    settled_fen: int = Field(ge=0)
    held_fen: int = Field(ge=0)
    unbilled_count: int = Field(ge=0)
    first_pass_rate: float = Field(ge=0, le=1)
    final_approval_rate: float = Field(ge=0, le=1)
    average_generations_per_shot: float = Field(ge=0)
    human_review_minutes: Decimal = Field(ge=0)
    cost_per_approved_shot_fen: Decimal | None = Field(default=None, ge=0)
    complete: bool


class HumanQcLabel(BaseModel):
    """一张视觉作品的人工真值，显式绑定用途、受众和创作目标。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: NonBlank
    image_path: Path
    deliverable_type: CreativeDeliverable = "still_image"
    target_platform: NonBlank
    genre: NonBlank
    target_audience: NonBlank
    business_goal: NonBlank | None = None
    key_message: NonBlank | None = None
    first_glance_goal: NonBlank | None = None
    visual_style: NonBlank | None = None
    style_reference: NonBlank | None = None
    persona_reference: NonBlank | None = None
    cinematography_requirements: NonBlank
    cinematography_notes: NonBlank
    review_seconds: int | None = Field(default=None, gt=0)
    result: QcResult
    approved: bool
    failure_reasons: list[QcFailureReason] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        hard_failure = (
            self.result.broken_hands
            or self.result.watermark
            or not self.result.composition_ok
        )
        if hard_failure and self.approved:
            raise ValueError("存在硬缺陷时不能人工通过")
        if self.approved and self.failure_reasons:
            raise ValueError("通过样本不能包含失败原因")
        if not self.approved and not self.failure_reasons:
            raise ValueError("未通过样本必须包含至少一个标准化失败原因")
        return self


class ReworkItem(BaseModel):
    """人工拒绝后形成的返工请求；入队本身不代表允许再次付费。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_request_id: NonBlank
    failure_reasons: list[QcFailureReason] = Field(min_length=1)
    reason: NonBlank
    status: ReworkStatus
    target_request_id: NonBlank | None = None
    created_at: NonBlank
    updated_at: NonBlank


class ReworkPlan(BaseModel):
    """从人工失败原因生成的最小改动计划；计划本身不授权付费。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_request_id: NonBlank
    preserve_constraints: list[NonBlank] = Field(min_length=1)
    correction_directives: list[NonBlank] = Field(min_length=1)
    evidence: NonBlank
    requires_human_approval: Literal[True] = True
