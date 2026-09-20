"""确定性的 Commerce Pricing Skill。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kantoku.config import ToolError


def execute(inputs: Mapping[str, Any], _context: Mapping[str, Any]) -> Mapping[str, Any]:
    """用整数分计算售价，避免浮点金额入账。"""
    cost_fen = int(inputs["cost_fen"])
    margin = float(inputs["margin_rate"])
    fee = float(inputs["marketplace_fee_rate"])
    denominator = 1 - margin - fee
    if cost_fen < 0 or denominator <= 0:
        raise ToolError("Pricing 参数不合法")
    return {"price_fen": round(cost_fen / denominator), "currency": "CNY"}
