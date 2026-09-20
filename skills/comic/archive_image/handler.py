"""文件化图片归档 Skill。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kantoku.tools.archive import archive_reviewed_image


def execute(inputs: Mapping[str, Any], _context: Mapping[str, Any]) -> Mapping[str, Any]:
    """归档已经人工终审的图片。"""
    result = archive_reviewed_image(str(inputs["request_id"]))
    return {"image_path": str(result.image_path), "metadata_path": str(result.metadata_path)}
