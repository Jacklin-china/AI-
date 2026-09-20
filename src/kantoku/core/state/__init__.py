"""跨领域 State 基础契约。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RunState(BaseModel):
    """Domain State 的通用基类；Node 只能返回局部字段更新。"""

    model_config = ConfigDict(extra="forbid")


__all__ = ["RunState"]
