"""Anthropic (Claude) API provider — uses official anthropic SDK."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from anthropic.types import (
    CacheControlEphemeralParam,
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from codebase_rag.common.logging import get_logger
from codebase_rag.services.agent.errors import LLMAPIError, LLMAuthError, LLMRateLimitError
from codebase_rag.services.agent.models import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from codebase_rag.services.agent.providers.base import LLMProvider

logger = get_logger(__name__)

_CACHE_BREAKPOINT_BLOCK_GAP_THRESHOLD = 20
_CACHE_CONTROL_SYSTEM = CacheControlEphemeralParam(type="ephemeral", ttl="1h")
_CACHE_CONTROL_EPHEMERAL = CacheControlEphemeralParam(type="ephemeral")


@dataclass
class _CacheAnchorState:
    """Tracks the last cache breakpoint position for incremental prompt caching."""
    message_count: int
    anchor_message_index: int
    anchor_message_hash: str


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API via official SDK."""

    def __init__(self, api_base_url: str, request_timeout: int = 120) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = request_timeout
        self._session_anchors: Dict[str, _CacheAnchorState] = {}
        self._pending_anchors: Dict[str, _CacheAnchorState] = {}

    def _make_client(self, api_key: str) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=self._api_base_url,
            timeout=self._timeout,
        )

    async def chat_completion(
        self,
        api_key: str,
        messages: List[Message],
        model: str,
        tools: Optional[List[ToolSchema]] = None,
        output_schema: Optional[Dict] = None,
        temperature: float = 0.0,
        max_output_tokens: Optional[int] = None,
        tool_choice: Optional[str] = None,
        thinking_config: Optional[Dict] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,
        prompt_cache_key: Optional[str] = None,
    ) -> LLMResponse:
        client = self._make_client(api_key)
        headers = dict(custom_headers or {})
        if session_id:
            headers["X-Relay-Session-Id"] = session_id
        headers["anthropic-beta"] = "prompt-caching-2024-07-31"

        # Extract system prompt
        system_text = None
        chat_messages = []
        for msg in messages:
            if msg.role == "system":
                system_text = msg.content
            else:
                chat_messages.append(msg)

        built_messages = self._build_messages(chat_messages)

        # Apply prompt caching: cache_control on system + message breakpoints
        cached_system = None
        if system_text:
            cached_system = [TextBlockParam(
                type="text",
                text=system_text,
                cache_control=_CACHE_CONTROL_SYSTEM,
            )]

        last_anchor = self._session_anchors.get(session_id) if session_id else None
        built_messages, new_anchor = self._apply_message_cache_control(
            built_messages, last_anchor,
        )
        if session_id and new_anchor:
            self._pending_anchors[session_id] = new_anchor

        kwargs: Dict = {
            "model": model,
            "messages": built_messages,
            "temperature": temperature,
            "max_tokens": max_output_tokens or 128000,
        }

        if cached_system:
            kwargs["system"] = cached_system
        elif system_text:
            kwargs["system"] = system_text

        # Thinking — override via extra_body (SDK custom field)
        extra_body = thinking_config if thinking_config else None

        has_forced_schema = False
        if tools:
            kwargs["tools"] = [
                ToolParam(
                    name=t.name,
                    description=t.description,
                    input_schema=t.parameters,
                )
                for t in tools
            ]
            # Tool choice: "auto" | "any" | "none" or {"type": "tool", "name": "xxx"}
            if tool_choice:
                _TC_MAP = {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "auto"}}
                kwargs["tool_choice"] = _TC_MAP.get(tool_choice, {"type": "auto"})

        # Structured output via forced tool_use
        if output_schema and not tools:
            kwargs["tools"] = [ToolParam(
                name="structured_output",
                description="Output structured data",
                input_schema=output_schema,
            )]
            kwargs["tool_choice"] = {"type": "tool", "name": "structured_output"}
            has_forced_schema = True

        try:
            # Use streaming mode — many proxies only support stream=true
            async with client.messages.stream(
                **kwargs, extra_headers=headers,
                extra_body=extra_body,
            ) as stream:
                response = await stream.get_final_message()
        except anthropic.AuthenticationError as e:
            if session_id:
                self._pending_anchors.pop(session_id, None)
            raise LLMAuthError(f"Anthropic auth error: {e}") from e
        except anthropic.RateLimitError as e:
            if session_id:
                self._pending_anchors.pop(session_id, None)
            raise LLMRateLimitError(f"Anthropic rate limit: {e}") from e
        except anthropic.APIError as e:
            if session_id:
                self._pending_anchors.pop(session_id, None)
            raise LLMAPIError(f"Anthropic API error: {e}") from e

        # Commit pending anchor on success
        if session_id and session_id in self._pending_anchors:
            self._session_anchors[session_id] = self._pending_anchors.pop(session_id)

        return self._parse_response(response, has_output_schema=has_forced_schema)

    def cleanup_session(self, session_id: str) -> None:
        """Remove cached anchor state for a completed session."""
        self._session_anchors.pop(session_id, None)
        self._pending_anchors.pop(session_id, None)

    async def health_check(self, api_key: str) -> bool:
        try:
            client = self._make_client(api_key)
            await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1,
                messages=[MessageParam(role="user", content="hi")],
            )
            return True
        except Exception:
            return False

    async def probe_stream(
        self,
        api_key: str,
        model: str,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> bool:
        client = self._make_client(api_key)
        headers = dict(custom_headers or {})
        try:
            async with client.messages.stream(
                model=model,
                max_tokens=1,
                temperature=0.0,
                messages=[MessageParam(role="user", content=".")],
                extra_headers=headers,
            ) as stream:
                async for text in stream.text_stream:
                    if text:
                        return True
            return False
        except anthropic.AuthenticationError as e:
            raise LLMAuthError(f"Anthropic auth error: {e}") from e
        except anthropic.RateLimitError as e:
            raise LLMRateLimitError(f"Anthropic rate limit: {e}") from e
        except anthropic.APIError as e:
            raise LLMAPIError(f"Anthropic API error: {e}") from e

    async def count_tokens(self, text: str, model: str, api_key: str = "") -> int:
        """Count tokens via Anthropic SDK count_tokens API."""
        if not api_key:
            raise ValueError("api_key required for Anthropic count_tokens")
        client = self._make_client(api_key)
        response = await client.messages.count_tokens(
            model=model,
            messages=[MessageParam(role="user", content=text)],
        )
        return response.input_tokens

    # -----------------------------------------------------------------------
    # Prompt caching helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _message_hash(msg: MessageParam) -> str:
        """Stable hash of a built message dict for anchor validation."""
        return hashlib.md5(json.dumps(msg, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _add_cache_control_to_message(msg: MessageParam) -> MessageParam:
        """Add cache_control (ephemeral) to the last content block of a message (mutates)."""
        content = msg.get("content")
        if isinstance(content, list) and content:
            last_block = content[-1]
            if isinstance(last_block, dict):
                last_block["cache_control"] = _CACHE_CONTROL_EPHEMERAL
        elif isinstance(content, str):
            msg["content"] = [TextBlockParam(
                type="text",
                text=content,
                cache_control=_CACHE_CONTROL_EPHEMERAL,
            )]
        return msg

    @staticmethod
    def _count_blocks_in_range(messages: List[MessageParam], start_idx: int, end_idx: int) -> int:
        """Count total content blocks in messages[start_idx:end_idx].

        Anthropic counts each item in a message's content array as one block.
        A string content counts as 1 block.
        """
        total = 0
        for msg in messages[start_idx:end_idx]:
            content = msg.get("content")
            if isinstance(content, list):
                total += len(content)
            else:
                total += 1
        return total

    @classmethod
    def _apply_message_cache_control(
        cls,
        messages: List[MessageParam],
        previous_anchor: Optional[_CacheAnchorState],
    ) -> Tuple[List[MessageParam], Optional[_CacheAnchorState]]:
        """Apply cache_control breakpoints to message list.

        Anthropic server searches from the BOTTOM of messages upward for
        the most recent cached block, but only scans up to 20 blocks.
        If the old cache breakpoint is more than 20 blocks away from
        the current end, we must explicitly re-mark it with cache_control.

        Strategy:
        1. Always mark the LAST message's last block with cache_control.
        2. If previous anchor is still valid and the block gap > 20,
           also mark the old anchor to ensure the server finds it.
        3. Return new anchor state for commit after successful request.
        """
        if not messages:
            return messages, None

        current_end_idx = len(messages) - 1

        # Build new anchor from current end
        new_anchor = _CacheAnchorState(
            message_count=len(messages),
            anchor_message_index=current_end_idx,
            anchor_message_hash=cls._message_hash(messages[current_end_idx]),
        )

        # Check if previous anchor is still valid
        if (
            previous_anchor
            and previous_anchor.message_count <= len(messages)
            and previous_anchor.anchor_message_index < len(messages)
        ):
            old_idx = previous_anchor.anchor_message_index
            old_hash = cls._message_hash(messages[old_idx])
            if old_hash == previous_anchor.anchor_message_hash:
                # Count content blocks between old anchor and current end
                block_gap = cls._count_blocks_in_range(
                    messages, old_idx + 1, current_end_idx + 1,
                )
                if block_gap > _CACHE_BREAKPOINT_BLOCK_GAP_THRESHOLD:
                    cls._add_cache_control_to_message(messages[old_idx])

        # Always mark the current end
        cls._add_cache_control_to_message(messages[current_end_idx])

        return messages, new_anchor

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_messages(messages: List[Message]) -> List[MessageParam]:
        result: List[MessageParam] = []
        for msg in messages:
            if msg.role == "tool" and msg.tool_call_id:
                tool_block = ToolResultBlockParam(
                    type="tool_result",
                    tool_use_id=msg.tool_call_id,
                    content=[TextBlockParam(type="text", text=msg.content or "")],
                )
                # Merge consecutive tool_result into one user message
                # (Anthropic API forbids consecutive messages with the same role)
                if result and result[-1]["role"] == "user" and isinstance(result[-1]["content"], list):
                    last_content = result[-1]["content"]
                    if last_content and isinstance(last_content[0], dict) and last_content[0].get("type") == "tool_result":
                        last_content.append(tool_block)
                        continue
                result.append(MessageParam(role="user", content=[tool_block]))
            elif msg.tool_calls:
                content_blocks: list = []
                # NOTE: thinking blocks require a 'signature' field for multi-turn.
                # Proxies strip signatures, so we omit thinking blocks entirely.
                # Anthropic docs allow omitting them (model loses some context).
                if msg.content:
                    content_blocks.append(TextBlockParam(type="text", text=msg.content))
                for tc in msg.tool_calls:
                    content_blocks.append(ToolUseBlockParam(
                        type="tool_use",
                        id=tc.id,
                        name=tc.name,
                        input=tc.arguments,
                    ))
                result.append(MessageParam(role="assistant", content=content_blocks))
            elif msg.role == "assistant" and msg.thinking_content:
                # Omit thinking blocks (no signature available)
                result.append(MessageParam(
                    role="assistant",
                    content=msg.content or "",
                ))
            else:
                result.append(MessageParam(
                    role=msg.role,  # type: ignore[arg-type]
                    content=msg.content or "",
                ))
        return result

    @staticmethod
    def _parse_response(response, has_output_schema: bool = False) -> LLMResponse:
        content_blocks = response.content or []
        stop_reason = response.stop_reason or "end_turn"

        text_parts = []
        thinking_parts = []
        tool_calls = []
        structured_output = None

        for block in content_blocks:
            if block.type == "thinking":
                thinking_parts.append(block.thinking)
            elif block.type == "text":
                # Some proxies embed thinking as <thinking> tags in text blocks
                # instead of returning proper thinking content blocks.
                raw = block.text or ""
                m = re.search(r"<thinking>(.*?)</thinking>", raw, re.DOTALL)
                if m:
                    thinking_parts.append(m.group(1).strip())
                    cleaned = re.sub(r"\s*<thinking>.*?</thinking>\s*", "", raw, flags=re.DOTALL).strip()
                    if cleaned:
                        text_parts.append(cleaned)
                else:
                    text_parts.append(raw)
            elif block.type == "tool_use":
                if has_output_schema and block.name == "structured_output":
                    structured_output = block.input
                else:
                    tool_calls.append(ToolCall(
                        id=block.id or "",
                        name=block.name or "",
                        arguments=block.input or {},
                    ))

        content = "\n".join(text_parts) if text_parts else None
        thinking_content = "\n".join(thinking_parts) if thinking_parts else None

        if content and not structured_output:
            try:
                structured_output = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                pass

        if tool_calls:
            finish_reason = "tool_calls"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        else:
            finish_reason = "stop"

        # Forced schema via tool_use → treat as stop
        if has_output_schema and structured_output and not tool_calls:
            finish_reason = "stop"

        usage = TokenUsage()
        if response.usage:
            cache_creation = getattr(response.usage, 'cache_creation_input_tokens', 0) or 0
            cache_read = getattr(response.usage, 'cache_read_input_tokens', 0) or 0
            usage = TokenUsage(
                prompt_tokens=response.usage.input_tokens or 0,
                completion_tokens=response.usage.output_tokens or 0,
                total_tokens=(response.usage.input_tokens or 0) + (response.usage.output_tokens or 0),
                cache_creation_tokens=cache_creation,
                cache_read_tokens=cache_read,
            )

        return LLMResponse(
            content=content,
            thinking_content=thinking_content,
            tool_calls=tool_calls if tool_calls else None,
            structured_output=structured_output,
            finish_reason=finish_reason,
            usage=usage,
        )
