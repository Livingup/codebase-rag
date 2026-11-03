"""Gemini dense embedding provider — delegates protocol handling to llm-sdk."""

from __future__ import annotations

from typing import List, Optional

from pythonsdk import GeminiEmbeddingClient

from codebase_rag.common.logging import get_logger
from codebase_rag.services.agent.providers.llm_sdk_common import make_client_config
from codebase_rag.services.embedding.providers.base import DenseEmbeddingProvider

logger = get_logger(__name__)


class GeminiDenseProvider(DenseEmbeddingProvider):
    """Gemini dense embedding via llm-sdk batchEmbedContents.

    Args:
        api_base_url: Gemini API origin (``/v1beta`` is added by llm-sdk).
        model_name: Model name (e.g. "gemini-embedding-001").
        dimension: Output vector dimension (None = model default).
        batch_size: Max texts per API call.
        request_timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        api_base_url: str = "https://generativelanguage.googleapis.com",
        model_name: str = "gemini-embedding-001",
        dimension: Optional[int] = None,
        batch_size: int = 32,
        request_timeout: int = 30,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._model_name = model_name
        self._dimension = dimension
        self._batch_size = batch_size
        self._timeout = request_timeout

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def _make_client(self, api_key: str) -> GeminiEmbeddingClient:
        return GeminiEmbeddingClient(
            make_client_config(api_key, self._api_base_url, self._timeout),
            model=self._model_name,
        )

    async def embed_batch(self, api_key: str, texts: List[str]) -> List[List[float]]:
        """Generate dense vectors via Gemini batchEmbedContents API."""
        client = self._make_client(api_key)

        requests_payload: List[dict] = []
        for text in texts:
            req: dict = {
                "content": {"parts": [{"text": text}]},
                "taskType": "RETRIEVAL_DOCUMENT",
            }
            if self._dimension is not None:
                req["outputDimensionality"] = self._dimension
            requests_payload.append(req)

        response = await client.batch_embed_contents(requests_payload)
        embeddings = response.get("embeddings", [])

        if len(embeddings) != len(texts):
            raise ValueError(
                f"Gemini returned {len(embeddings)} embeddings "
                f"for {len(texts)} inputs"
            )

        return [emb["values"] for emb in embeddings]

    async def count_tokens(self, api_key: str, text: str) -> int:
        """Count tokens via Gemini countTokens API."""
        return await self._make_client(api_key).count_tokens(text)

    async def health_check(self, api_key: str) -> bool:
        """Verify key by counting tokens on a short test string."""
        try:
            tokens = await self.count_tokens(api_key, "health check")
            return tokens > 0
        except Exception as e:
            logger.warning("gemini_health_check_failed", error=str(e))
            return False

    async def close(self) -> None:
        """Kept for interface compatibility (llm-sdk holds no persistent client)."""
        return None
