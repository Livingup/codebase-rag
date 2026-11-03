"""DeepSeek provider — delegates protocol handling to llm-sdk.

Differs from plain OpenAI Chat Completions in three ways handled here:
- ``reasoning_content`` must be echoed back on assistant messages,
- thinking is toggled through request-level fields (``thinking`` /
  ``reasoning_effort``),
- structured output uses ``response_format={"type": "json_object"}``.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import tiktoken
from pythonsdk import OpenAIChatCompletionsClient, OpenAIChatCompletionsRequest

from codebase_rag.common.logging import get_logger
from codebase_rag.services.agent.models import LLMResponse, Message, ToolSchema
from codebase_rag.services.agent.providers.base import LLMProvider
from codebase_rag.services.agent.providers.llm_sdk_common import (
    build_chat_messages,
    build_chat_tools,
    collect_stream,
    make_client_config,
    make_request_options,
)

logger = get_logger(__name__)


class DeepSeekProvider(LLMProvider):
    """DeepSeek chat completions via llm-sdk."""

    def __init__(self, api_base_url: str, request_timeout: int = 120) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = request_timeout

    def _make_client(self, api_key: str) -> OpenAIChatCompletionsClient:
        return OpenAIChatCompletionsClient(
            make_client_config(api_key, self._api_base_url, self._timeout)
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
        extra_fields = self._normalize_extra_body(thinking_config)

        request = OpenAIChatCompletionsRequest(
            model=model,
            messages=build_chat_messages(messages, include_reasoning_content=True),
            # DeepSeek rejects temperature sampling while thinking is enabled.
            temperature=None if self._is_thinking_enabled(extra_fields) else temperature,
            max_tokens=max_output_tokens,
            tools=build_chat_tools(tools) if tools else None,
            tool_choice=tool_choice if tools and tool_choice else None,
            prompt_cache_key=prompt_cache_key,
            extra_fields=extra_fields or {},
        )

        if output_schema:
            request.response_format = {"type": "json_object"}

        return await collect_stream(
            client.stream(request, make_request_options(custom_headers, session_id)),
            provider_label="DeepSeek",
        )

    async def count_tokens(self, text: str, model: str, api_key: str = "") -> int:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))

    @staticmethod
    def _normalize_extra_body(thinking_config: Optional[Dict]) -> Optional[Dict]:
        if not thinking_config:
            return None
        extra_body = dict(thinking_config)
        thinking = extra_body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "disabled":
            extra_body.pop("reasoning_effort", None)
        return extra_body

    @staticmethod
    def _is_thinking_enabled(extra_body: Optional[Dict]) -> bool:
        if not extra_body:
            return True
        thinking = extra_body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "disabled":
            return False
        return True
