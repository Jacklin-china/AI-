"""Commerce Domain State、Artifact Schema 与端口数据契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kantoku.core.state import RunState


class CommerceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Requirement(CommerceModel):
    query: str
    locale: Literal["zh-CN", "ru-RU"]
    revision_instruction: str | None = None


class Candidate(CommerceModel):
    id: str
    title: str
    cost_fen: int = Field(ge=0)
    skus: list[str] = Field(min_length=1)
    source: str
    source_external_id: str | None = None
    source_url: str | None = None
    captured_at: str | None = None
    supplier: str | None = None
    currency: str = "CNY"
    raw_snapshot_hash: str | None = None
    mock: bool = True


class CandidateList(CommerceModel):
    items: list[Candidate]
    query: str
    revision_instruction: str | None = None


class CandidateAnalysis(CommerceModel):
    recommended_id: str
    candidate_count: int = Field(ge=0)
    basis: str
    revision_instruction: str | None = None
    mock: bool = True


class SkuSelection(CommerceModel):
    candidate_id: str
    sku: str
    mock: bool = True


class PricingResult(CommerceModel):
    cost_fen: int = Field(ge=0)
    price_fen: int = Field(ge=0)
    currency: str
    margin_rate: float = Field(ge=0, lt=1)
    marketplace_fee_rate: float = Field(ge=0, lt=1)
    mock: bool = True


class ListingDraft(CommerceModel):
    title: str
    description: str
    sku: str
    price_fen: int = Field(ge=0)
    currency: str = "CNY"
    locale: str = "zh-CN"
    revision: int = Field(default=0, ge=0)
    revision_instruction: str | None = None
    mock: bool = True


class LocalizedListing(ListingDraft):
    locale: str


class ProductImageBrief(CommerceModel):
    product_name: str
    sku: str
    marketplace: str
    locale: str
    image_purpose: str
    background: str
    composition: str
    constraints: list[str]
    revision: int = Field(default=0, ge=0)
    revision_instruction: str | None = None


class ProductImageResult(CommerceModel):
    status: Literal["succeeded", "mock", "blocked", "failed", "unknown"]
    origin: Literal["real", "mock", "blocked"]
    location: str | None = None
    provider: str
    model: str
    prompt: str
    brief: ProductImageBrief
    actual_fen: int | None = Field(default=None, ge=0)
    message: str | None = None


class QcReport(CommerceModel):
    passed: bool
    checks: list[str]
    asset_artifact_id: str | None = None
    rework_count: int = Field(ge=0)
    mock: bool = True


class MarketplaceDraft(CommerceModel):
    id: str
    listing: dict[str, Any]
    status: Literal["draft", "submitted"]
    mock: Literal[True] = True


class PublishResult(CommerceModel):
    draft_id: str
    status: str
    marketplace: str
    mock: bool = True


class CommerceState(RunState):
    """Commerce Pack v0.1 的完整可恢复状态。"""

    requirement: str
    data_mode: Literal["demo", "production"] = "demo"
    locale: Literal["zh-CN", "ru-RU"] = "zh-CN"
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    analysis: dict[str, Any] | None = None
    candidate_decision: str | None = None
    candidate_revision: int = Field(default=0, ge=0)
    candidate_revision_instruction: str | None = None
    selected_candidate_id: str | None = None
    selected_sku: str | None = None
    pricing: dict[str, Any] | None = None
    listing: dict[str, Any] | None = None
    localized_listing: dict[str, Any] | None = None
    asset_artifact_id: str | None = None
    product_image: dict[str, Any] | None = None
    qc_result: dict[str, Any] | None = None
    qc_passed: bool | None = None
    publish_decision: str | None = None
    publish_revision: int = Field(default=0, ge=0)
    publish_revision_instruction: str | None = None
    rework_count: int = Field(default=0, ge=0)
    max_reworks: int = Field(default=1, ge=0, le=5)
    marketplace_draft: dict[str, Any] | None = None
    mock: bool = True
