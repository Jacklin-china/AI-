"""Comic Domain 的状态；Core 不导入本模块。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

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
