"""Abstract base classes for embedding providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Sequence

from codebase_rag.common.types import EmbedInputItem


class DenseEmbeddingProvider(ABC):
    """Abstract interface for dense vector embedding providers.

    Each provider implementation handles a single API call with a given key.
    Key scheduling is managed by KeyPool at the service layer.
    """

    @abstractmethod
    async def embed_batch(
        self,
        api_key: str,
        inputs: Sequence[EmbedInputItem],
        is_query: bool = False,
    ) -> List[List[float]]:
        """Generate dense vectors for a batch of text or multimodal inputs.

        Args:
            api_key: API key to use for this call.
            inputs: Batch of texts or MultimodalEmbedInput items to embed.
            is_query: Whether the inputs are search queries (RETRIEVAL_QUERY)
                or documents (RETRIEVAL_DOCUMENT).

        Returns:
            List of dense vectors, one per input item.
        """
        ...

    @abstractmethod
    async def count_tokens(self, api_key: str, text: str) -> int:
        """Count tokens in a text using the provider's tokenizer.

        Args:
            api_key: API key to use for this call.
            text: Text to count tokens for.

        Returns:
            Token count.
        """
        ...

    @abstractmethod
    async def health_check(self, api_key: str) -> bool:
        """Verify API key is valid and service is reachable.

        Args:
            api_key: API key to test.

        Returns:
            True if the key is valid and service responds correctly.
        """
        ...

