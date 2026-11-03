"""OpenAI Chat Completions provider — delegates protocol handling to llm-sdk.

Compatible with: OpenAI, DeepSeek, 通义千问, Moonshot, 零一万物,
and any OpenAI-compatible third-party service (just change api_base_url).
"""

from __future__ import annotations

from typing import Dict, List, Optional

import tiktoken
from pythonsdk import OpenAIChatCompletionsClient, OpenAIChatCompletionsRequest

from codebase_rag.common.logging import get_logger
from codebase_rag.services.agent.models import (
    LLMResponse,
    Message,
    ToolSchema,
)
from codebase_rag.services.agent.providers.base import LLMProvider
from codebase_rag.services.agent.providers.llm_sdk_common import (
    build_chat_messages,
    build_chat_tools,
    collect_stream,
    make_client_config,
    make_request_options,
    with_api_version,
)

logger = get_logger(__name__)


class OpenAIChatProvider(LLMProvider):
    """OpenAI Chat Completions via llm-sdk. Works with any compatible API."""

    def __init__(self, api_base_url: str, request_timeout: int = 120) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = request_timeout

    def _make_client(self, api_key: str) -> OpenAIChatCompletionsClient:
        return OpenAIChatCompletionsClient(
            make_client_config(
                api_key,
                with_api_version(self._api_base_url, "v1"),
                self._timeout,
            )
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

        request = OpenAIChatCompletionsRequest(
            model=model,
            messages=build_chat_messages(messages),
            temperature=temperature,
            max_tokens=max_output_tokens,
            tools=build_chat_tools(tools) if tools else None,
            tool_choice=tool_choice if tools and tool_choice else None,
            prompt_cache_key=prompt_cache_key,
            extra_fields=thinking_config or {},
        )

        if output_schema:
            request.response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "output",
                    "schema": output_schema,
                    "strict": True,
                },
            }

        return await collect_stream(
            client.stream(request, make_request_options(custom_headers, session_id)),
            provider_label="OpenAI",
        )

    async def count_tokens(self, text: str, model: str, api_key: str = "") -> int:
        """Count tokens via tiktoken (accurate for OpenAI-compatible models)."""
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
