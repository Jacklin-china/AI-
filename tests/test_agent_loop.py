"""Agent 工具循环的成功、恢复、终止和埋点测试。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from openai.types.chat import ChatCompletionMessage

from kantoku.agent import loop, tools_registry
from kantoku.config import LLMError, ToolError


def _tool_call(name: str = "echo", arguments: str = '{"text":"你好"}') -> ChatCompletionMessage:
    return ChatCompletionMessage.model_validate(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            ],
        }
    )


@pytest.fixture(autouse=True)
def isolated_loop(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(tools_registry, "TOOLS", {})
    monkeypatch.setattr(tools_registry, "_SCHEMAS", {})
    trace_writer = MagicMock()
    monkeypatch.setattr(loop, "write_trace", trace_writer)
    return trace_writer


def _register_echo() -> None:
    @tools_registry.register(
        "echo",
        description="回显文本",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    def echo(text: str) -> dict[str, str]:
        return {"echo": text}


def test_direct_answer_finishes_without_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    chat_mock = MagicMock(return_value=ChatCompletionMessage(role="assistant", content="直接回答"))
    monkeypatch.setattr(loop, "chat", chat_mock)

    assert loop.run("普通问题") == "直接回答"
    assert chat_mock.call_count == 1
    assert chat_mock.call_args.kwargs["tools"] == []


def test_tool_call_is_executed_and_fed_back_with_matching_id(
    monkeypatch: pytest.MonkeyPatch,
    isolated_loop: MagicMock,
) -> None:
    _register_echo()
    chat_mock = MagicMock(
        side_effect=[_tool_call(), ChatCompletionMessage(role="assistant", content="已完成")]
    )
    monkeypatch.setattr(loop, "chat", chat_mock)

    assert loop.run("请回显") == "已完成"

    second_messages = chat_mock.call_args_list[1].args[0]
    assert second_messages[-2]["role"] == "assistant"
    assert second_messages[-2]["tool_calls"][0]["id"] == "call-1"
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_call_id"] == "call-1"
    payload = json.loads(second_messages[-1]["content"])
    assert payload == {"ok": True, "result": {"echo": "你好"}}
    assert isolated_loop.call_args.args[0].kind == "tool.echo"
    assert isolated_loop.call_args.args[0].ok is True


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("echo", "not-json", "工具参数不是合法 JSON"),
        ("", "{}", "模型请求了未知工具"),
        ("echo", "[]", "工具参数必须是 JSON 对象"),
        ("echo", "{}", "工具参数与函数签名不匹配"),
    ],
)
def test_bad_tool_call_becomes_recoverable_tool_message(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: str,
    expected: str,
) -> None:
    _register_echo()
    chat_mock = MagicMock(
        side_effect=[
            _tool_call(name=name, arguments=arguments),
            ChatCompletionMessage(role="assistant", content="已说明失败"),
        ]
    )
    monkeypatch.setattr(loop, "chat", chat_mock)

    assert loop.run("错误调用") == "已说明失败"
    payload = json.loads(chat_mock.call_args_list[1].args[0][-1]["content"])
    assert payload["ok"] is False
    assert payload["error"]["message"] == expected


def test_unexpected_tool_error_is_hidden_behind_safe_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @tools_registry.register("explode")
    def explode() -> None:
        """触发内部错误。"""
        raise RuntimeError("private value")

    chat_mock = MagicMock(
        side_effect=[
            _tool_call(name="explode", arguments="{}"),
            ChatCompletionMessage(role="assistant", content="执行失败"),
        ]
    )
    monkeypatch.setattr(loop, "chat", chat_mock)

    assert loop.run("执行") == "执行失败"
    content = chat_mock.call_args_list[1].args[0][-1]["content"]
    assert "工具执行失败" in content
    assert "private value" not in content


def test_trace_failure_does_not_repeat_tool(
    monkeypatch: pytest.MonkeyPatch,
    isolated_loop: MagicMock,
) -> None:
    _register_echo()
    isolated_loop.side_effect = RuntimeError("trace down")
    chat_mock = MagicMock(
        side_effect=[_tool_call(), ChatCompletionMessage(role="assistant", content="完成")]
    )
    monkeypatch.setattr(loop, "chat", chat_mock)

    assert loop.run("请回显") == "完成"
    assert chat_mock.call_count == 2


def test_max_steps_stops_repeated_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _register_echo()
    monkeypatch.setattr(loop, "chat", MagicMock(return_value=_tool_call()))

    with pytest.raises(LLMError, match="超出最大工具调用步数"):
        loop.run("一直调用", max_steps=2)


@pytest.mark.parametrize(("message", "max_steps"), [("", 1), ("ok", 0), ("ok", True)])
def test_run_rejects_invalid_limits(message: str, max_steps: int) -> None:
    with pytest.raises(ToolError):
        loop.run(message, max_steps=max_steps)
