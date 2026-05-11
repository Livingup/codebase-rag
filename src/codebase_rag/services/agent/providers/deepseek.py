from __future__ import annotations

import json
from typing import Dict, List, Optional

import openai
import tiktoken
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_message_function_tool_call_param import (
    ChatCompletionMessageFunctionToolCallParam,
    Function as ToolCallFunction,
)
from openai.types.shared_params import FunctionDefinition

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


class DeepSeekProvider(LLMProvider):
    def __init__(self, api_base_url: str, request_timeout: int = 120) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = request_timeout

    def _make_client(self, api_key: str) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
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

        extra_body = self._normalize_extra_body(thinking_config)
        kwargs: Dict = {
            "model": model,
            "messages": self._build_messages(messages),
        }

        if not self._is_thinking_enabled(extra_body):
            kwargs["temperature"] = temperature

        if max_output_tokens:
            kwargs["max_tokens"] = max_output_tokens

        if tools:
            kwargs["tools"] = [
                ChatCompletionToolParam(
                    type="function",
                    function=FunctionDefinition(
                        name=t.name,
                        description=t.description,
                        parameters=t.parameters,
                    ),
                )
                for t in tools
            ]
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        if output_schema:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            content_parts = []
            thinking_parts = []
            tool_calls_acc: Dict[int, Dict] = {}
            finish_reason_raw = "stop"
            usage_data = None

            stream = await client.chat.completions.create(
                **kwargs,
                extra_headers=headers or None,
                extra_body=extra_body,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if chunk.usage:
                    usage_data = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if chunk.choices[0].finish_reason:
                    finish_reason_raw = chunk.choices[0].finish_reason

                if delta.content:
                    content_parts.append(delta.content)

                reasoning_content = getattr(delta, "reasoning_content", None)
                if reasoning_content:
                    thinking_parts.append(reasoning_content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc_delta.id or "",
                                "name": tc_delta.function.name if tc_delta.function and tc_delta.function.name else "",
                                "arguments": "",
                            }
                        else:
                            if tc_delta.id:
                                tool_calls_acc[idx]["id"] = tc_delta.id
                            if tc_delta.function and tc_delta.function.name:
                                tool_calls_acc[idx]["name"] = tc_delta.function.name
                        if tc_delta.function and tc_delta.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments

        except openai.AuthenticationError as e:
            raise LLMAuthError(f"DeepSeek auth error: {e}") from e
        except openai.RateLimitError as e:
            raise LLMRateLimitError(f"DeepSeek rate limit: {e}") from e
        except openai.APIError as e:
            raise LLMAPIError(f"DeepSeek API error: {e}") from e

        return self._build_response_from_stream(
            content_parts, thinking_parts, tool_calls_acc,
            finish_reason_raw, usage_data,
        )

    async def health_check(self, api_key: str) -> bool:
        try:
            client = self._make_client(api_key)
            await client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[ChatCompletionUserMessageParam(role="user", content="hi")],
                max_tokens=1,
                extra_body={"thinking": {"type": "disabled"}},
            )
            return True
        except Exception:
            return False

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

    @staticmethod
    def _build_messages(messages: List[Message]) -> List[ChatCompletionMessageParam]:
        result: List[ChatCompletionMessageParam] = []
        for msg in messages:
            if msg.role == "system":
                result.append(ChatCompletionSystemMessageParam(
                    role="system",
                    content=msg.content or "",
                ))
            elif msg.role == "tool" and msg.tool_call_id:
                result.append(ChatCompletionToolMessageParam(
                    role="tool",
                    content=msg.content or "",
                    tool_call_id=msg.tool_call_id,
                ))
            elif msg.role == "assistant":
                assistant_msg = ChatCompletionAssistantMessageParam(
                    role="assistant",
                    content=msg.content,
                )
                if msg.thinking_content is not None:
                    assistant_msg["reasoning_content"] = msg.thinking_content
                if msg.tool_calls:
                    assistant_msg["tool_calls"] = [
                        ChatCompletionMessageFunctionToolCallParam(
                            id=tc.id,
                            type="function",
                            function=ToolCallFunction(
                                name=tc.name,
                                arguments=json.dumps(tc.arguments),
                            ),
                        )
                        for tc in msg.tool_calls
                    ]
                result.append(assistant_msg)
            else:
                result.append(ChatCompletionUserMessageParam(
                    role="user",
                    content=msg.content or "",
                ))
        return result

    @staticmethod
    def _build_response_from_stream(
        content_parts: list,
        thinking_parts: list,
        tool_calls_acc: Dict[int, Dict],
        finish_reason_raw: str,
        usage_data,
    ) -> LLMResponse:
        content = "".join(content_parts) if content_parts else None
        thinking_content = "".join(thinking_parts) if thinking_parts else None
        structured_output = None
        tool_calls = None

        if tool_calls_acc:
            tool_calls = []
            for idx in sorted(tool_calls_acc.keys()):
                tc = tool_calls_acc[idx]
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(
                    id=tc["id"],
                    name=tc["name"],
                    arguments=args,
                ))

        if content:
            try:
                structured_output = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                pass

        finish_map = {
            "stop": "stop",
            "length": "length",
            "tool_calls": "tool_calls",
            "content_filter": "error",
            "insufficient_system_resource": "error",
        }
        finish_reason = finish_map.get(finish_reason_raw, "stop")
        if tool_calls:
            finish_reason = "tool_calls"

        usage = TokenUsage()
        if usage_data:
            cache_read = getattr(usage_data, "prompt_cache_hit_tokens", 0) or 0
            ptd = getattr(usage_data, "prompt_tokens_details", None)
            if ptd:
                cache_read = getattr(ptd, "cached_tokens", cache_read) or cache_read
            usage = TokenUsage(
                prompt_tokens=usage_data.prompt_tokens or 0,
                completion_tokens=usage_data.completion_tokens or 0,
                total_tokens=usage_data.total_tokens or 0,
                cache_read_tokens=cache_read,
            )

        return LLMResponse(
            content=content,
            thinking_content=thinking_content,
            tool_calls=tool_calls,
            structured_output=structured_output,
            finish_reason=finish_reason,
            usage=usage,
        )
