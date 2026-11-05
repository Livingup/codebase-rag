"""Anthropic (Claude) provider — delegates protocol handling to llm-sdk.

The prompt-caching breakpoint strategy is business logic and stays here; all
HTTP/SSE parsing, retry and error classification come from llm-sdk.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from pythonsdk import (
    ClaudeCountTokensClient,
    ClaudeCountTokensRequest,
    ClaudeMessagesClient,
    ClaudeMessagesRequest,
)

from codebase_rag.common.logging import get_logger
from codebase_rag.services.agent.models import LLMResponse, Message, ToolSchema
from codebase_rag.services.agent.providers.base import LLMProvider
from codebase_rag.services.agent.providers.llm_sdk_common import (
    collect_stream,
    make_client_config,
    make_request_options,
    with_api_version,
)

logger = get_logger(__name__)

_CACHE_BREAKPOINT_BLOCK_GAP_THRESHOLD = 20
_CACHE_CONTROL_SYSTEM = {"type": "ephemeral", "ttl": "1h"}
_CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}
_PROMPT_CACHING_BETA = "prompt-caching-2024-07-31"


@dataclass
class _CacheAnchorState:
    """Tracks the last cache breakpoint position for incremental prompt caching."""

    message_count: int
    anchor_message_index: int
    anchor_message_hash: str


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API via llm-sdk."""

    def __init__(self, api_base_url: str, request_timeout: int = 120) -> None:
        self._api_base_url = with_api_version(api_base_url.rstrip("/"), "v1")
        self._timeout = request_timeout
        self._session_anchors: Dict[str, _CacheAnchorState] = {}
        self._pending_anchors: Dict[str, _CacheAnchorState] = {}

    def _make_client(self, api_key: str) -> ClaudeMessagesClient:
        return ClaudeMessagesClient(
            make_client_config(api_key, self._api_base_url, self._timeout),
            anthropic_beta=[_PROMPT_CACHING_BETA],
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
            cached_system = [
                {"type": "text", "text": system_text, "cache_control": _CACHE_CONTROL_SYSTEM}
            ]

        last_anchor = self._session_anchors.get(session_id) if session_id else None
        built_messages, new_anchor = self._apply_message_cache_control(
            built_messages, last_anchor,
        )
        if session_id and new_anchor:
            self._pending_anchors[session_id] = new_anchor

        has_forced_schema = False
        claude_tools: Optional[List[Dict[str, Any]]] = None
        claude_tool_choice: Optional[Dict[str, Any]] = None

        if tools:
            claude_tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
            # Tool choice: "auto" | "any" | "none" | {"type": "tool", "name": ...}
            _TC_MAP = {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "auto"}}
            if tool_choice:
                claude_tool_choice = _TC_MAP.get(tool_choice, {"type": "auto"})

        # Structured output via forced tool_use
        if output_schema and not tools:
            claude_tools = [
                {
                    "name": "structured_output",
                    "description": "Output structured data",
                    "input_schema": output_schema,
                }
            ]
            claude_tool_choice = {"type": "tool", "name": "structured_output"}
            has_forced_schema = True

        request = ClaudeMessagesRequest(
            model=model,
            max_tokens=max_output_tokens or 128000,
            messages=built_messages,
            system=cached_system or system_text,
            temperature=temperature,
            tools=claude_tools,
            tool_choice=claude_tool_choice,
            extra_fields=thinking_config or {},
        )

        try:
            response = await collect_stream(
                client.stream(
                    request,
                    make_request_options(custom_headers, session_id),
                ),
                provider_label="Anthropic",
                forced_tool_name="structured_output" if has_forced_schema else None,
                extract_thinking_tags=True,
            )
        except BaseException:
            if session_id:
                self._pending_anchors.pop(session_id, None)
            raise

        # Commit pending anchor on success
        if session_id and session_id in self._pending_anchors:
            self._session_anchors[session_id] = self._pending_anchors.pop(session_id)

        if has_forced_schema and response.tool_calls:
            response.tool_calls = None
        if has_forced_schema and response.structured_output:
            response.finish_reason = "stop"
        return response

    def cleanup_session(self, session_id: str) -> None:
        """Remove cached anchor state for a completed session."""
        self._session_anchors.pop(session_id, None)
        self._pending_anchors.pop(session_id, None)

    async def count_tokens(self, text: str, model: str, api_key: str = "") -> int:
        """Count tokens via Anthropic count_tokens API."""
        if not api_key:
            raise ValueError("api_key required for Anthropic count_tokens")
        client = ClaudeCountTokensClient(
            make_client_config(api_key, self._api_base_url, self._timeout),
            anthropic_beta=[_PROMPT_CACHING_BETA],
        )
        return await client.count_tokens(
            ClaudeCountTokensRequest(
                model=model,
                messages=[{"role": "user", "content": text}],
            )
        )

    # -----------------------------------------------------------------------
    # Prompt caching helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _message_hash(msg: Dict[str, Any]) -> str:
        """Stable hash of a built message dict for anchor validation."""
        return hashlib.md5(json.dumps(msg, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _add_cache_control_to_message(msg: Dict[str, Any]) -> Dict[str, Any]:
        """Add cache_control (ephemeral) to the last content block of a message (mutates)."""
        content = msg.get("content")
        if isinstance(content, list) and content:
            last_block = content[-1]
            if isinstance(last_block, dict):
                last_block["cache_control"] = _CACHE_CONTROL_EPHEMERAL
        elif isinstance(content, str):
            msg["content"] = [
                {"type": "text", "text": content, "cache_control": _CACHE_CONTROL_EPHEMERAL}
            ]
        return msg

    @staticmethod
    def _count_blocks_in_range(messages: List[Dict[str, Any]], start_idx: int, end_idx: int) -> int:
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
        messages: List[Dict[str, Any]],
        previous_anchor: Optional[_CacheAnchorState],
    ) -> Tuple[List[Dict[str, Any]], Optional[_CacheAnchorState]]:
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
    def _build_messages(messages: List[Message]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "tool" and msg.tool_call_id:
                tool_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": [{"type": "text", "text": msg.content or ""}],
                }
                # Merge consecutive tool_result into one user message
                # (Anthropic API forbids consecutive messages with the same role)
                if result and result[-1]["role"] == "user" and isinstance(result[-1]["content"], list):
                    last_content = result[-1]["content"]
                    if last_content and isinstance(last_content[0], dict) and last_content[0].get("type") == "tool_result":
                        last_content.append(tool_block)
                        continue
                result.append({"role": "user", "content": [tool_block]})
            elif msg.tool_calls:
                content_blocks: list = []
                # NOTE: thinking blocks require a 'signature' field for multi-turn.
                # Proxies strip signatures, so we omit thinking blocks entirely.
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                result.append({"role": "assistant", "content": content_blocks})
            elif msg.role == "assistant" and msg.thinking_content:
                # Omit thinking blocks (no signature available)
                result.append({"role": "assistant", "content": msg.content or ""})
            elif isinstance(msg.content, list):
                anthropic_blocks: List[Dict[str, Any]] = []
                for block in msg.content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        anthropic_blocks.append({"type": "text", "text": str(block.get("text", ""))})
                    elif btype == "image_url":
                        url = str((block.get("image_url") or {}).get("url", ""))
                        if url.startswith("data:") and ";base64," in url:
                            header, b64_data = url.split(";base64,", 1)
                            mime = header[5:] or "image/png"
                            anthropic_blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime,
                                    "data": b64_data,
                                },
                            })
                    else:
                        anthropic_blocks.append(block)
                result.append({"role": msg.role, "content": anthropic_blocks})
            else:
                result.append({"role": msg.role, "content": msg.content or ""})
        return result
