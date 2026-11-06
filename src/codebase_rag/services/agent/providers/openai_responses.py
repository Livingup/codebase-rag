"""OpenAI Responses API provider — delegates protocol handling to llm-sdk."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import tiktoken
from pythonsdk import OpenAIResponsesClient, OpenAIResponsesRequest

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


class OpenAIResponsesProvider(LLMProvider):
    """OpenAI Responses API via llm-sdk (stateless multi-turn, store=False)."""

    def __init__(self, api_base_url: str, request_timeout: int = 120) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = request_timeout

    def _make_client(self, api_key: str) -> OpenAIResponsesClient:
        return OpenAIResponsesClient(
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

        request = OpenAIResponsesRequest(
            model=model,
            input=self._build_input(messages),
            instructions=self._build_instructions(messages),
            tools=self._build_tools(tools),
            tool_choice=tool_choice if tools and tool_choice else None,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            prompt_cache_key=prompt_cache_key,
            store=False,
            extra_fields=thinking_config or {},
        )

        if output_schema:
            request.text = {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "schema": output_schema,
                    "strict": True,
                }
            }

        return await collect_stream(
            client.stream(request, make_request_options(custom_headers, session_id)),
            provider_label="OpenAI Responses",
        )

    async def count_tokens(self, text: str, model: str, api_key: str = "") -> int:
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_instructions(messages: List[Message]) -> Optional[str]:
        parts: List[str] = []
        for msg in messages:
            if msg.role != "system":
                continue
            if isinstance(msg.content, str):
                parts.append(msg.content)
            elif isinstance(msg.content, list):
                parts.extend(
                    str(block.get("text", ""))
                    for block in msg.content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
        value = "\n\n".join(part for part in parts if part)
        return value or None

    @staticmethod
    def _convert_content(content: Any, role: str) -> Any:
        if not isinstance(content, list):
            return content
        converted: List[Dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                converted.append(
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": str(block.get("text", "")),
                    }
                )
            elif block_type == "image_url":
                image_value = block.get("image_url") or {}
                image_url = (
                    image_value.get("url", "")
                    if isinstance(image_value, dict)
                    else str(image_value)
                )
                if image_url:
                    image_part: Dict[str, Any] = {
                        "type": "input_image", "image_url": image_url,
                    }
                    if isinstance(image_value, dict) and image_value.get("detail"):
                        image_part["detail"] = image_value["detail"]
                    converted.append(image_part)
            else:
                converted.append(block)
        return converted

    @staticmethod
    def _build_input(messages: List[Message]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                continue

            if msg.role == "tool" and msg.tool_call_id:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.tool_call_id,
                        "output": msg.content or "",
                    }
                )
                continue

            assistant_tool_calls = []
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    assistant_tool_calls.append(
                        {
                            "type": "function_call",
                            "call_id": tc.id,
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                            "status": "completed",
                        }
                    )

            if msg.content or msg.role != "assistant":
                items.append(
                    {
                        "type": "message",
                        "role": msg.role,
                        "content": OpenAIResponsesProvider._convert_content(
                            msg.content or "", msg.role,
                        ),
                    }
                )
            items.extend(assistant_tool_calls)
        return items

    @staticmethod
    def _build_tools(tools: Optional[List[ToolSchema]]) -> Optional[List[Dict[str, Any]]]:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in tools
        ]
