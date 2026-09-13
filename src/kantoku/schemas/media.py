"""生图结果契约，区分生成状态与账单是否已确认。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ImageGenerationStatus = Literal["succeeded", "failed", "unknown"]


class ImageGenerationResult(BaseModel):
    """供应商查询结果；actual_fen 为空表示仍需对账。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: Path | None = None
    provider_job_id: NonBlank | None = None
    status: ImageGenerationStatus
    actual_fen: int | None = Field(default=None, ge=0)
    error: NonBlank | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status == "succeeded" and (
            self.path is None or self.provider_job_id is None
        ):
            raise ValueError("成功结果必须包含文件路径和供应商任务 ID")
        if self.status == "failed" and self.path is not None:
            raise ValueError("失败结果不能包含文件路径")
        if self.status == "unknown" and (
            self.path is not None or self.actual_fen is not None
        ):
            raise ValueError("未知结果不能声称已有文件或实际费用")
        return self
