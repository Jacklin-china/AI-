"""Comic 领域的真实 Skill 示例。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kantoku.core.skills import Skill, SkillMetadata
from kantoku.tools.archive import archive_reviewed_image
from kantoku.tools.studio import compose_prompt


def _compose(inputs: Mapping[str, Any], _context: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"prompt": compose_prompt(
        str(inputs["subject"]), str(inputs["purpose"]),
        str(inputs["audience"]), str(inputs["style"]),
    )}


def _archive(inputs: Mapping[str, Any], _context: Mapping[str, Any]) -> Mapping[str, Any]:
    result = archive_reviewed_image(str(inputs["request_id"]))
    return {"image_path": str(result.image_path), "metadata_path": str(result.metadata_path)}


def comic_skills() -> tuple[Skill, ...]:
    """导出可由 SkillRegistry 加载的真实 Skill。"""
    return (
        Skill(
            SkillMetadata(
                id="comic.compose_prompt", name="Compose Comic Prompt", domain="comic",
                description="复用 Studio 规则，把创作简报整理为可编辑提示词。", version="1.0.0",
                required_tools=("compose_prompt",),
                input_schema={"required": ["subject", "purpose", "audience", "style"]},
                output_schema={"required": ["prompt"]},
            ),
            _compose,
        ),
        Skill(
            SkillMetadata(
                id="comic.archive_image", name="Archive Reviewed Image", domain="comic",
                description="复用不可变人工终审与图片归档能力。", version="1.0.0",
                required_tools=("archive_reviewed_image",),
                input_schema={"required": ["request_id"]},
                output_schema={"required": ["image_path", "metadata_path"]},
            ),
            _archive,
        ),
    )

