"""构建可审计的 Commerce Product Image Brief。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def execute(inputs: Mapping[str, Any], _context: Mapping[str, Any]) -> Mapping[str, Any]:
    """只做领域 Prompt 编排；真正生图交给 Core Image Capability。"""
    brief = {
        "product_name": str(inputs["product_name"]),
        "sku": str(inputs["sku"]),
        "marketplace": str(inputs["marketplace"]),
        "locale": str(inputs["locale"]),
        "image_purpose": "marketplace_main_image",
        "background": "neutral light gray seamless studio background",
        "composition": "single product, centered, three-quarter view, generous safe margin",
        "constraints": [
            "preserve product facts and physical structure",
            "no text, logo, watermark, badge, border or invented accessory",
            "commercial product photography, realistic materials and soft shadow",
        ],
        "revision": int(inputs["revision"]),
        "revision_instruction": (
            str(inputs["revision_instruction"]).strip()
            if inputs.get("revision_instruction") else None
        ),
    }
    prompt = (
        f"Professional marketplace product photography of {brief['product_name']} "
        f"(SKU {brief['sku']}). {brief['composition']}; {brief['background']}. "
        + "; ".join(brief["constraints"])
        + f". Target marketplace: {brief['marketplace']}; locale: {brief['locale']}; "
        f"revision: {brief['revision']}."
    )
    if brief["revision_instruction"]:
        prompt += f" Human revision instruction: {brief['revision_instruction']}."
    return {"brief": brief, "prompt": prompt}
