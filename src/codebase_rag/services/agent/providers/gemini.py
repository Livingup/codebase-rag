"""Gemini LLM provider — uses official google-genai SDK."""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from google import genai
from google.genai import types as genai_types

from codebase_rag.common.logging import get_logger
from codebase_rag.services.agent.errors import LLMAPIError, LLMAuthError, LLMRateLimitError
from codebase_rag.services.agent.models import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from codebase_rag.services.agent.providers.base import LLMProvider, close_stream

logger = get_logger(__name__)


class GeminiLLMProvider(LLMProvider):
    """Google Gemini API provider via google-genai SDK."""

    def __init__(self, api_base_url: str, request_timeout: int = 120) -> None:
        self._api_base_url = api_base_url
        self._timeout = request_timeout

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
        headers = dict(custom_headers or {})
        if session_id:
            headers["X-Relay-Session-Id"] = session_id
        http_opts_kwargs: dict = {}
        if self._api_base_url:
            http_opts_kwargs["baseUrl"] = self._api_base_url
        if headers:
            http_opts_kwargs["headers"] = headers
        client_kwargs: dict = {"api_key": api_key}
        if http_opts_kwargs:
            client_kwargs["http_options"] = genai_types.HttpOptions(**http_opts_kwargs)
        client = genai.Client(**client_kwargs)

        # Build contents
        contents = self._build_contents(messages)

        # Generation config
        gen_config = genai_types.GenerateContentConfig(
            temperature=temperature,
        )
        if max_output_tokens:
            gen_config.max_output_tokens = max_output_tokens

        # System instruction
        system_msgs = [m for m in messages if m.role == "system"]
        if system_msgs:
            gen_config.system_instruction = system_msgs[0].content

        # Thinking — extract thinkingConfig from raw nested structure
        if thinking_config:
            tc = thinking_config.get("generationConfig", {}).get("thinkingConfig", {})
            if tc:
                gen_config.thinking_config = genai_types.ThinkingConfig(**tc)

        # Structured output
        if output_schema:
            gen_config.response_mime_type = "application/json"
            gen_config.response_schema = output_schema

        # Tools
        if tools:
            declarations = []
            for t in tools:
                declarations.append(genai_types.FunctionDeclaration(
                    name=t.name,
                    description=t.description,
                    parameters=t.parameters,
                ))
            gen_config.tools = [genai_types.Tool(function_declarations=declarations)]

            # Tool choice
            _MODE_MAP = {"auto": "AUTO", "none": "NONE", "required": "ANY"}
            if tool_choice and tool_choice in _MODE_MAP:
                gen_config.tool_config = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode=_MODE_MAP[tool_choice],
                    ),
                )

        try:
            # Use streaming — force stream for all agent calls
            text_parts = []
            thinking_parts = []
            tool_calls = []
            finish_reason_raw = "STOP"
            usage_metadata = None

            stream = await client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=gen_config,
            )
            async for chunk in stream:
                if chunk.candidates:
                    candidate = chunk.candidates[0]
                    if candidate.finish_reason:
                        finish_reason_raw = candidate.finish_reason
                    if candidate.content and candidate.content.parts:
                        for part in candidate.content.parts:
                            if getattr(part, 'thought', False) and part.text:
                                thinking_parts.append(part.text)
                            elif part.text:
                                text_parts.append(part.text)
                            if part.function_call:
                                fc = part.function_call
                                tool_calls.append(ToolCall(
                                    id=fc.name,
                                    name=fc.name,
                                    arguments=dict(fc.args) if fc.args else {},
                                ))
                if chunk.usage_metadata:
                    usage_metadata = chunk.usage_metadata

        except Exception as e:
            err_str = str(e).lower()
            if "401" in err_str or "403" in err_str or "api key" in err_str:
                raise LLMAuthError(f"Gemini auth error: {e}") from e
            if "429" in err_str or "rate" in err_str or "quota" in err_str:
                raise LLMRateLimitError(f"Gemini rate limit: {e}") from e
            raise LLMAPIError(f"Gemini API error: {e}") from e

        return self._build_response_from_stream(
            text_parts, thinking_parts, tool_calls,
            finish_reason_raw, usage_metadata,
        )

    async def health_check(self, api_key: str) -> bool:
        try:
            client = genai.Client(api_key=api_key)
            async for _ in client.aio.models.list():
                return True
            return True
        except Exception:
            return False

    async def probe_stream(
        self,
        api_key: str,
        model: str,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> bool:
        headers = dict(custom_headers or {})
        http_opts_kwargs: dict = {}
        if self._api_base_url:
            http_opts_kwargs["baseUrl"] = self._api_base_url
        if headers:
            http_opts_kwargs["headers"] = headers
        client_kwargs: dict = {"api_key": api_key}
        if http_opts_kwargs:
            client_kwargs["http_options"] = genai_types.HttpOptions(**http_opts_kwargs)
        client = genai.Client(**client_kwargs)
        contents = [
            genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=".")])
        ]
        gen_config = genai_types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=1,
        )
        stream = None
        try:
            stream = await client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=gen_config,
            )
            async for chunk in stream:
                if not chunk.candidates:
                    continue
                candidate = chunk.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    continue
                for part in candidate.content.parts:
                    if part.text or part.function_call:
                        return True
            return False
        except Exception as e:
            err_str = str(e).lower()
            if "401" in err_str or "403" in err_str or "api key" in err_str:
                raise LLMAuthError(f"Gemini auth error: {e}") from e
            if "429" in err_str or "rate" in err_str or "quota" in err_str:
                raise LLMRateLimitError(f"Gemini rate limit: {e}") from e
            raise LLMAPIError(f"Gemini API error: {e}") from e
        finally:
            if stream is not None:
                await close_stream(stream)

    async def count_tokens(self, text: str, model: str, api_key: str = "") -> int:
        """Count tokens via Gemini countTokens API."""
        if not api_key:
            raise ValueError("api_key required for Gemini count_tokens")
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.count_tokens(
            model=model, contents=text,
        )
        return response.total_tokens or 0

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_contents(messages: List[Message]) -> list:
        """Convert Message list to genai Content objects."""
        contents = []
        for msg in messages:
            if msg.role == "system":
                continue

            role = "user" if msg.role in ("user", "tool") else "model"
            parts = []

            if msg.role == "tool" and msg.tool_call_id:
                parts.append(genai_types.Part.from_function_response(
                    name=msg.name or "",
                    response={"result": msg.content or ""},
                ))
            elif msg.tool_calls:
                if msg.thinking_content:
                    parts.append(genai_types.Part(text=msg.thinking_content, thought=True))
                for tc in msg.tool_calls:
                    parts.append(genai_types.Part.from_function_call(
                        name=tc.name,
                        args=tc.arguments,
                    ))
            elif msg.content:
                if msg.thinking_content:
                    parts.append(genai_types.Part(text=msg.thinking_content, thought=True))
                parts.append(genai_types.Part.from_text(text=msg.content))

            if parts:
                contents.append(genai_types.Content(role=role, parts=parts))
        return contents

    @staticmethod
    def _build_response_from_stream(
        text_parts: list,
        thinking_parts: list,
        tool_calls: list,
        finish_reason_raw,
        usage_metadata,
    ) -> LLMResponse:
        """Build LLMResponse from accumulated stream chunks."""
        text = "".join(text_parts) if text_parts else None
        thinking_content = "".join(thinking_parts) if thinking_parts else None
        structured_output = None

        if text:
            try:
                structured_output = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                pass

        finish_map = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "error"}
        finish_reason = (
            "tool_calls" if tool_calls
            else finish_map.get(str(finish_reason_raw), "stop")
        )

        usage = TokenUsage()
        if usage_metadata:
            um = usage_metadata
            cache_read = getattr(um, 'cached_content_token_count', 0) or 0
            usage = TokenUsage(
                prompt_tokens=um.prompt_token_count or 0,
                completion_tokens=um.candidates_token_count or 0,
                total_tokens=um.total_token_count or 0,
                cache_read_tokens=cache_read,
            )

        return LLMResponse(
            content=text,
            thinking_content=thinking_content,
            tool_calls=tool_calls if tool_calls else None,
            structured_output=structured_output,
            finish_reason=finish_reason,
            usage=usage,
        )
