"""Abstract base class for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from codebase_rag.services.agent.models import LLMResponse, Message, ToolSchema


class LLMProvider(ABC):
    """LLM provider abstraction. Each provider implements this interface."""

    @abstractmethod
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
        """Execute a single chat completion call."""
        ...

    @abstractmethod
    async def health_check(self, api_key: str) -> bool:
        """Check if a key is valid and the provider is reachable."""
        ...

    async def count_tokens(self, text: str, model: str, api_key: str = "") -> int:
        """Count tokens for text. Subclasses should override with accurate implementation."""
        return max(1, len(text) // 4)
