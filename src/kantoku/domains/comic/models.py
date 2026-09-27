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


class DirectorSpecDraft(BaseModel):
    """可编辑的导演决策，不包含供应商 Prompt 或生成参数。"""

    model_config = ConfigDict(extra="forbid", strict=True)

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
