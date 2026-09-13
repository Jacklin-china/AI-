"""工具注册表契约测试。"""

from __future__ import annotations

from typing import Any

import pytest

from kantoku.agent import tools_registry
from kantoku.config import ToolError


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试使用独立注册表，避免污染生产工具。"""
    monkeypatch.setattr(tools_registry, "TOOLS", {})
    monkeypatch.setattr(tools_registry, "_SCHEMAS", {})


def test_register_get_and_schema_are_consistent() -> None:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"script": {"type": "string"}},
        "required": ["script"],
    }

    @tools_registry.register("storyboard", description="生成分镜", parameters=parameters)
    def storyboard(script: str) -> str:
        return script

    parameters["properties"]["script"]["type"] = "number"
    exposed = tools_registry.to_json_schema()

    assert tools_registry.get("storyboard") is storyboard
    assert exposed[0]["function"]["parameters"]["properties"]["script"]["type"] == "string"

    exposed[0]["function"]["name"] = "tampered"
    assert tools_registry.to_json_schema()[0]["function"]["name"] == "storyboard"


def test_register_rejects_duplicate_name() -> None:
    @tools_registry.register("same")
    def first() -> str:
        """第一个工具。"""
        return "first"

    with pytest.raises(ToolError, match="工具重复注册"):

        @tools_registry.register("same")
        def second() -> str:
            """第二个工具。"""
            return "second"


@pytest.mark.parametrize(
    "parameters",
    [
        {"type": "array", "properties": {}},
        {"type": "object"},
        {"type": "object", "properties": {}, "required": "script"},
    ],
)
def test_register_rejects_invalid_parameter_schema(parameters: dict[str, Any]) -> None:
    with pytest.raises(ToolError, match="工具参数 Schema 无效"):
        tools_registry.register("invalid", parameters=parameters)


def test_get_rejects_unknown_tool_without_key_error() -> None:
    with pytest.raises(ToolError, match="模型请求了未知工具") as caught:
        tools_registry.get("missing")

    assert caught.value.detail == "missing"
