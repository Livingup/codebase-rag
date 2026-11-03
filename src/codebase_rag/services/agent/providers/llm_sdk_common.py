"""Bridge between the ``pythonsdk`` (llm-sdk) protocol clients and codebase-rag.

All HTTP / SSE / retry / error handling for LLM calls lives in llm-sdk. This
module only:

* builds a :class:`~pythonsdk.ClientConfig` from provider settings,
* converts llm-sdk stream events into :class:`LLMResponse`,
* maps llm-sdk error kinds onto the Agent layer exception hierarchy.

Batch/key scheduling and retry decisions stay in ``agent/service.py`` and
``embedding/service.py``; llm-sdk's own retry loop is disabled
(``max_attempts=1``) so the existing KeyPool semantics remain authoritative.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator, Dict, List, Optional

from pythonsdk import (
    ClientConfig,
    ErrorKind,
    LLMClientError,
    RequestOptions,
    RetryPolicy,
    TransportTimeouts,
)
from pythonsdk import TokenUsage as SDKTokenUsage

from codebase_rag.services.agent.errors import (
    LLMAPIError,
    LLMAuthError,
    LLMRateLimitError,
)
from codebase_rag.services.agent.models import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
)

_FINISH_REASON_MAP: Dict[str, str] = {
    "stop": "stop",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "completed": "stop",
    "STOP": "stop",
    "FINISH_REASON_UNSPECIFIED": "stop",
    "length": "length",
    "max_tokens": "length",
    "MAX_TOKENS": "length",
    "incomplete": "length",
    "tool_calls": "tool_calls",
    "tool_use": "tool_calls",
    "content_filter": "error",
    "insufficient_system_resource": "error",
    "refusal": "error",
    "failed": "error",
    "cancelled": "error",
    "SAFETY": "error",
    "RECITATION": "error",
    "BLOCKLIST": "error",
    "PROHIBITED_CONTENT": "error",
    "SPII": "error",
    "MALFORMED_FUNCTION_CALL": "error",
    "IMAGE_SAFETY": "error",
    "OTHER": "error",
}

_THINKING_TAG_RE = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL)
_THINKING_TAG_STRIP_RE = re.compile(r"\s*<thinking>.*?</thinking>\s*", re.DOTALL)


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def with_api_version(base_url: Optional[str], version: str) -> str:
    """Ensures ``base_url`` ends with a version segment such as ``/v1``."""
    base = (base_url or "").rstrip("/")
    if base.endswith(f"/{version}"):
        return base
    return f"{base}/{version}"


def make_client_config(
    api_key: str,
    base_url: Optional[str],
    request_timeout: int,
) -> ClientConfig:
    """Builds an llm-sdk client config for a single request.

    The SDK retry loop is disabled on purpose: retries, cooldown and key
    rotation are owned by the Agent/Embedding services.
    """
    timeout_ms = max(1, int(request_timeout)) * 1000
    return ClientConfig(
        api_key=api_key,
        base_url=base_url or None,
        retry_policy=RetryPolicy(max_attempts=1),
        timeouts=TransportTimeouts(
            connect_timeout_ms=min(15000, timeout_ms),
            response_header_timeout_ms=timeout_ms,
            stream_idle_timeout_ms=timeout_ms,
        ),
    )


def make_request_options(
    custom_headers: Optional[Dict[str, str]] = None,
    session_id: Optional[str] = None,
) -> RequestOptions:
    headers = dict(custom_headers or {})
    if session_id:
        headers["X-Relay-Session-Id"] = session_id
    return RequestOptions(custom_headers=headers or None)


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def error_from_sdk(exc: LLMClientError) -> Exception:
    """Maps an llm-sdk error kind onto the Agent layer exception hierarchy."""
    message = str(exc)
    if exc.kind == ErrorKind.ABORTED:
        # Task cancellation is surfaced by llm-sdk as ABORTED; restore the
        # asyncio semantics the Agent layer relies on.
        return asyncio.CancelledError(message)
    if exc.kind == ErrorKind.AUTH_ERROR:
        return LLMAuthError(message)
    if exc.kind in (ErrorKind.RATE_LIMIT, ErrorKind.OVERLOADED):
        return LLMRateLimitError(message)
    return LLMAPIError(message)


def translate_llm_error(exc: Exception) -> Exception:
    if isinstance(exc, LLMClientError):
        return error_from_sdk(exc)
    return LLMAPIError(str(exc))


# ---------------------------------------------------------------------------
# Stream aggregation
# ---------------------------------------------------------------------------


def _usage_from_sdk(usage: Optional[SDKTokenUsage]) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    total = usage.total_tokens or (usage.input_tokens + usage.output_tokens)
    return TokenUsage(
        prompt_tokens=usage.input_tokens,
        completion_tokens=usage.output_tokens,
        total_tokens=total,
        cache_creation_tokens=usage.cache_creation_input_tokens,
        cache_read_tokens=usage.cache_read_input_tokens,
    )


def _join_blocks(blocks: Dict[int, List[str]]) -> Optional[str]:
    if not blocks:
        return None
    parts = ["".join(blocks[idx]) for idx in sorted(blocks.keys())]
    joined = "\n".join(parts) if len(parts) > 1 else parts[0]
    return joined or None


def _parse_arguments(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _split_thinking_tags(content: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Extracts ``<thinking>...</thinking>`` blocks some Claude proxies emit inline."""
    if not content:
        return content, None
    matches = _THINKING_TAG_RE.findall(content)
    if not matches:
        return content, None
    cleaned = _THINKING_TAG_STRIP_RE.sub("", content).strip() or None
    thinking = "\n".join(m.strip() for m in matches) or None
    return cleaned, thinking


async def collect_stream(
    events: AsyncIterator[Any],
    *,
    provider_label: str,
    forced_tool_name: Optional[str] = None,
    extract_thinking_tags: bool = False,
) -> LLMResponse:
    """Consumes llm-sdk stream events and aggregates them into an ``LLMResponse``."""
    text_blocks: Dict[int, List[str]] = {}
    thinking_blocks: Dict[int, List[str]] = {}
    tool_calls: List[ToolCall] = []
    structured_output: Optional[Dict[str, Any]] = None
    finish_raw: Optional[str] = None
    usage = TokenUsage()

    try:
        async for event in events:
            event_type = getattr(event, "type", "")
            if event_type == "text_delta":
                text_blocks.setdefault(event.block_index, []).append(event.delta)
            elif event_type == "thinking_delta":
                thinking_blocks.setdefault(event.block_index, []).append(event.delta)
            elif event_type == "tool_call_stop":
                arguments = _parse_arguments(event.arguments_raw)
                if forced_tool_name and event.name == forced_tool_name:
                    structured_output = arguments
                else:
                    tool_calls.append(
                        ToolCall(
                            id=event.call_id,
                            name=event.name,
                            arguments=arguments,
                        )
                    )
            elif event_type == "response_complete":
                finish_raw = event.stop_reason
                usage = _usage_from_sdk(event.usage)
    except LLMClientError as exc:
        raise error_from_sdk(exc) from exc
    except Exception as exc:  # network/transport failures outside the taxonomy
        raise translate_llm_error(exc) from exc

    content = _join_blocks(text_blocks)
    thinking_content = _join_blocks(thinking_blocks)

    if extract_thinking_tags:
        content, inline_thinking = _split_thinking_tags(content)
        if inline_thinking:
            thinking_content = (
                f"{thinking_content}\n{inline_thinking}"
                if thinking_content
                else inline_thinking
            )

    if structured_output is None and content:
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            structured_output = parsed

    if tool_calls:
        finish_reason = "tool_calls"
    else:
        finish_reason = _FINISH_REASON_MAP.get(str(finish_raw), "stop")

    return LLMResponse(
        content=content,
        thinking_content=thinking_content,
        tool_calls=tool_calls or None,
        structured_output=structured_output,
        finish_reason=finish_reason,
        usage=usage,
    )


# ---------------------------------------------------------------------------
# Message conversion helpers (codebase-rag Message -> native wire shapes)
# ---------------------------------------------------------------------------


def build_chat_messages(
    messages: List[Message],
    *,
    include_reasoning_content: bool = False,
) -> List[Dict[str, Any]]:
    """OpenAI Chat Completions ``messages`` array."""
    result: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            result.append({"role": "system", "content": msg.content or ""})
        elif msg.role == "tool" and msg.tool_call_id:
            result.append(
                {
                    "role": "tool",
                    "content": msg.content or "",
                    "tool_call_id": msg.tool_call_id,
                }
            )
        elif msg.role == "assistant":
            assistant: Dict[str, Any] = {"role": "assistant", "content": msg.content}
            if include_reasoning_content and msg.thinking_content is not None:
                assistant["reasoning_content"] = msg.thinking_content
            if msg.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            result.append(assistant)
        else:
            result.append({"role": "user", "content": msg.content or ""})
    return result


def build_chat_tools(tools: List[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]
