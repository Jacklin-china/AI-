"""Function Calling 工具注册表：保存可调用函数及其公开 JSON Schema。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, TypeAlias

from kantoku.config import ToolError

ToolFunction: TypeAlias = Callable[..., Any]

TOOLS: dict[str, ToolFunction] = {}
_SCHEMAS: dict[str, dict[str, Any]] = {}


def _validated_parameters(parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    """复制并校验工具参数的顶层结构，防止注册后被调用方篡改。"""
    if parameters is None:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    schema = deepcopy(dict(parameters))
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise ToolError("工具参数 Schema 无效", detail="必须是带 properties 的 object")

    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ToolError("工具参数 Schema 无效", detail="required 必须是字符串列表")
    schema.setdefault("additionalProperties", False)
    return schema


def register(
    name: str,
    *,
    description: str | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> Callable[[ToolFunction], ToolFunction]:
    """把函数登记为模型可见工具；Schema 由调用处显式编写。"""
    if not name or name != name.strip():
        raise ToolError("工具名无效", detail="名称不能为空或包含首尾空白")
    schema = _validated_parameters(parameters)

    def decorator(function: ToolFunction) -> ToolFunction:
        if name in TOOLS:
            raise ToolError("工具重复注册", detail=name)
        tool_description = (description or function.__doc__ or "").strip()
        if not tool_description:
            raise ToolError("工具描述不能为空", detail=name)

        TOOLS[name] = function
        _SCHEMAS[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": tool_description,
                "parameters": schema,
            },
        }
        return function

    return decorator


def to_json_schema() -> list[dict[str, Any]]:
    """按注册顺序返回 OpenAI 兼容的 tools 参数，并隔离外部修改。"""
    return [deepcopy(_SCHEMAS[name]) for name in TOOLS]


def get(name: str) -> ToolFunction:
    """取得已注册工具；未知名称转换为项目异常。"""
    try:
        return TOOLS[name]
    except KeyError:
        raise ToolError("模型请求了未知工具", detail=name) from None
