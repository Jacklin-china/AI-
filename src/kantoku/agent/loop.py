"""最小 Agent 循环：让模型选择工具，执行后把结果回喂给模型。"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast

from loguru import logger
from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageParam
from pydantic import BaseModel

from kantoku.agent import tools_registry
from kantoku.config import KantokuError, LLMError, ToolError
from kantoku.core.llm import chat
from kantoku.core.tracing import Trace, write_trace
from kantoku.tools import generate_storyboard as _generate_storyboard

SYSTEM = """你是监督酱，协助用户低成本完成 AI 图片、海报、宣传片、视频和漫剧制作。
用户需求模糊时，先澄清用途、受众、平台、核心信息、第一眼视觉重点、风格、预算与验收标准；
关键商业要求不明确时不得擅自替用户决定，也不得把主观偏好说成统一的大众审美。
当用户要求把剧本拆成分镜时，必须调用 generate_storyboard，不要自己伪造分镜数据。
工具成功后，把全部分镜整理成清晰的 Markdown 表格，不得遗漏镜号、描述、台词、运镜、时长和人物。
工具失败时，依据工具返回的安全错误调整参数；无法恢复就直说失败原因，不得假装成功。
普通咨询无需调用工具，直接简洁回答。
"""

# 显式引用保证内置工具模块完成注册，也让静态检查知道导入有意为之。
_BUILTIN_TOOL = _generate_storyboard


def _assistant_message(message: ChatCompletionMessage) -> ChatCompletionMessageParam:
    """保留工具调用 ID 和供应商推理字段，供下一轮请求正确关联。"""
    payload: dict[str, Any] = {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [call.model_dump(exclude_none=True) for call in message.tool_calls or []],
    }
    reasoning_content = getattr(message, "reasoning_content", None)
    if reasoning_content:
        payload["reasoning_content"] = reasoning_content
    return cast(ChatCompletionMessageParam, payload)


def _serialize_result(result: Any) -> str:
    """把工具结果转换成模型可消费的 JSON，不退回不稳定的 repr。"""
    if isinstance(result, BaseModel):
        result = result.model_dump(mode="json")
    try:
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ToolError("工具返回值无法序列化", detail=type(error).__name__) from error


def _safe_tool_error(error: ToolError) -> str:
    """回喂可恢复信息，但绝不包含 SchemaError.raw 等原始业务正文。"""
    payload = {
        "ok": False,
        "error": {
            "type": type(error).__name__,
            "message": error.message,
            "detail": error.detail,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def _write_tool_trace(*, name: str, started_at: float, error: ToolError | None) -> None:
    """尽力记录工具调用；埋点故障不能诱发重复执行有副作用的工具。"""
    try:
        write_trace(
            Trace(
                ts=datetime.now(UTC).isoformat(),
                kind=f"tool.{name}",
                model=None,
                in_tokens=0,
                out_tokens=0,
                latency_ms=max(0, round((perf_counter() - started_at) * 1000)),
                cost_fen=None,
                ok=error is None,
                shot_no=None,
                error=None if error is None else f"{type(error).__name__}:{error.message}",
                usage_reported=None,
            )
        )
    except Exception as trace_error:
        logger.error("工具 trace 写入失败：{}", type(trace_error).__name__)


def _invoke_tool(name: str, raw_arguments: str) -> str:
    """完成参数解析、签名校验、执行、序列化与工具级埋点。"""
    started_at = perf_counter()
    tool_error: ToolError | None = None
    try:
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            raise ToolError("工具参数不是合法 JSON", detail=type(error).__name__) from error
        if not isinstance(arguments, dict):
            raise ToolError("工具参数必须是 JSON 对象")

        function = tools_registry.get(name)
        try:
            inspect.signature(function).bind(**arguments)
        except TypeError as error:
            raise ToolError("工具参数与函数签名不匹配", detail=type(error).__name__) from error

        try:
            return _serialize_result(function(**arguments))
        except ToolError:
            raise
        except KantokuError as error:
            raise ToolError(error.message, detail=error.detail) from error
        except Exception as error:
            raise ToolError("工具执行失败", detail=type(error).__name__) from error
    except ToolError as error:
        tool_error = error
        return _safe_tool_error(error)
    finally:
        _write_tool_trace(name=name or "unknown", started_at=started_at, error=tool_error)


def run(user_msg: str, max_steps: int = 8) -> str:
    """运行有限步工具循环；达到上限时明确失败，避免费用失控。"""
    if not isinstance(user_msg, str) or not user_msg.strip():
        raise ToolError("用户消息不能为空")
    if type(max_steps) is not int or max_steps <= 0:
        raise ToolError("max_steps 必须是正整数")

    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_msg.strip()},
    ]
    for _ in range(max_steps):
        message = chat(messages, tools=tools_registry.to_json_schema())
        if not message.tool_calls:
            if message.content:
                return message.content
            if message.refusal:
                return message.refusal
            raise LLMError("模型没有返回可展示内容")

        messages.append(_assistant_message(message))
        for call in message.tool_calls:
            result = _invoke_tool(call.function.name, call.function.arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                }
            )

    raise LLMError("超出最大工具调用步数，可能陷入循环")
