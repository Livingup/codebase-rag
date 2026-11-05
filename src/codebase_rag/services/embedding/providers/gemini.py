"""Gemini dense embedding provider — delegates protocol handling to llm-sdk."""

from __future__ import annotations

import base64
from typing import List, Optional, Sequence

from pythonsdk import GeminiEmbeddingClient, l2_normalize

from codebase_rag.common.logging import get_logger
from codebase_rag.common.types import EmbedInputItem, MultimodalEmbedInput
from codebase_rag.services.agent.providers.llm_sdk_common import make_client_config
from codebase_rag.services.embedding.providers.base import DenseEmbeddingProvider

logger = get_logger(__name__)


class GeminiDenseProvider(DenseEmbeddingProvider):
    """Gemini dense embedding via llm-sdk batchEmbedContents (gemini-embedding-2).

    Args:
        api_base_url: Gemini API origin (``/v1beta`` is added by llm-sdk).
        model_name: Model name (default "gemini-embedding-2").
        dimension: Output vector dimension (None = model default 3072).
        batch_size: Max items per API call.
        request_timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        api_base_url: str = "https://generativelanguage.googleapis.com",
        model_name: str = "gemini-embedding-2",
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

    @staticmethod
    def _build_content_parts(item: EmbedInputItem) -> List[dict]:
        """Build Gemini Content parts for a text or multimodal input item."""
        if isinstance(item, MultimodalEmbedInput):
            parts: List[dict] = [{"text": item.text}]
            if item.image_bytes:
                b64_data = base64.b64encode(item.image_bytes).decode("ascii")
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": item.mime_type or "image/png",
                            "data": b64_data,
                        }
                    }
                )
            return parts
        return [{"text": str(item)}]

    async def embed_batch(
        self,
        api_key: str,
        inputs: Sequence[EmbedInputItem],
        is_query: bool = False,
    ) -> List[List[float]]:
        """Generate dense vectors via Gemini batchEmbedContents API."""
        client = self._make_client(api_key)
        task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"

        requests_payload: List[dict] = []
        for item in inputs:
            req: dict = {
                "content": {"parts": self._build_content_parts(item)},
                "taskType": task_type,
            }
            if self._dimension is not None:
                req["outputDimensionality"] = self._dimension
            requests_payload.append(req)

        response = await client.batch_embed_contents(requests_payload)
        embeddings = response.get("embeddings", [])

        if len(embeddings) != len(inputs):
            raise ValueError(
                f"Gemini returned {len(embeddings)} embeddings "
                f"for {len(inputs)} inputs"
            )

        vectors = [emb["values"] for emb in embeddings]
        if self._dimension is not None and self._dimension < 3072:
            vectors = [l2_normalize(vec) for vec in vectors]
        return vectors

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

