"""Token counter utility — Embedding API fallback + local estimate.

Provides a shared token counting function with two-tier fallback:
  1. Embedding Service countTokens API (accurate, remote)
  2. Local character-based estimate (len // 4)

Used by Agent Service (Tier 2/3 after provider SDK fails) and
Workflow Service (for embedding token validation).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from codebase_rag.common.logging import get_logger

if TYPE_CHECKING:
    from codebase_rag.services.embedding.service import EmbeddingService

logger = get_logger(__name__)


class TokenCounter:
    """Token counter with Embedding API fallback.

    Args:
        embedding: Optional EmbeddingService for accurate counting.
            If None, always falls back to local estimate.
    """

    def __init__(self, embedding: Optional["EmbeddingService"] = None) -> None:
        self._embedding = embedding

    async def count(self, text: str) -> int:
        """Count tokens with fallback: Embedding API → local estimate."""
        # Tier 1: Embedding Service (Gemini countTokens API)
        if self._embedding:
            try:
                return await self._embedding.count_tokens(text)
            except Exception:
                logger.debug("token_counter_embedding_fallback")

        # Tier 2: Local estimate
        return max(1, len(text) // 4)
