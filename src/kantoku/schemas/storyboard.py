"""分镜输入输出契约：拒绝无效镜头，并安全报告模型返回格式错误。"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from kantoku.config import SchemaError

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Shot(BaseModel):
    """一个可交给后续生产模块的镜头；人物为空表示空镜。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    shot_no: int = Field(gt=0, description="从 1 开始连续排列的镜号")
    desc: NonBlank = Field(description="只描述这一镜可见的场景、人物动作和关键物件")
    dialogue: str = Field(default="", description="这一镜的台词或旁白；没有时为空字符串")
    camera: NonBlank = Field(description="景别、机位或运镜方式")
    duration_s: int = Field(gt=0, description="镜头时长，单位为整数秒")
    characters: list[NonBlank] = Field(
        description="本镜出现或发声的角色原名，包含动物、AI 等非人角色；空镜用空数组"
    )


class Storyboard(BaseModel):
    """按制作顺序排列的一集分镜，镜号从 1 连续编号。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    episode: NonBlank = Field(description="简短且可辨识的剧集标题")
    shots: list[Shot] = Field(min_length=1, description="按制作顺序排列的全部镜头")

    @model_validator(mode="after")
    def check_shot_order(self) -> Self:
        """避免重复、漏号或乱序导致下游素材被错误关联。"""
        if [shot.shot_no for shot in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("镜号必须从 1 开始连续排列")
        return self


def parse_storyboard(raw: str) -> Storyboard:
    """校验完整 JSON；原文保留在异常属性，不输出到日志。"""
    try:
        return Storyboard.model_validate_json(raw)
    except ValidationError as error:
        detail = "; ".join(
            f"{'.'.join(map(str, item['loc'])) or 'root'}: {item['type']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
        raise SchemaError("分镜 JSON 校验失败", detail=detail, raw=raw) from None


# 仅用于展示结构的虚构样例，不代表真实订单或模型质量验收数据。
EXAMPLE_SHOT_JSON = Storyboard(
    episode="教学样例：雨夜空街",
    shots=[Shot(shot_no=1, desc="雨落在空街上", camera="固定远景", duration_s=3, characters=[])],
).model_dump_json(indent=2)
