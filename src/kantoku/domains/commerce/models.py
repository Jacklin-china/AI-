"""Commerce Mock Domain State。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from kantoku.core.state import RunState


class CommerceState(RunState):
    """用于验证 One Core / Multiple Domains 的最小 Mock 状态。"""

    requirement: str
    research: list[dict[str, Any]] = Field(default_factory=list)
    supplier: dict[str, Any] | None = None
    approval_decision: str | None = None
    product_image: str | None = None
    qc_result: dict[str, Any] | None = None
    mock: bool = True
