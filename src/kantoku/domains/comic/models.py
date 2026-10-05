"""Comic Domain 的状态；Core 不导入本模块。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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


class CreativeBriefInput(BaseModel):
    """用户明确提供的创意边界；空列表不会被猜测成模型已理解的内容。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    original_request: BriefText
    hard_constraints: list[BriefItem] = Field(default_factory=list, max_length=50)
    soft_preferences: list[BriefItem] = Field(default_factory=list, max_length=50)
    creative_freedom: list[BriefItem] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_items(self) -> CreativeBriefInput:
        for name in ("hard_constraints", "soft_preferences", "creative_freedom"):
            items = getattr(self, name)
            if len(items) != len(set(items)):
                raise ValueError(f"{name} 不允许重复")
        return self


class ComicProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: ProjectText
    description: str = Field(default="", max_length=2000)
    brief: CreativeBriefInput | None = None


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
    character_expression: BriefText | None = None
    character_pose: BriefText | None = None
    character_presence: BriefText | None = None
    continuity_rules: list[BriefItem] = Field(default_factory=list, max_length=50)
    creative_choices: list[BriefItem] = Field(default_factory=list, max_length=20)
    risk_flags: list[BriefItem] = Field(default_factory=list, max_length=20)


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
        if self.status == "needs_revision" and not self.missing_fields:
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


class DirectorSpecDraft(BaseModel):
    """可编辑的导演决策，不包含供应商 Prompt 或生成参数。

    ``schema_version=1`` 表示现有扁平 DirectorSpec；
    ``schema_version=2`` 才允许保存分层导演决策。这样旧调用方可以继续写入
    v1，而 Phase 7.2 Coordinator 准备好完整结构后再显式写入 v2。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1, 2] = 1
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
    asset_versions: dict[str, int] = Field(default_factory=dict, max_length=50)
    storyboard_version: int | None = Field(default=None, ge=1)
    shot_version: int | None = Field(default=None, ge=1)

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

class DirectorSpecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
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

    @model_validator(mode="after")
    def validate_creation_mode(self) -> DirectorSpecRequest:
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


class ComicShotDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class ComicPromptDraft(BaseModel):
    """编译结果；内容可编辑，但不能伪装成已经调用生图模型。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    director_summary: BriefText
    positive_prompt: BriefText
    negative_prompt: str = Field(default="", max_length=4000)


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


class ComicPromptEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_project_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    draft: ComicPromptDraft
