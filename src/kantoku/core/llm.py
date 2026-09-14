"""LLM 调用的唯一出口：统一配置、超时、重试、降级与异常边界。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from time import perf_counter, sleep
from typing import Any, Literal

from loguru import logger
from openai import APIConnectionError, APIStatusError, OpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolUnionParam,
)
from openai.types.chat.completion_create_params import (
    CompletionCreateParamsNonStreaming,
    ResponseFormat,
)

from kantoku.config import ConfigError, LLMError, get_settings
from kantoku.core.tracing import Trace, write_trace


def _build_client(*, base_url: str, api_key: str, timeout_s: float) -> OpenAI:
    """创建单个供应商客户端，并关闭 SDK 自带重试。"""
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout_s,
        max_retries=0,
    )


def _request_once(
    client: OpenAI,
    *,
    messages: Sequence[ChatCompletionMessageParam],
    model: str,
    temperature: float | None,
    max_tokens: int,
    max_tokens_parameter: Literal["max_tokens", "max_completion_tokens"],
    response_format: ResponseFormat | None,
    tools: Sequence[ChatCompletionToolUnionParam] | None,
    extra_body: Mapping[str, Any] | None,
) -> ChatCompletion:
    """向一个供应商发送一次请求；本函数自身不重试。"""
    request: CompletionCreateParamsNonStreaming = {
        "messages": messages,
        "model": model,
    }
    if temperature is not None:
        request["temperature"] = temperature
    if max_tokens_parameter == "max_completion_tokens":
        request["max_completion_tokens"] = max_tokens
    else:
        request["max_tokens"] = max_tokens
    if response_format is not None:
        request["response_format"] = response_format
    if tools is not None:
        request["tools"] = tools

    started_at = perf_counter()
    in_tokens = out_tokens = 0
    usage_reported = False
    ok = False
    error_summary: str | None = None
    try:
        if extra_body:
            response = client.chat.completions.create(**request, extra_body=dict(extra_body))
        else:
            response = client.chat.completions.create(**request)
        in_tokens, out_tokens, usage_reported = _reported_usage(response)
        _validated_message(response)
        ok = True
        return response
    except (Exception, KeyboardInterrupt) as error:
        error_summary = _error_summary(error)
        raise
    finally:
        # 每次尝试都记录，重试后成功也不能抹掉前面的失败。
        _write_llm_trace(
            model=model,
            started_at=started_at,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            usage_reported=usage_reported,
            ok=ok,
            error=error_summary,
        )


def _reported_usage(response: ChatCompletion) -> tuple[int, int, bool]:
    """只认可完整、非负整数的用量，缺失或异常值不伪装成零消费。"""
    usage = response.usage
    if usage is not None:
        incoming, outgoing = usage.prompt_tokens, usage.completion_tokens
        if all(type(value) is int and value >= 0 for value in (incoming, outgoing)):
            return incoming, outgoing, True
    logger.warning("供应商未报告有效 usage；本次 token 用量未知，需对账")
    return 0, 0, False


def _validated_message(response: ChatCompletion) -> ChatCompletionMessage:
    """HTTP 成功并不代表有可消费的消息；先校验再记成功。"""
    if not response.choices:
        raise LLMError("供应商返回了空 choices")
    message = response.choices[0].message
    if not isinstance(message, ChatCompletionMessage):
        raise LLMError("供应商返回了无效 message")
    if not (message.content or message.tool_calls or message.refusal):
        raise LLMError("供应商未返回可消费的消息")
    return message


def _is_retryable(error: Exception) -> bool:
    """判断错误是否可能通过等待后再次请求而恢复。"""
    if isinstance(error, APIConnectionError):
        return True
    if isinstance(error, APIStatusError):
        return error.status_code in {408, 409, 429} or error.status_code >= 500
    return False


def _should_fallback(error: Exception) -> bool:
    """判断主供应商失败后是否值得尝试备用供应商。"""
    if _is_retryable(error):
        return True
    return isinstance(error, APIStatusError) and error.status_code == 404


def _error_summary(error: BaseException) -> str:
    """生成不包含密钥和提示词正文的错误摘要。"""
    if isinstance(error, APIStatusError):
        return f"{type(error).__name__}(status={error.status_code})"
    return type(error).__name__


def _request_with_retry(
    client: OpenAI,
    *,
    provider: str,
    retry: int,
    messages: Sequence[ChatCompletionMessageParam],
    model: str,
    temperature: float | None,
    max_tokens: int,
    max_tokens_parameter: Literal["max_tokens", "max_completion_tokens"],
    response_format: ResponseFormat | None,
    tools: Sequence[ChatCompletionToolUnionParam] | None,
    extra_body: Mapping[str, Any] | None,
) -> ChatCompletion:
    """执行一次请求，并仅对可恢复错误做指数退避重试。"""
    for attempt in range(retry + 1):
        try:
            return _request_once(
                client,
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                max_tokens_parameter=max_tokens_parameter,
                response_format=response_format,
                tools=tools,
                extra_body=extra_body,
            )
        except Exception as error:
            if not _is_retryable(error) or attempt == retry:
                raise

            retry_number = attempt + 1
            delay_s = float(2**retry_number)
            logger.warning(
                "{} 调用失败，{} 秒后进行第 {}/{} 次重试：{}",
                provider,
                delay_s,
                retry_number,
                retry,
                _error_summary(error),
            )
            sleep(delay_s)

    raise RuntimeError("重试循环未返回结果")


def _write_llm_trace(
    *,
    model: str,
    started_at: float,
    in_tokens: int,
    out_tokens: int,
    usage_reported: bool,
    ok: bool,
    error: str | None,
) -> None:
    """尽力记录 LLM 调用；埋点故障不能覆盖模型调用结果。"""
    latency_ms = max(0, round((perf_counter() - started_at) * 1000))
    try:
        trace = Trace(
            ts=datetime.now(UTC).isoformat(),
            kind="llm.chat.attempt",
            model=model,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            latency_ms=latency_ms,
            cost_fen=None,
            ok=ok,
            shot_no=None,
            error=error,
            usage_reported=usage_reported,
        )
        write_trace(trace)
    except Exception as trace_error:
        logger.error("LLM trace 写入失败：{}", type(trace_error).__name__)


def _call_provider(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    timeout_s: float,
    retry: int,
    messages: Sequence[ChatCompletionMessageParam],
    model: str,
    temperature: float | None,
    max_tokens: int,
    max_tokens_parameter: Literal["max_tokens", "max_completion_tokens"],
    response_format: ResponseFormat | None,
    tools: Sequence[ChatCompletionToolUnionParam] | None,
    extra_body: Mapping[str, Any] | None,
) -> ChatCompletionMessage:
    """创建一个供应商客户端，完成请求后释放连接资源。"""
    client: OpenAI | None = None
    try:
        client = _build_client(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
        response = _request_with_retry(
            client,
            provider=provider,
            retry=retry,
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            max_tokens_parameter=max_tokens_parameter,
            response_format=response_format,
            tools=tools,
            extra_body=extra_body,
        )
        return response.choices[0].message
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as close_error:
                # 释放资源失败不能把成功结果变成失败，诱发重复付费请求。
                logger.error("LLM 连接关闭失败：{}", type(close_error).__name__)


def chat(
    messages: Sequence[ChatCompletionMessageParam],
    *,
    model: str | None = None,
    temperature: float | None = None,
    response_format: ResponseFormat | None = None,
    tools: Sequence[ChatCompletionToolUnionParam] | None = None,
    timeout_s: float | None = None,
) -> ChatCompletionMessage:
    """调用主聊天模型；可恢复失败时自动重试并尝试备用模型。"""
    settings = get_settings()
    selected_model = model or settings.llm.model_chat
    selected_temperature = temperature if temperature is not None else settings.llm.temperature
    selected_timeout = timeout_s if timeout_s is not None else settings.llm.timeout_s

    try:
        return _call_provider(
            provider="主模型",
            base_url=settings.llm.base_url,
            api_key=settings.llm.chat_api_key(),
            timeout_s=selected_timeout,
            retry=settings.llm.retry,
            messages=messages,
            model=selected_model,
            temperature=(selected_temperature if settings.llm.chat_use_temperature else None),
            max_tokens=settings.llm.max_tokens,
            max_tokens_parameter=settings.llm.chat_max_tokens_parameter,
            response_format=response_format,
            tools=tools,
            extra_body=settings.llm.chat_extra_body,
        )
    except ConfigError:
        raise
    except Exception as primary_error:
        if not _should_fallback(primary_error):
            raise LLMError(
                "主模型调用失败",
                detail=_error_summary(primary_error),
            ) from primary_error

        logger.warning(
            "主模型不可用，准备切换备用模型：{}",
            _error_summary(primary_error),
        )

        try:
            return _call_provider(
                provider="备用模型",
                base_url=settings.llm.fallback_base_url,
                api_key=settings.llm.fallback_api_key(),
                timeout_s=selected_timeout,
                retry=settings.llm.retry,
                messages=messages,
                model=settings.llm.fallback_model_chat,
                temperature=(
                    selected_temperature if settings.llm.fallback_use_temperature else None
                ),
                max_tokens=settings.llm.max_tokens,
                max_tokens_parameter=settings.llm.fallback_max_tokens_parameter,
                response_format=response_format,
                tools=tools,
                extra_body=settings.llm.fallback_extra_body,
            )
        except ConfigError:
            raise
        except Exception as fallback_error:
            raise LLMError(
                "主模型和备用模型调用均失败",
                detail=(
                    f"primary={_error_summary(primary_error)}；"
                    f"fallback={_error_summary(fallback_error)}"
                ),
            ) from fallback_error


def vision_chat(
    messages: Sequence[ChatCompletionMessageParam],
    *,
    temperature: float | None = None,
    response_format: ResponseFormat | None = None,
    timeout_s: float | None = None,
) -> ChatCompletionMessage:
    """调用独立视觉模型；不降级到不支持看图的文本模型。"""
    settings = get_settings()
    selected_temperature = (
        temperature if temperature is not None else settings.llm.structured_temperature
    )
    selected_timeout = timeout_s if timeout_s is not None else settings.llm.timeout_s
    try:
        return _call_provider(
            provider="视觉模型",
            base_url=settings.llm.vision_base_url,
            api_key=settings.llm.vision_api_key(),
            timeout_s=selected_timeout,
            retry=settings.llm.retry,
            messages=messages,
            model=settings.llm.model_vision,
            temperature=(selected_temperature if settings.llm.vision_use_temperature else None),
            max_tokens=settings.llm.vision_max_tokens,
            max_tokens_parameter=settings.llm.vision_max_tokens_parameter,
            response_format=response_format,
            tools=None,
            extra_body=settings.llm.vision_extra_body,
        )
    except ConfigError:
        raise
    except Exception as error:
        raise LLMError(
            "视觉模型调用失败",
            detail=_error_summary(error),
        ) from error
