"""通用视频生成契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class VideoGenerationRequest(BaseModel):
    """由图片 Artifact 派生视频的稳定请求。"""

    model_config = ConfigDict(extra="forbid")
    request_id: str
    run_id: str
    node_id: str
    image_artifact_id: str
    prompt: str
    project: str
    shot_no: int = Field(gt=0)
    config: dict[str, Any] = Field(default_factory=dict)


class VideoGenerationResult(BaseModel):
    """Provider 结果；Mock 永远显式标记。"""

    model_config = ConfigDict(extra="forbid")
    status: Literal["succeeded", "failed", "unknown", "rejected", "blocked"]
    location: str | None = None
    provider_job_id: str | None = None
    actual_fen: int | None = Field(default=None, ge=0)
    mock: bool = False
    error: str | None = None
