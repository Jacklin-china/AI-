"""Comic Domain 的状态；Core 不导入本模块。"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from kantoku.core.conversation_context import SourceRef
from kantoku.core.state import RunState


class ComicState(RunState):
    """单张创作从生成到归档的可恢复状态。"""

    project: str
    prompt: str
    execution_mode: Literal["fast", "professional"] = "professional"
    conversation_id: str | None = None
    message_id: str | None = None
    trace_id: str | None = None
    # Existing Run/checkpoint payload, not a second project or runtime store.
    quick_creation: dict[str, Any] | None = None
    total_estimate_fen: int | None = Field(default=None, gt=0)
    shot_no: int = Field(gt=0)
    estimate_fen: int = Field(gt=0)
    image_count: int = Field(default=1, ge=1, le=20)
    confirmed: bool = False
    cost_decision: str | None = None
    target_platform: str = "studio"
    genre: str = "comic"
    target_audience: str = "general"
    visual_style: str = "illustration"
    cinematography_requirements: str = "主体清楚，构图服务叙事"
    request_id: str | None = None
    image_path: str | None = None
    provider_job_id: str | None = None
    qc_result: dict[str, Any] | None = None
    qc_passed: bool | None = None
    approval_decision: str | None = None
    rework_count: int = Field(default=0, ge=0)
    max_reworks: int = Field(default=1, ge=0, le=3)
    archive_path: str | None = None
    image_artifact_id: str | None = None
    video_artifact_id: str | None = None


ProjectText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
BriefText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
BriefItem = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    PRODUCTION = "production"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class CreativeConstraint(BaseModel):
    """有原文依据的视觉要求；模型选择不升级成用户事实。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    constraint_id: ProjectText
    requirement: BriefItem
    target: BriefItem
    scope: Literal["identity", "world", "style", "shot", "output"]
    source_ref: SourceRef
    source_quote: BriefText
    origin: Literal["user_request", "asset_fact", "model_choice"]
    trust: Literal["source_verified", "creative_choice"]
    status: Literal["active", "superseded", "withdrawn"] = "active"
    verification: Literal["semantic", "exact"] = "semantic"
    literal_value: BriefItem | None = None

    @model_validator(mode="after")
    def validate_trust(self) -> CreativeConstraint:
        if (self.origin == "model_choice") != (self.trust == "creative_choice"):
            raise ValueError("创作选择与已核验事实必须分别记录")
        if self.verification == "exact":
            if self.literal_value is None:
                self.literal_value = self.requirement  # 保留已写入的早期 exact 记录。
            if self.literal_value not in self.source_quote:
                raise ValueError("逐字要求必须存在于来源原文")
        elif self.literal_value is not None:
            raise ValueError("普通语义要求不能携带原样文字值")
        return self


class CreativeBriefInput(BaseModel):
    """用户明确提供的创意边界；空列表不会被猜测成模型已理解的内容。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    original_request: BriefText
    hard_constraints: list[BriefItem] = Field(default_factory=list, max_length=50)
    soft_preferences: list[BriefItem] = Field(default_factory=list, max_length=50)
    creative_freedom: list[BriefItem] = Field(default_factory=list, max_length=50)
    constraint_contract_version: Literal[2] | None = None
    semantic_constraints: list[CreativeConstraint] = Field(default_factory=list, max_length=50)
    constraint_evidence: dict[str, Any] = Field(default_factory=dict, max_length=10)

    @model_validator(mode="after")
    def unique_items(self) -> CreativeBriefInput:
        for name in ("hard_constraints", "soft_preferences", "creative_freedom"):
            items = getattr(self, name)
            if len(items) != len(set(items)):
                raise ValueError(f"{name} 不允许重复")
        if self.semantic_constraints and self.constraint_contract_version != 2:
            raise ValueError("语义约束必须声明契约版本")
        if len({item.constraint_id for item in self.semantic_constraints}) != len(
            self.semantic_constraints
        ):
            raise ValueError("约束身份不能重复")
        if self.constraint_contract_version == 2 and self.hard_constraints != list(dict.fromkeys(
            item.requirement for item in self.semantic_constraints
            if item.status == "active" and item.origin == "user_request"
        )):
            raise ValueError("语义版本的硬约束必须来自活跃的用户要求")
        return self


class ComicProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: ProjectText
    description: str = Field(default="", max_length=2000)
    brief: CreativeBriefInput | None = None


class ComicWorkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: ProjectText
    title: ProjectText = "未命名作品"


class ComicWorkWrite(ComicWorkCreate):
    expected_version: int = Field(ge=1)


class ComicImageTaskCreate(ComicWorkWrite):
    title: ProjectText = "新画面"
    requirement: str = Field(default="", max_length=1000)


class ComicPlanRecovery(ComicWorkWrite):
    source_run_groups: list[list[ProjectText]] = Field(min_length=1, max_length=50)


class ComicTaskTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: ProjectText
    conversation_id: ProjectText
    expected_project_version: int = Field(ge=1)
    expected_director_version: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=1000)
    creation_mode: Literal["fast", "professional"] = "professional"
    image: ImageVersionRef | None = None


class CreativeBriefUpdate(CreativeBriefInput):
    expected_version: int = Field(ge=1)


class CreativeBriefFork(CreativeBriefUpdate):
    """新创意的追加修订；父版本只作溯源，不能进入新创意的模型上下文。"""

    parent_brief_id: str = Field(min_length=1, max_length=100)
    parent_brief_version: int = Field(ge=1)
    reason: BriefText


class CreativeIntentBoundary(BaseModel):
    """公开创意边界判断，不是导演方案、Prompt 或模型思维链。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    new_creative_direction: bool
    reason: BriefText
    new_brief: CreativeBriefInput | None = None

    @model_validator(mode="after")
    def require_new_brief(self) -> CreativeIntentBoundary:
        if self.new_creative_direction != (self.new_brief is not None):
            raise ValueError("新创意必须提供独立 Brief，同方向不得替换 Brief")
        return self


class CreativeProject(BaseModel):
    """作品根对象；current_version 在每次作品内容变更时递增。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: str
    title: ProjectText
    description: str
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
    current_version: int = Field(ge=1)
    brief_id: str
    brief_version: int = Field(ge=1)
    director_id: str | None = None
    director_version: int | None = Field(default=None, ge=1)


class CreativeBrief(CreativeBriefInput):
    brief_id: str
    project_id: str
    version: int = Field(ge=1)
    created_at: datetime
    status: Literal["active", "archived"] = "active"
    parent_brief_id: str | None = None
    parent_brief_version: int | None = Field(default=None, ge=1)
    fork_reason: BriefText | None = None


class ComicProjectSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: CreativeProject
    creative_brief: CreativeBrief


class ComicContext(BaseModel):
    """只传任务必需信息；后续资产与镜头阶段填充 relevant_memory。"""

    model_config = ConfigDict(extra="forbid")

    stable_context: dict[str, Any]
    relevant_memory: list[dict[str, Any]]
    current_task: str | None
    source_versions: dict[str, int]


class CreativeDecision(BaseModel):
    """公开的创意理解结果，不包含模型私有思维链或供应商 Prompt。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    intent_summary: BriefText
    narrative_context: BriefText
    emotional_target: BriefText
    audience_experience: BriefText
    narrative_focus: BriefText
    hard_constraints: list[BriefItem] = Field(default_factory=list, max_length=50)
    soft_preferences: list[BriefItem] = Field(default_factory=list, max_length=50)
    creative_freedom: list[BriefItem] = Field(default_factory=list, max_length=50)
    unresolved_questions: list[BriefItem] = Field(default_factory=list, max_length=20)


class DirectorPlan(BaseModel):
    """导演对视觉表达的决策，而不是固定关键词到镜头的映射。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    visual_strategy: BriefText
    visual_focus: BriefText
    subject_environment_relation: BriefText
    composition_strategy: BriefText
    color_strategy: BriefText
    style_boundary: BriefText | None = None
    characters_present: bool | None = None
    character_expression: BriefText | None = None
    character_pose: BriefText | None = None
    character_presence: BriefText | None = None
    continuity_rules: list[BriefItem] = Field(default_factory=list, max_length=50)
    creative_choices: list[BriefItem] = Field(default_factory=list, max_length=20)
    risk_flags: list[BriefItem] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def character_applicability(self) -> DirectorPlan:
        if self.characters_present is False and any(getattr(self, name) is not None for name in (
            "character_presence", "character_pose", "character_expression",
        )):
            raise ValueError("没有人物时，人物身份、姿态与表情字段应留 null")
        return self


class CinematographyPlan(BaseModel):
    """把导演意图转成可编辑的摄影决策；不直接生成供应商参数。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["complete", "needs_revision", "missing"] = "complete"
    public_decision: BriefText | None = None
    creative_reason: BriefText | None = None
    shot_size: BriefText | None = None
    camera_angle: BriefText | None = None
    camera_distance: BriefText | None = None
    # 兼容审核模型可能使用的总括摄影语言；具体镜头字段仍是首选来源。
    camera_language: BriefText | None = None
    spatial_feel: BriefText | None = None
    lens_or_spatial_feel: BriefText | None = None
    movement: BriefText | None = None
    lighting: BriefText | None = None
    light_source: BriefText | None = None
    light_direction: BriefText | None = None
    color_relationship: BriefText | None = None
    depth_strategy: BriefText | None = None
    material_language: BriefText | None = None
    unresolved_decisions: list[BriefItem] = Field(default_factory=list, max_length=10)

    @property
    def missing_fields(self) -> list[str]:
        return [name for name in (
            "shot_size", "camera_angle", "camera_distance", "spatial_feel",
            "lens_or_spatial_feel", "lighting", "light_source", "light_direction",
            "color_relationship", "depth_strategy", "material_language",
        ) if getattr(self, name) is None]

    @model_validator(mode="after")
    def validate_completeness(self) -> CinematographyPlan:
        if self.status == "missing" and len(self.missing_fields) < 11:
            # 用户在现有草稿编辑入口补字段时，状态随真实内容更新，不要求改隐藏状态。
            self.status = "needs_revision"
        if self.unresolved_decisions:
            self.status = "needs_revision"
        if (self.status == "needs_revision" and not self.missing_fields
                and not self.unresolved_decisions):
            self.status = "complete"
        if self.status == "complete" and self.missing_fields:
            raise ValueError("完整摄影方案不能缺少摄影字段")
        return self


class DirectorCriticFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    severity: Literal["info", "warning", "error"]
    code: BriefItem = "LEGACY_FINDING"
    field_path: str = Field(default="", max_length=200)
    evidence: str = Field(default="", max_length=4000)
    expected: str = Field(default="", max_length=4000)
    suggested_action: str = Field(default="", max_length=2000)
    # 保留既有修订的可读性；新审核使用上面的公开证据字段。
    category: str = Field(default="", max_length=300)
    message: str = Field(default="", max_length=4000)


class DirectorCriticPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    field: BriefItem
    reason: BriefText
    value: str | None = Field(default=None, min_length=1, max_length=4000)
    expected_value: str | None = Field(default=None, max_length=4000)


class DirectorCriticResult(BaseModel):
    """生成前的公开审核结果；不保存模型隐藏推理。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["pass", "needs_revision", "blocked"]
    public_summary: BriefText
    findings: list[DirectorCriticFinding] = Field(default_factory=list, max_length=50)
    suggested_patches: list[DirectorCriticPatch] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    knowledge_refs: list[BriefItem] = Field(default_factory=list, max_length=50)
    allowed_patches: list[BriefItem] = Field(default_factory=list, max_length=20)
    review_version: str | None = Field(default=None, max_length=100)
    reviewed_spec_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_request_id: str | None = Field(default=None, max_length=100)


DirectorSource = Literal["user_fact", "asset_fact", "skill_method", "model_choice", "manual_edit"]


class DirectorEvidence(BaseModel):
    """可解析的依据；knowledge 只是方法输入，不能证明一个作品事实。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    source_type: DirectorSource
    reference: BriefItem
    field_path: BriefItem | None = None
    version: int | None = Field(default=None, ge=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DirectorFieldProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_type: DirectorSource
    trust_status: Literal["confirmed_fact", "creative_choice", "unresolved"]
    value_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: list[DirectorEvidence] = Field(default_factory=list, max_length=16)
    previous_source: DirectorSource | None = None
    previous_value_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    derived_from: BriefItem | None = None


class DirectorSpecDraft(BaseModel):
    """可编辑的导演决策，不包含供应商 Prompt 或生成参数。

    ``schema_version=1`` 表示现有扁平 DirectorSpec；
    ``schema_version=2`` 才允许保存分层导演决策。这样旧调用方可以继续写入
    v1，而 Phase 7.2 Coordinator 准备好完整结构后再显式写入 v2。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1, 2] = 1
    execution_policy: Literal["single_image"] | None = None
    visual_direction: BriefText
    storytelling_goal: BriefText
    camera_language: BriefText
    composition: BriefText
    lighting: BriefText
    color_language: BriefText
    emotion: BriefText
    character_focus: BriefText
    constraints: list[BriefItem] = Field(default_factory=list, max_length=50)
    creative_choices: list[BriefItem] = Field(min_length=1, max_length=20)
    creative_decision: CreativeDecision | None = None
    director_plan: DirectorPlan | None = None
    cinematography: CinematographyPlan | None = None
    critic_result: DirectorCriticResult | None = None
    critic_status: Literal["unavailable"] | None = None
    knowledge_refs: list[BriefItem] = Field(default_factory=list, max_length=50)
    field_provenance: dict[str, DirectorFieldProvenance] = Field(
        default_factory=dict, max_length=100,
    )
    asset_versions: dict[str, int] = Field(default_factory=dict, max_length=50)
    storyboard_version: int | None = Field(default=None, ge=1)
    shot_version: int | None = Field(default=None, ge=1)
    reference_context: dict[str, Any] = Field(default_factory=dict, max_length=3)

    @model_validator(mode="after")
    def validate_layered_v2(self) -> DirectorSpecDraft:
        if self.schema_version == 2 and (
            self.creative_decision is None
            or self.director_plan is None
            or self.cinematography is None
        ):
            raise ValueError(
                "DirectorSpec v2 必须包含 creative_decision、director_plan 和 cinematography"
            )
        if (
            self.schema_version == 2
            and self.cinematography is not None
            and self.cinematography.status == "complete"
            and self.cinematography.camera_language is None
        ):
            # 旧 v2 记录只有顶层兼容字段。读取时投影到分层结构，避免迁移覆盖历史数据。
            self.cinematography = self.cinematography.model_copy(
                update={"camera_language": self.camera_language},
            )
        return self


class DirectorSpec(DirectorSpecDraft):
    spec_id: str
    project_id: str
    creative_brief_version: int = Field(ge=1)
    version: int = Field(ge=1)
    created_at: datetime
    source: Literal["model", "manual", "restored"]
    restored_from_version: int | None = Field(default=None, ge=1)

GenerationMode = Literal["FIRST_GENERATION", "USER_EDIT", "NEW_CONTEXT"]


class CreativeContextLock(BaseModel):
    """Conversation-scoped creative anchors, backed by immutable domain revisions."""

    model_config = ConfigDict(extra="forbid", strict=True)

    director_version: int = Field(ge=1)
    brief_version: int = Field(ge=1)
    bible_sources: dict[str, Any] = Field(default_factory=dict)
    character: str = ""
    world: str = ""
    visual_style: str = ""
    camera_style: str = ""
    environment: str = ""
    fixed_attributes: list[str] = Field(default_factory=list)


class DirectorSpecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    request_id: str | None = Field(default=None, min_length=1, max_length=100)
    task: str | None = Field(default=None, max_length=1000)
    draft: DirectorSpecDraft | None = None
    asset_ids: list[str] = Field(default_factory=list, max_length=8)
    conversation_id: str | None = Field(default=None, max_length=100)
    creation_mode: Literal["fast", "professional"] | None = None
    storyboard_id: str | None = Field(default=None, max_length=100)
    shot_id: str | None = Field(default=None, max_length=100)
    previous_run_id: str | None = Field(default=None, max_length=100)
    rerun_from: Literal[
        "creative_understanding", "visual_direction", "cinematography",
        "director_critic", "director_assemble",
    ] | None = None
    stage_edits: dict[str, Any] = Field(default_factory=dict, max_length=1)
    resume_run_id: str | None = Field(default=None, max_length=100)
    creative_operation: Literal["auto", "new"] = "auto"
    revision_instruction: str | None = Field(default=None, min_length=1, max_length=1000)
    expected_director_version: int | None = Field(default=None, ge=1)
    review_current: bool = False
    independent_plan: bool = False
    target_task_id: str | None = Field(default=None, max_length=100)
    expected_target_project_version: int | None = Field(default=None, ge=1)
    turn_operation: Literal["auto", "revise"] = "auto"
    turn_image: ImageVersionRef | None = None

    @model_validator(mode="after")
    def validate_creation_mode(self) -> DirectorSpecRequest:
        if self.independent_plan and (
            not self.task or not self.conversation_id or self.creation_mode is None
            or self.draft or self.revision_instruction or self.review_current
            or self.previous_run_id or self.resume_run_id or self.stage_edits
            or self.storyboard_id or self.shot_id
        ):
            raise ValueError("独立方案需要本轮创作需求，不能混入旧方案操作")
        if self.revision_instruction or self.review_current:
            if self.expected_director_version is None:
                raise ValueError("修改或审核草稿必须绑定导演版本")
            if self.task or self.previous_run_id or self.resume_run_id or self.stage_edits:
                raise ValueError("当前草稿操作不能混入新创意或历史任务")
        if self.revision_instruction and (self.draft or self.creation_mode):
            raise ValueError("指令修改先保存草稿，不绕过审核")
        if self.review_current and (self.creation_mode is None or self.draft):
            raise ValueError("审核当前草稿需要创作模式且不能传入审核结果")
        if self.creation_mode is None and (
            self.previous_run_id or self.rerun_from or self.stage_edits or self.resume_run_id
        ):
            raise ValueError("节点操作需要 creation_mode")
        if self.creation_mode is not None and self.draft is not None:
            raise ValueError("模式任务不能通过 draft 绕过导演审核，请修改节点后重新审核")
        if (self.previous_run_id is None) != (self.rerun_from is None):
            raise ValueError("重跑节点需要来源 Run 和起始节点")
        if self.resume_run_id and (self.previous_run_id or self.stage_edits):
            raise ValueError("恢复与节点编辑不能同时提交")
        if self.stage_edits and (
            self.creation_mode != "professional" or self.rerun_from not in {
                "creative_understanding", "visual_direction", "cinematography",
            } or set(self.stage_edits) != {self.rerun_from}
        ):
            raise ValueError("仅专业模式允许修改当前创意、导演或摄影节点")
        return self


class DirectorSpecRestore(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    version: int = Field(ge=1)


class CharacterAsset(BaseModel):
    """角色的稳定视觉锚点；未知细节保持空值，不由存储层猜测。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["character"] = "character"
    appearance: str = Field(default="", max_length=2000)
    outfit: str = Field(default="", max_length=2000)
    features: list[BriefItem] = Field(default_factory=list, max_length=30)


class SceneAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["scene"] = "scene"
    location: str = Field(default="", max_length=1000)
    time_of_day: str = Field(default="", max_length=300)
    weather: str = Field(default="", max_length=300)
    lighting: str = Field(default="", max_length=1000)
    atmosphere: str = Field(default="", max_length=1000)
    environment_features: list[BriefItem] = Field(default_factory=list, max_length=30)


class StyleBible(BaseModel):
    """作品风格参考，不是固定提示词模板。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["style"] = "style"
    art_direction: str = Field(default="", max_length=2000)
    color_language: str = Field(default="", max_length=1000)
    materials: str = Field(default="", max_length=1000)
    camera_language: str = Field(default="", max_length=1000)
    lighting: str = Field(default="", max_length=1000)


class ComicAssetDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: ProjectText
    aliases: list[BriefItem] = Field(default_factory=list, max_length=20)
    details: Annotated[CharacterAsset | SceneAsset | StyleBible, Field(discriminator="kind")]
    fixed_constraints: list[BriefItem] = Field(default_factory=list, max_length=50)
    reference_artifact_ids: list[BriefItem] = Field(default_factory=list, max_length=20)
    tags: list[BriefItem] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check_asset(self) -> ComicAssetDraft:
        for name in ("aliases", "fixed_constraints", "reference_artifact_ids", "tags"):
            items = getattr(self, name)
            if len(items) != len(set(items)):
                raise ValueError(f"{name} 不允许重复")
        content = self.details.model_dump(exclude={"kind"})
        if not any(value for value in content.values()):
            raise ValueError("资产至少需要一项实际描述")
        return self


class ComicAsset(ComicAssetDraft):
    asset_id: str
    project_id: str
    version: int = Field(ge=1)
    project_version: int = Field(ge=1)
    created_at: datetime
    state: Literal["active", "deleted"] = "active"
    pinned_version: int | None = Field(default=None, ge=1)
    source: Literal["created", "edited", "restored", "locked", "deleted"]
    restored_from_version: int | None = Field(default=None, ge=1)


class ComicAssetRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    asset_id: str = Field(min_length=1, max_length=100)
    version: int | None = Field(default=None, ge=1)


class ComicAssetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    asset: ComicAssetDraft


class ComicAssetEditRequest(ComicAssetCreateRequest):
    expected_asset_version: int = Field(ge=1)


class ComicWorkSettingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: ProjectText
    expected_version: int = Field(ge=1)
    asset: ComicAssetDraft
    asset_id: ProjectText | None = None
    expected_asset_version: int | None = Field(default=None, ge=1)
    source_task_id: ProjectText | None = None
    source_asset_id: ProjectText | None = None
    source_version: int | None = Field(default=None, ge=1)


class ComicTaskSettingsSync(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: ProjectText
    expected_project_version: int = Field(ge=1)
    refs: list[ComicAssetRef] = Field(max_length=8)


class ComicLegacyImportRequest(ComicWorkCreate):
    conversation_id: ProjectText
    preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ids: list[ProjectText] = Field(min_length=1, max_length=100)
    include_story: bool = False


class ComicAssetVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_asset_version: int = Field(ge=1)
    version: int = Field(ge=1)


class ComicAssetLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_asset_version: int = Field(ge=1)
    version: int | None = Field(default=None, ge=1)


class ComicAssetDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_asset_version: int = Field(ge=1)


class StoryboardStatus(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    APPROVED = "approved"
    ARCHIVED = "archived"


class ShotStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    GENERATING = "generating"
    CHECKING = "checking"
    APPROVED = "approved"
    FAILED = "failed"
    REPAIRING = "repairing"
    DELETED = "deleted"


class ComicShotAssetRef(ComicAssetRef):
    """Shot 永远固定到具体资产版本，不追随最新版本漂移。"""

    version: int = Field(ge=1)


class ComicShotDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    purpose: BriefText
    subject: BriefText
    action: str = Field(default="", max_length=2000)
    environment: str = Field(default="", max_length=2000)
    emotion: str = Field(default="", max_length=1000)
    shot_size: str = Field(default="", max_length=300)
    camera_angle: str = Field(default="", max_length=300)
    camera_movement: str = Field(default="", max_length=300)
    character_asset_versions: list[ComicShotAssetRef] = Field(default_factory=list, max_length=8)
    scene_asset_versions: list[ComicShotAssetRef] = Field(default_factory=list, max_length=8)
    style_version: ComicShotAssetRef | None = None


class ComicShot(ComicShotDraft):
    shot_id: str
    storyboard_id: str
    project_id: str
    sequence_number: int = Field(ge=1)
    version: int = Field(ge=1)
    project_version: int = Field(ge=1)
    status: ShotStatus
    created_at: datetime
    source: Literal["created", "edited", "restored", "reordered", "deleted", "model"]
    restored_from_version: int | None = Field(default=None, ge=1)


class ComicStoryboardDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: ProjectText
    description: str = Field(default="", max_length=2000)


class ComicStoryboard(ComicStoryboardDraft):
    storyboard_id: str
    project_id: str
    director_spec_version: int = Field(ge=1)
    version: int = Field(ge=1)
    project_version: int = Field(ge=1)
    status: StoryboardStatus
    shot_ids: list[str]
    created_at: datetime
    source: Literal["created", "edited", "restored", "reordered", "model"]
    restored_from_version: int | None = Field(default=None, ge=1)


class ComicStoryboardPlan(ComicStoryboardDraft):
    shots: list[ComicShotDraft] = Field(min_length=1, max_length=100)


class ComicStoryboardCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    draft: ComicStoryboardDraft | None = None
    generate: bool = False
    task: str | None = Field(default=None, max_length=1000)
    asset_ids: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def check_mode(self) -> ComicStoryboardCreateRequest:
        if self.generate == (self.draft is not None):
            raise ValueError("只能选择 AI 规划或提供手动分镜草案")
        return self


class ComicStoryboardEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    draft: ComicStoryboardDraft
    status: Literal["draft", "planning", "approved", "archived"]
    shot_ids: list[str] | None = None


class ComicShotCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_storyboard_version: int = Field(ge=1)
    shot: ComicShotDraft


class ComicShotEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    shot: ComicShotDraft
    status: Literal["draft", "planned"]


class ComicVersionRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    version: int = Field(ge=1)


class ComicVersionDeleteRequest(BaseModel):
    """删除单个历史版本：作品级 CAS + 目标版本号。

    只删除 comic_entity_versions 中该版本的一行，不触碰其他版本；
    "当前正在使用的版本"由服务层拒绝，这里不做隐式回退。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    version: int = Field(ge=1)


class ComicShotDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class ComicPromptContext(BaseModel):
    """Prompt 分段字段契约；使用已有 Prompt 修订，不另建 Bible 存储。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    labels: ClassVar[dict[str, str]]
    frame_terms: ClassVar[str] = (
        r"景别|机位|构图|镜头方向|主光方向|光线方向|仰拍|俯拍|低角度|低机位|平视|"
        r"远景|中景|近景|特写|前景|后景|竖幅|横幅|侧脸|剪影辨识|"
        r"三分之一|黄金分割|画面(?:左|右|上|下)|"
        r"(?:主光|月光|灯光|光线).{0,8}(?:左|右|上方|下方|照射)|"
        r"low[- ]angle|high[- ]angle|close[- ]up|camera angle|shot size"
    )
    action_terms: ClassVar[str] = (
        r"站立|站姿|站在|远望|战斗姿态|战斗动作|拔剑|冲刺|转身|背影|"
        r"抬头(?!纹)|回眸|握剑|蹲姿|卧姿|目光(?:朝|向)|"
        r"夜立|登临|静观|脊背挺直|束于身前|衣带自然垂落|轻扬|飘动|"
        r"standing|fighting pose|looking (?:at|toward)"
    )
    lighting_terms: ClassVar[str] = (
        r"光源|受光|背光|轮廓光|落影|画外|光照|月轮.{0,8}入画|微亮.{0,8}转暗"
    )

    @classmethod
    def shot_field(cls, value: str) -> str | None:
        if re.search(cls.lighting_terms, value, re.IGNORECASE):
            return "lighting_changes"
        if re.search(cls.frame_terms, value, re.IGNORECASE):
            return "composition" if re.search(
                r"构图|前景|中景|后景|三分之一|黄金分割|画面", value) else "camera"
        if re.search(cls.action_terms, value, re.IGNORECASE):
            return "pose"
        return None

    @model_validator(mode="after")
    def validate_scope(self, info: ValidationInfo) -> ComicPromptContext:
        values = self.model_dump()
        if (not any(value.strip() for value in values.values())
                and not (info.context or {}).get("semantic_scope")):
            raise ValueError("Context 不能全部为空")
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"{name} 必须是单项描述，不能夹入其他字段")
            if (not (info.context or {}).get("semantic_scope")
                    and not isinstance(self, ShotPromptContext) and self.shot_field(value)):
                raise ValueError(f"{name} 包含镜头信息，请移入 Shot Context")
        return self

    def render(self) -> str:
        return "\n".join(
            f"{self.labels[name]}：{value}" for name, value in self.model_dump().items()
            if value.strip()
        )

    @classmethod
    def from_text(cls, text: str, *, semantic_scope: bool = False) -> ComicPromptContext:
        names = {label: name for name, label in cls.labels.items()}
        values = {}
        for line in text.splitlines():
            label, separator, value = line.partition("：")
            if not separator or label not in names or names[label] in values:
                raise ValueError(f"{cls.__name__} 字段标题不合法或重复")
            values[names[label]] = value.strip()
        return cls.model_validate(values, context={"semantic_scope": semantic_scope})


class CharacterPromptContext(ComicPromptContext):
    """持续角色身份，不含当镜头行为、姿态或摄影。"""

    labels = {"identity": "身份", "appearance": "外貌", "age": "年龄",
              "clothing": "服装", "fixed_features": "固定特征"}
    identity: str = Field(default="", max_length=300, validate_default=True)
    appearance: str = Field(default="", max_length=600)
    age: str = Field(default="", max_length=80)
    clothing: str = Field(default="", max_length=600)
    fixed_features: str = Field(default="", max_length=600)

    @field_validator("identity")
    @classmethod
    def require_legacy_identity(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip() and not (info.context or {}).get("semantic_scope"):
            raise ValueError("身份不能为空")
        return value.strip()

class WorldPromptContext(ComicPromptContext):
    labels = {"location": "地点", "time": "时间", "world_setting": "世界观",
              "environment": "环境"}
    location: str = Field(default="", max_length=600)
    time: str = Field(default="", max_length=300)
    world_setting: str = Field(default="", max_length=600)
    environment: str = Field(default="", max_length=1000)


class StylePromptContext(ComicPromptContext):
    labels = {"art_style": "艺术风格", "photographic_style": "摄影风格",
              "color": "色彩", "materials": "材质"}
    art_style: str = Field(default="", max_length=600)
    photographic_style: str = Field(default="", max_length=600)
    color: str = Field(default="", max_length=600)
    materials: str = Field(default="", max_length=600)


class ShotPromptContext(ComicPromptContext):
    labels = {"action": "动作", "pose": "姿态", "composition": "构图", "camera": "镜头",
              "lighting_changes": "光影变化", "literal_text": "指定文字"}
    action: str = Field(default="", max_length=600)
    pose: str = Field(default="", max_length=600)
    composition: str = Field(default="", max_length=600)
    camera: str = Field(default="", max_length=600)
    lighting_changes: str = Field(default="", max_length=1000)
    literal_text: str = Field(default="", max_length=1000)


class ComicPromptContexts(BaseModel):
    """完整图片 Prompt 四段的唯一结构；正文从这些字段渲染。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    character_context: CharacterPromptContext
    world_context: WorldPromptContext
    style_context: StylePromptContext
    shot_context: ShotPromptContext

    @model_validator(mode="after")
    def require_content(self) -> ComicPromptContexts:
        if not any(value.strip() for name in ComicPromptContexts.model_fields
                   for value in getattr(self, name).model_dump().values()):
            raise ValueError("Prompt 不能只有空段落")
        return self

    @classmethod
    def from_sections(cls, sections: dict[str, str], *,
                      semantic_scope: bool = False) -> ComicPromptContexts:
        return cls.model_validate({name: cls.model_fields[name].annotation.from_text(
            text, semantic_scope=semantic_scope).model_dump() for name, text in sections.items()},
            context={"semantic_scope": semantic_scope})

    def sections(self) -> dict[str, str]:
        return {name: getattr(self, name).render() for name in ComicPromptContexts.model_fields}


class ComicPromptDraft(BaseModel):
    """编译结果；内容可编辑，但不能伪装成已经调用生图模型。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    director_summary: BriefText
    positive_prompt: BriefText
    negative_prompt: str = Field(default="", max_length=4000)
    context_version: Literal["image-bibles-1"] | None = None
    character_context: str = Field(default="", max_length=2000)
    world_context: str = Field(default="", max_length=2000)
    style_context: str = Field(default="", max_length=2000)
    shot_context: str = Field(default="", max_length=2000)
    context_sources: dict[str, Any] = Field(default_factory=dict, max_length=20)


class CompleteImagePromptOutput(ComicPromptContexts):
    """编译模型的完整输出；四段、摘要与负向正文必须处于同一 Schema。"""
    director_summary: BriefText
    negative_prompt: BriefText


class ImageContinuityChange(BaseModel):
    """只有本轮原话授权的 Prompt 叶子字段可以解除图片约束。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    field: str = Field(json_schema_extra={"enum": [
        f"{section}.{leaf}" for section, body in ComicPromptContexts.model_fields.items()
        for leaf in body.annotation.model_fields
    ]})
    quote: str = Field(min_length=1, max_length=1000)

    @field_validator("quote")
    @classmethod
    def require_quote_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("修改依据不能只有空白")
        return value

    @field_validator("field")
    @classmethod
    def prompt_leaf(cls, value: str) -> str:
        section, _, leaf = value.partition(".")
        field = ComicPromptContexts.model_fields.get(section)
        if field is None or leaf not in field.annotation.model_fields:
            raise ValueError("只能指定现有 Prompt 叶子字段")
        return value


class ImageContinuityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: str | None = None
    changes: list[ImageContinuityChange] = Field(default_factory=list, max_length=30)
    new_context_quote: str | None = Field(default=None, max_length=1000)
    question: str | None = Field(default=None, max_length=300)

    @field_validator("candidate_id")
    @classmethod
    def resolve_candidate_identity(cls, value: str | None, info: ValidationInfo) -> str | None:
        context = info.context or {}
        if value is None or "candidate_ids" not in context:
            return value
        if value in context["candidate_ids"]:
            return value
        matches = context.get("candidate_aliases", {}).get(value, ())
        if len(matches) == 1:
            return matches[0]
        raise ValueError("图片来源不在本轮候选中，或对应多个文字状态")

    @field_validator("new_context_quote", "question")
    @classmethod
    def require_decision_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("独立画面依据或澄清问题不能只有空白")
        return value

    @model_validator(mode="after")
    def require_decision(self) -> ImageContinuityDecision:
        if self.candidate_id is None and not (self.new_context_quote or self.question):
            raise ValueError("必须选择图片、说明独立画面依据或提出澄清问题")
        if self.candidate_id is not None and self.new_context_quote is not None:
            raise ValueError("选择图片与独立画面依据不能同时提供")
        if self.new_context_quote is not None and self.changes:
            raise ValueError("独立画面不能同时释放已有图片的修改字段")
        return self


class ComicPromptArtifact(ComicPromptDraft):
    prompt_id: str
    artifact_id: str
    project_id: str
    storyboard_id: str
    shot_id: str
    version: int = Field(ge=1)
    project_version: int = Field(ge=1)
    creative_brief_version: int = Field(ge=1)
    director_spec_version: int = Field(ge=1)
    storyboard_version: int = Field(ge=1)
    shot_version: int = Field(ge=1)
    character_asset_versions: list[ComicShotAssetRef]
    scene_asset_versions: list[ComicShotAssetRef]
    style_version: ComicShotAssetRef | None
    original_intent: BriefText
    model_target: ProjectText
    compiler_version: ProjectText
    created_at: datetime
    source: Literal["compiled", "edited", "restored"]
    restored_from_version: int | None = Field(default=None, ge=1)


class ComicPromptCompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_shot_version: int = Field(ge=1)
    image_mode: Literal["provider", "external"] = "provider"
    conversation_id: str | None = None
    complete_prompt: bool = False


class ComicPromptEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    draft: ComicPromptDraft


class ComicPromptConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_project_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    conversation_id: BriefItem


class ComicShotImageRequest(BaseModel):
    """Future capability：未暴露路由，外部图片主流程不使用此请求。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_project_version: int = Field(ge=1)
    expected_shot_version: int = Field(ge=1)
    prompt_version: int = Field(ge=1)
    conversation_id: BriefItem
    request_id: BriefItem
    model: ProjectText | None = None


class ComicExternalImageRequest(BaseModel):
    """用户上传的文件，不是图片生成请求。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_project_version: int = Field(ge=1)
    expected_shot_version: int = Field(ge=1)
    expected_prompt_version: int = Field(ge=1)
    conversation_id: BriefItem
    filename: ProjectText
    data_url: str = Field(min_length=1, max_length=7_000_000)


class ImageVersionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    image_id: ProjectText
    version: int = Field(ge=1)


class ImageVersion(ImageVersionRef):
    """不可变内容身份；查看/选择分别保存，不重写图片来源。"""
    schema_version: Literal[1] = 1
    project_id: ProjectText
    conversation_id: ProjectText
    artifact_id: ProjectText
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: ProjectText
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    parent: ImageVersionRef | None = None
    instruction_artifact_id: ProjectText | None = None
    provenance: Literal["direct_upload", "external_edit", "prompt_output"]
    created_at: datetime


class ComicMediaMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: ProjectText
    conversation_id: ProjectText
    expected_project_version: int = Field(ge=1)


class ComicImageImportRequest(ComicMediaMutation):
    filename: ProjectText
    data_url: str = Field(min_length=1, max_length=7_000_000)
    image_id: ProjectText | None = None
    expected_image_version: int | None = Field(default=None, ge=1)
    parent: ImageVersionRef | None = None
    instruction_artifact_id: ProjectText | None = None

    @model_validator(mode="after")
    def version_binding(self) -> ComicImageImportRequest:
        if bool(self.image_id) != (self.expected_image_version is not None):
            raise ValueError("追加图片版本必须指定当前图片身份与版本")
        if self.image_id and (self.parent is None or self.parent.image_id != self.image_id):
            raise ValueError("追加图片版本必须绑定该图片的父版本")
        return self


class ComicImageSelectionRequest(ComicMediaMutation):
    version: int = Field(ge=1)
    expected_selection_version: int = Field(ge=0)


class ReferenceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    image: ImageVersionRef
    roles: list[Literal["identity", "style", "composition"]] = Field(min_length=1, max_length=3)
    target: BriefItem

    @model_validator(mode="after")
    def unique_roles(self) -> ReferenceBinding:
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("参考用途不能重复")
        return self


class ComicReferenceRequest(ComicMediaMutation):
    expected_reference_version: int = Field(ge=0)
    bindings: list[ReferenceBinding] = Field(max_length=8)

    @model_validator(mode="after")
    def unique_bindings(self) -> ComicReferenceRequest:
        keys = [(item.image.image_id, item.image.version, item.target) for item in self.bindings]
        if len(keys) != len(set(keys)):
            raise ValueError("同一图片版本和对象的用途请合并到一条绑定")
        return self


class ImageFinding(BaseModel):
    """模型公开观察；发现项数量和题材均由实际图像决定。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    finding_id: ProjectText
    observation: BriefItem
    suggested_change: BriefItem
    confidence: float = Field(ge=0, le=1)
    uncertainty: str = Field(default="", max_length=500)
    region: list[float] | None = Field(default=None, min_length=4, max_length=4,
        description="按显示方向，以图片左上为原点的归一化 [x1,y1,x2,y2]；"
                    "无法可靠定位或整图建议返回 null，不猜测位置。")

    @model_validator(mode="after")
    def normalized_region(self) -> ImageFinding:
        if self.region is not None:
            x1, y1, x2, y2 = self.region
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                raise ValueError("区域必须为归一化的左上/右下坐标")
        return self


class ImageAnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: BriefItem
    findings: list[ImageFinding] = Field(max_length=50)
    protected_features: list[BriefItem] = Field(max_length=50)
    limitations: list[BriefItem] = Field(max_length=20)

    @model_validator(mode="after")
    def unique_findings(self) -> ImageAnalysisOutput:
        ids = [item.finding_id for item in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("发现项身份不能重复")
        return self


class ImageEditDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    positive_prompt: str = Field(min_length=1, max_length=16000)
    negative_prompt: str = Field(default="", max_length=4000)
    summary: BriefItem


class ImageComparisonCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    check_id: ProjectText
    status: Literal["satisfied", "not_satisfied", "uncertain"]
    evidence: BriefItem
    confidence: float = Field(ge=0, le=1)


class ImageComparisonOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: BriefItem
    changes: list[ImageComparisonCheck] = Field(max_length=101)
    preservation: list[ImageComparisonCheck] = Field(max_length=100)
    other_observations: list[BriefItem] = Field(max_length=20)
    limitations: list[BriefItem] = Field(max_length=20)


class ComicImageAnalysisRequest(ComicMediaMutation):
    image: ImageVersionRef
    task: str = Field(min_length=1, max_length=4000)


class ImageEditRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    region_id: ProjectText
    label: str = Field(default="", max_length=80)
    bounds: list[float] = Field(min_length=4, max_length=4)
    finding_id: ProjectText | None = None
    instruction: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def valid_bounds(self) -> ImageEditRegion:
        x1, y1, x2, y2 = self.bounds
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError("选区必须位于原图范围内")
        return self


class ImageInstructionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["whole", "selected_regions", "region"]
    region_id: ProjectText | None = None

    @model_serializer(mode="wrap")
    def serialize_target(self, handler: Any) -> dict:
        result = handler(self)
        if self.mode != "region":
            result.pop("region_id", None)
        return result

    @model_validator(mode="after")
    def valid_target(self) -> ImageInstructionTarget:
        if (self.mode == "region") != bool(self.region_id):
            raise ValueError("指定选区需要区域身份，其他范围不提交区域身份")
        return self


class ImageEditScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["whole", "regions", "mixed"]
    regions: list[ImageEditRegion] = Field(default_factory=list, max_length=50)
    whole_finding_ids: list[ProjectText] = Field(default_factory=list, max_length=50)
    instruction_target: ImageInstructionTarget | None = None

    @model_serializer(mode="wrap")
    def serialize_scope(self, handler: Any) -> dict:
        """Unset additions do not change legacy request or confirmation fingerprints."""
        result = handler(self)
        for name in ("whole_finding_ids", "instruction_target"):
            if name not in self.model_fields_set:
                result.pop(name)
        return result

    def frozen_scope(self) -> dict:
        return self.model_dump(mode="json")

    @model_validator(mode="after")
    def valid_regions(self) -> ImageEditScope:
        if self.mode == "whole" and self.regions:
            raise ValueError("整图模式不提交局部选区")
        if (self.mode == "regions" and not self.regions
                and self.instruction_target is None):
            raise ValueError("请先选择修改区域")
        if self.mode == "regions" and self.whole_finding_ids:
            raise ValueError("局部模式不能授权整图建议")
        if len(self.whole_finding_ids) != len(set(self.whole_finding_ids)):
            raise ValueError("整图建议身份不能重复")
        ids = [item.region_id for item in self.regions]
        if len(ids) != len(set(ids)):
            raise ValueError("选区身份不能重复")
        return self


class ComicImageEditRequest(ComicImageAnalysisRequest):
    analysis_artifact_id: ProjectText | None = None
    selected_finding_ids: list[ProjectText] = Field(default_factory=list, max_length=50)
    instruction: str = Field(default="", max_length=4000)
    edit_scope: ImageEditScope | None = None
    round_id: ProjectText | None = None

    @model_validator(mode="after")
    def explicit_changes(self) -> ComicImageEditRequest:
        if len(self.selected_finding_ids) != len(set(self.selected_finding_ids)):
            raise ValueError("选中发现项不能重复")
        if self.selected_finding_ids and not self.analysis_artifact_id:
            raise ValueError("使用图片建议必须提供分析报告")
        if not self.analysis_artifact_id and self.edit_scope is None:
            raise ValueError("人工改图请明确整图或局部修改范围")
        if not self.selected_finding_ids and not self.instruction.strip() and not (
            self.edit_scope and any(item.instruction.strip() for item in self.edit_scope.regions)
        ):
            raise ValueError("请选择发现项或输入修改要求")
        return self


class ComicImageWorkbenchWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    conversation_id: ProjectText
    request_id: ProjectText
    expected_version: int = Field(ge=0)
    image: ImageVersionRef | None = None
    edit_image: ImageVersionRef | None = None
    auto_analyze: bool = True


class ComicImageDraftWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    conversation_id: ProjectText
    request_id: ProjectText
    expected_version: int = Field(ge=0)
    image: ImageVersionRef
    round_id: ProjectText
    task: str = Field(min_length=1, max_length=4000)
    analysis_artifact_id: ProjectText | None = None
    selected_finding_ids: list[ProjectText] = Field(default_factory=list, max_length=50)
    dismissed_finding_region_ids: list[ProjectText] = Field(default_factory=list, max_length=50)
    edit_scope: ImageEditScope | None = None
    instruction: str = Field(default="", max_length=4000)
    instruction_artifact_id: ProjectText | None = None
    phase: Literal["editing", "review"] = "editing"

    @model_serializer(mode="wrap")
    def serialize_draft(self, handler: Any) -> dict:
        result = handler(self)
        if "dismissed_finding_region_ids" not in self.model_fields_set:
            result.pop("dismissed_finding_region_ids", None)
        return result


class ComicImageRestoreRequest(ComicMediaMutation):
    image: ImageVersionRef
    filename: ProjectText
    data_url: str = Field(min_length=1, max_length=7_000_000)


class ComicImageEditConfirmRequest(ComicMediaMutation):
    instruction_artifact_id: ProjectText


class ComicImageCompareRequest(ComicMediaMutation):
    before: ImageVersionRef
    after: ImageVersionRef
    instruction_artifact_id: ProjectText


class ComicStoryShot(BaseModel):
    """故事镜头固定图片内容版本；未填制作文字可作为草稿保存。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    shot_id: ProjectText
    image: ImageVersionRef
    action: str = Field(default="", max_length=4000)
    camera: str = Field(default="", max_length=2000)
    # 草稿允许保存未完成的时长；范围错误由故事校验定位到具体镜头，确认仍严格阻断。
    duration_seconds: float | None = Field(default=None, allow_inf_nan=False)
    transition: str = Field(default="", max_length=1000)


class ComicStoryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="", max_length=200)
    summary: str = Field(default="", max_length=8000)
    instruction: str = Field(default="", max_length=4000)
    shots: list[ComicStoryShot] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_shots(self) -> ComicStoryDraft:
        if len({shot.shot_id for shot in self.shots}) != len(self.shots):
            raise ValueError("故事镜头 ID 不能重复")
        if len({(shot.image.image_id, shot.image.version) for shot in self.shots}) > 8:
            raise ValueError("一个故事最多固定 8 个图片版本")
        return self


class ComicStoryMutation(ComicMediaMutation):
    anchor_project_id: ProjectText
    expected_story_version: int = Field(ge=0)


class ComicStorySaveRequest(ComicStoryMutation):
    draft: ComicStoryDraft


class ComicStoryGenerateRequest(ComicStoryMutation):
    """整理初稿：可以用本轮要求，也可以直接用已填写的标题/概述/镜头作为框架。"""

    instruction: str = Field(default="", max_length=4000)
    images: list[ImageVersionRef] = Field(min_length=1, max_length=8)
    draft: ComicStoryDraft | None = None

    @model_validator(mode="after")
    def unique_images(self) -> ComicStoryGenerateRequest:
        if not self.instruction.strip() and not self._has_framework():
            raise ValueError("请填写故事要求，或先填写故事标题、概述与镜头内容")
        if len({(image.image_id, image.version) for image in self.images}) != len(self.images):
            raise ValueError("候选图片版本不能重复")
        return self

    def _has_framework(self) -> bool:
        if not self.draft:
            return False
        return bool(self.draft.title.strip() or self.draft.summary.strip() or self.draft.shots)


class ComicStoryConfirmRequest(ComicStoryMutation):
    accept_warnings: bool = False


class ComicStoryRestoreRequest(ComicStoryMutation):
    version: int = Field(ge=1)


class ComicStoryAdoptCandidateRequest(ComicStoryMutation):
    candidate_run_id: ProjectText


STORY_FINDING_CODES = frozenset({
    "missing_title", "missing_summary", "missing_transition",
    "missing_shots", "missing_action", "missing_camera", "missing_duration",
    "duration_conflict", "camera_conflict", "transition_conflict", "image_conflict",
    "requirement_conflict", "continuity", "other",
})


class ComicStoryFinding(BaseModel):
    """核验发现：必须能指到具体镜头与字段，并给出可执行的修改建议。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    severity: Literal["warning", "error"]
    shot_id: ProjectText | None = None
    field: Literal[
        "title", "summary", "instruction", "action", "camera", "duration_seconds",
        "transition", "image",
    ]
    message: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=4000)
    code: str = Field(default="other", max_length=64)
    suggestion: str = Field(default="", max_length=2000)

    @field_validator("code", mode="before")
    @classmethod
    def known_code(cls, value: object) -> str:
        """未知类型归入 other：类型是给人看的标签，不能因为它让整次核验失败。"""
        return value if isinstance(value, str) and value in STORY_FINDING_CODES else "other"


class ComicStoryReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["pass", "warning", "needs_revision"]
    findings: list[ComicStoryFinding] = Field(default_factory=list, max_length=100)
