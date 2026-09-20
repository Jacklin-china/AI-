"""Commerce Domain State 与端口数据契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kantoku.core.state import RunState


class CommerceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(CommerceModel):
    id: str
    title: str
    cost_fen: int = Field(ge=0)
    skus: list[str] = Field(min_length=1)
    source: str
    mock: Literal[True] = True


class MarketplaceDraft(CommerceModel):
    id: str
    listing: dict[str, Any]
    status: Literal["draft", "submitted"]
    mock: Literal[True] = True


class CommerceState(RunState):
    """Commerce Pack v0.1 的完整可恢复状态。"""

    requirement: str
    locale: Literal["zh-CN", "ru-RU"] = "zh-CN"
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    analysis: dict[str, Any] | None = None
    candidate_decision: str | None = None
    candidate_revision: int = Field(default=0, ge=0)
    selected_candidate_id: str | None = None
    selected_sku: str | None = None
    pricing: dict[str, Any] | None = None
    listing: dict[str, Any] | None = None
    localized_listing: dict[str, Any] | None = None
    asset_artifact_id: str | None = None
    qc_result: dict[str, Any] | None = None
    qc_passed: bool | None = None
    publish_decision: str | None = None
    publish_revision: int = Field(default=0, ge=0)
    rework_count: int = Field(default=0, ge=0)
    max_reworks: int = Field(default=1, ge=0, le=5)
    marketplace_draft: dict[str, Any] | None = None
    mock: Literal[True] = True
