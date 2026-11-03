"""Gemini provider — delegates protocol handling to llm-sdk."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pythonsdk import (
    GeminiEmbeddingClient,
    GeminiStreamClient,
    GeminiStreamRequest,
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

_TOOL_CHOICE_MODES = {"auto": "AUTO", "none": "NONE", "required": "ANY"}


class GeminiLLMProvider(LLMProvider):
    """Google Gemini API provider via llm-sdk."""

    def __init__(self, api_base_url: str, request_timeout: int = 120) -> None:
        self._api_base_url = (api_base_url or "").rstrip("/")
        self._timeout = request_timeout

    def _make_client(self, api_key: str) -> GeminiStreamClient:
        return GeminiStreamClient(
            make_client_config(
                api_key,
                with_api_version(self._api_base_url, "v1beta"),
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

        contents = self._build_contents(messages)

        gen_config: Dict[str, Any] = {"temperature": temperature}
        if max_output_tokens:
            gen_config["maxOutputTokens"] = max_output_tokens

        # Thinking — raw nested generationConfig.thinkingConfig from configuration
        if thinking_config:
            gen_config.update(thinking_config.get("generationConfig", {}))

        if output_schema:
            gen_config["responseMimeType"] = "application/json"
            gen_config["responseSchema"] = output_schema

        system_msgs = [m for m in messages if m.role == "system"]
        system_instruction = None
        if system_msgs:
            system_instruction = {"parts": [{"text": system_msgs[0].content or ""}]}

        gemini_tools = None
        tool_config = None
        if tools:
            gemini_tools = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        }
                        for t in tools
                    ]
                }
            ]
            mode = _TOOL_CHOICE_MODES.get(tool_choice or "")
            if mode:
                tool_config = {"functionCallingConfig": {"mode": mode}}

        request = GeminiStreamRequest(
            model=model,
            contents=contents,
            systemInstruction=system_instruction,
            tools=gemini_tools,
            toolConfig=tool_config,
            generationConfig=gen_config,
        )

        return await collect_stream(
            client.stream(request, make_request_options(custom_headers, session_id)),
            provider_label="Gemini",
        )

    async def count_tokens(self, text: str, model: str, api_key: str = "") -> int:
        """Count tokens via Gemini countTokens API."""
        if not api_key:
            raise ValueError("api_key required for Gemini count_tokens")
        origin = re.sub(r"/v1beta$", "", self._api_base_url)
        client = GeminiEmbeddingClient(
            make_client_config(api_key, origin or None, self._timeout),
            model=model,
        )
        return await client.count_tokens(text)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_contents(messages: List[Message]) -> List[Dict[str, Any]]:
        """Convert Message list to Gemini ``contents`` objects."""
        contents: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                continue

            role = "user" if msg.role in ("user", "tool") else "model"
            parts: List[Dict[str, Any]] = []

            if msg.role == "tool" and msg.tool_call_id:
                parts.append(
                    {
                        "functionResponse": {
                            "name": msg.name or "",
                            "response": {"result": msg.content or ""},
                        }
                    }
                )
            elif msg.tool_calls:
                if msg.thinking_content:
                    parts.append({"text": msg.thinking_content, "thought": True})
                for tc in msg.tool_calls:
                    parts.append(
                        {"functionCall": {"name": tc.name, "args": tc.arguments}}
                    )
            elif msg.content:
                if msg.thinking_content:
                    parts.append({"text": msg.thinking_content, "thought": True})
                parts.append({"text": msg.content})

            if parts:
                contents.append({"role": role, "parts": parts})
        return contents
