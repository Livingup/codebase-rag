"""DashScope (Qwen) sparse embedding provider — delegates to llm-sdk.

llm-sdk's ``DashScopeSparseEmbeddingClient`` covers the
``text-embedding-v4`` / ``output_type=sparse`` endpoint, including batch
ordering, API-level error codes and the retry/error taxonomy.
"""

from __future__ import annotations

from typing import List

from pythonsdk import DashScopeSparseEmbeddingClient

from codebase_rag.common.logging import get_logger
from codebase_rag.common.types import SparseVector
from codebase_rag.services.agent.providers.llm_sdk_common import make_client_config
from codebase_rag.services.embedding.providers.base import SparseEmbeddingProvider

logger = get_logger(__name__)


class DashScopeSparseProvider(SparseEmbeddingProvider):
    """DashScope sparse embedding provider using text-embedding-v4.

    Args:
        api_base_url: DashScope API endpoint URL.
        model_name: Model name (e.g. "text-embedding-v4").
        dimension: Embedding dimension for the model.
        batch_size: Max texts per API call.
        request_timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        api_base_url: str = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        model_name: str = "text-embedding-v4",
        dimension: int = 2048,
        batch_size: int = 10,
        request_timeout: int = 30,
    ) -> None:
        self._api_url = api_base_url
        self._model_name = model_name
        self._dimension = dimension
        self._batch_size = batch_size
        self._timeout = request_timeout

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def _make_client(self, api_key: str) -> DashScopeSparseEmbeddingClient:
        return DashScopeSparseEmbeddingClient(
            make_client_config(api_key, self._api_url, self._timeout),
            model=self._model_name,
            dimension=self._dimension,
            max_batch_size=self._batch_size,
        )

    async def embed_batch(self, api_key: str, texts: List[str]) -> List[SparseVector]:
        """Generate sparse vectors with text_type=document."""
        vectors = await self._make_client(api_key).embed_texts(texts)
        return [
            SparseVector(indices=list(v["indices"]), values=list(v["values"]))
            for v in vectors
        ]

    async def embed_batch_for_query(self, api_key: str, texts: List[str]) -> List[SparseVector]:
        """Generate sparse vectors with text_type=query (for search queries)."""
        vectors = await self._make_client(api_key).embed_query(texts)
        return [
            SparseVector(indices=list(v["indices"]), values=list(v["values"]))
            for v in vectors
        ]

    async def health_check(self, api_key: str) -> bool:
        """Verify key by embedding a short test string."""
        try:
            results = await self.embed_batch(api_key, ["health check"])
            return len(results) == 1 and len(results[0].indices) > 0
        except Exception as e:
            logger.warning("dashscope_health_check_failed", error=str(e))
            return False

    async def close(self) -> None:
        """Kept for interface compatibility (llm-sdk holds no persistent client)."""
        return None
