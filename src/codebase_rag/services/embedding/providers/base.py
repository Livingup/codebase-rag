"""Abstract base classes for embedding providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from codebase_rag.common.types import SparseVector


class DenseEmbeddingProvider(ABC):
    """Abstract interface for dense vector embedding providers.

    Each provider implementation handles a single API call with a given key.
    Key scheduling is managed by KeyPool at the service layer.
    """

    @abstractmethod
    async def embed_batch(self, api_key: str, texts: List[str]) -> List[List[float]]:
        """Generate dense vectors for a batch of texts.

        Args:
            api_key: API key to use for this call.
            texts: Batch of texts to embed.

        Returns:
            List of dense vectors, one per input text.
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


class SparseEmbeddingProvider(ABC):
    """Abstract interface for sparse vector embedding providers."""

    @abstractmethod
    async def embed_batch(self, api_key: str, texts: List[str]) -> List[SparseVector]:
        """Generate sparse vectors for a batch of texts.

        Args:
            api_key: API key to use for this call.
            texts: Batch of texts to embed.

        Returns:
            List of SparseVector, one per input text.
        """
        ...

    @abstractmethod
    async def health_check(self, api_key: str) -> bool:
        """Verify API key is valid and service is reachable."""
        ...
