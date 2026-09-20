"""文件化 Comic Prompt Skill。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kantoku.tools.studio import compose_prompt


def execute(inputs: Mapping[str, Any], _context: Mapping[str, Any]) -> Mapping[str, Any]:
    """整理创作需求。"""
    return {"prompt": compose_prompt(
        str(inputs["subject"]), str(inputs["purpose"]),
        str(inputs["audience"]), str(inputs["style"]),
    )}
