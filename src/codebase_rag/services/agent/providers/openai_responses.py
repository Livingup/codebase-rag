"""OpenAI Responses API provider — uses official openai SDK.

The newer OpenAI API (2024+). Uses client.responses.create().
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

import openai
import tiktoken
from openai.types.responses import (
    EasyInputMessageParam,
    FunctionToolParam,
    ResponseFunctionToolCallParam,
    ResponseInputItemParam,
)
from openai.types.responses.response_input_item_param import FunctionCallOutput

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


class OpenAIResponsesProvider(LLMProvider):
    """OpenAI Responses API via official SDK."""

    def __init__(self, api_base_url: str, request_timeout: int = 120) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = request_timeout

    def _make_client(self, api_key: str) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            api_key=api_key,
            base_url=f"{self._api_base_url}/v1",
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

        # Client-managed context: store=false + full input every request.
        # Server does not retain conversation state.
        input_items = self._build_input(messages)

        kwargs: Dict = {
            "model": model,
            "input": input_items,
            "temperature": temperature,
            "store": False,
        }

        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key

        if max_output_tokens:
            kwargs["max_output_tokens"] = max_output_tokens

        # Thinking — override via extra_body (SDK custom field)
        extra_body = thinking_config if thinking_config else None

        # System instruction
        system_msgs = [m for m in messages if m.role == "system"]
        if system_msgs:
            kwargs["instructions"] = system_msgs[0].content

        if tools:
            kwargs["tools"] = [
                FunctionToolParam(
                    type="function",
                    name=t.name,
                    description=t.description,
                    parameters=t.parameters,
                    strict=None,
                )
                for t in tools
            ]
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        if output_schema:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "schema": output_schema,
                    "strict": True,
                }
            }

        try:
            # Use streaming — many proxies only support streamed responses.
            # Accumulate output items from stream events because some proxies
            # return an empty output array in the response.completed event.
            response = None
            accumulated_output = []
            stream = await client.responses.create(
                **kwargs, extra_headers=headers or None,
                extra_body=extra_body, stream=True,
            )
            async for event in stream:
                if hasattr(event, 'type'):
                    if event.type == "response.output_item.done":
                        accumulated_output.append(event.item)
                    elif event.type == "response.completed":
                        response = event.response
            if response is None:
                raise LLMAPIError("OpenAI Responses stream ended without response.completed event")
            # Patch output if the completed event had an empty array
            if not response.output and accumulated_output:
                response.output = accumulated_output
        except openai.AuthenticationError as e:
            raise LLMAuthError(f"OpenAI Responses auth error: {e}") from e
        except openai.RateLimitError as e:
            raise LLMRateLimitError(f"OpenAI Responses rate limit: {e}") from e
        except openai.APIError as e:
            err_str = str(e).lower()
            if ("429" in err_str or "rate limit" in err_str or "rate_limit" in err_str
                    or "concurrency" in err_str or "502" in err_str or "503" in err_str):
                raise LLMRateLimitError(f"OpenAI Responses transient error (upstream): {e}") from e
            raise LLMAPIError(f"OpenAI Responses API error: {e}") from e

        return self._parse_response(response)

    async def health_check(self, api_key: str) -> bool:
        try:
            client = self._make_client(api_key)
            await client.models.list()
            return True
        except Exception:
            return False

    async def count_tokens(self, text: str, model: str, api_key: str = "") -> int:
        """Count tokens via tiktoken (accurate for OpenAI models)."""
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_input(messages: List[Message]) -> List[ResponseInputItemParam]:
        """Convert messages to Responses API input format (full history).

        Uses SDK typed params to ensure correct serialisation.
        """
        items: List[ResponseInputItemParam] = []
        for msg in messages:
            if msg.role == "system":
                continue

            if msg.role == "tool" and msg.tool_call_id:
                items.append(FunctionCallOutput(
                    type="function_call_output",
                    call_id=msg.tool_call_id,
                    output=msg.content or "",
                ))
            elif msg.tool_calls:
                for tc in msg.tool_calls:
                    items.append(ResponseFunctionToolCallParam(
                        type="function_call",
                        call_id=tc.id,
                        name=tc.name,
                        arguments=json.dumps(tc.arguments),
                        status="completed",
                    ))
            else:
                items.append(EasyInputMessageParam(
                    role=msg.role,  # type: ignore[arg-type]
                    content=msg.content or "",
                ))
        return items

    @staticmethod
    def _parse_response(response) -> LLMResponse:
        """Parse Responses API response object."""
        output = response.output or []
        status = response.status or "completed"

        text_parts = []
        thinking_parts = []
        tool_calls = []
        structured_output = None

        for item in output:
            if item.type == "reasoning":
                if hasattr(item, 'summary') and item.summary:
                    for s in item.summary:
                        if hasattr(s, 'text'):
                            thinking_parts.append(s.text)
            elif item.type == "message":
                for content_block in item.content:
                    if content_block.type == "output_text":
                        text_parts.append(content_block.text)
            elif item.type == "function_call":
                try:
                    args = json.loads(item.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(
                    id=getattr(item, 'call_id', None) or item.id or "",
                    name=item.name or "",
                    arguments=args,
                ))

        content = "\n".join(text_parts) if text_parts else None
        thinking_content = "\n".join(thinking_parts) if thinking_parts else None

        if content:
            try:
                structured_output = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                pass

        if tool_calls:
            finish_reason = "tool_calls"
        elif status == "incomplete":
            finish_reason = "length"
        elif status == "failed":
            finish_reason = "error"
        else:
            finish_reason = "stop"

        usage = TokenUsage()
        if response.usage:
            cache_read = 0
            input_details = getattr(response.usage, 'input_tokens_details', None)
            if input_details:
                cache_read = getattr(input_details, 'cached_tokens', 0) or 0
            usage = TokenUsage(
                prompt_tokens=response.usage.input_tokens or 0,
                completion_tokens=response.usage.output_tokens or 0,
                total_tokens=(response.usage.input_tokens or 0) + (response.usage.output_tokens or 0),
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
