"""DashScope (Qwen) sparse embedding provider.

API Reference:
  - DashScope Embedding API: https://help.aliyun.com/zh/model-studio/embedding-api

Endpoint:
  - POST https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding

Request format:
{
  "model": "text-embedding-v4",
  "input": { "texts": ["text1", "text2"] },
  "parameters": {
    "text_type": "document",
    "dimension": 2048,
    "output_type": "sparse"
  }
}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from codebase_rag.common.logging import get_logger
from codebase_rag.common.types import SparseVector
from codebase_rag.services.embedding.providers.base import SparseEmbeddingProvider

logger = get_logger(__name__)


class DashScopeSparseProvider(SparseEmbeddingProvider):
    """DashScope sparse embedding provider using text-embedding-v4.

    Uses output_type=sparse to get sparse vectors for BM25-style keyword matching.

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
        self._client = httpx.AsyncClient(timeout=request_timeout, trust_env=False)

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def embed_batch(self, api_key: str, texts: List[str]) -> List[SparseVector]:
        """Generate sparse vectors via DashScope API.

        Response format:
        {
          "output": {
            "embeddings": [
              {
                "text_index": 0,
                "sparse_embedding": [
                  {"index": 7149, "value": 0.829, "token": "..."}
                ]
              }
            ]
          },
          "usage": { "total_tokens": 123 }
        }
        """
        body: Dict[str, Any] = {
            "model": self._model_name,
            "input": {"texts": texts},
            "parameters": {
                "text_type": "document",
                "dimension": self._dimension,
                "output_type": "sparse",
            },
        }

        response = await self._client.post(
            self._api_url,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

        # Check for API-level errors
        if data.get("code"):
            raise RuntimeError(
                f"DashScope API error: code={data.get('code')}, "
                f"message={data.get('message')}"
            )

        output = data.get("output", {})
        embeddings_data = output.get("embeddings", [])

        if len(embeddings_data) != len(texts):
            raise ValueError(
                f"DashScope returned {len(embeddings_data)} embeddings "
                f"for {len(texts)} inputs"
            )

        # Sort by text_index to ensure correct ordering
        embeddings_data.sort(key=lambda x: x.get("text_index", 0))

        results: List[SparseVector] = []
        for emb in embeddings_data:
            sparse_data = emb.get("sparse_embedding", [])
            indices = [item["index"] for item in sparse_data]
            values = [item["value"] for item in sparse_data]
            results.append(SparseVector(indices=indices, values=values))

        return results

    async def embed_batch_for_query(self, api_key: str, texts: List[str]) -> List[SparseVector]:
        """Generate sparse vectors with text_type=query (for search queries)."""
        body: Dict[str, Any] = {
            "model": self._model_name,
            "input": {"texts": texts},
            "parameters": {
                "text_type": "query",
                "dimension": self._dimension,
                "output_type": "sparse",
            },
        }

        response = await self._client.post(
            self._api_url,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

        if data.get("code"):
            raise RuntimeError(
                f"DashScope API error: code={data.get('code')}, "
                f"message={data.get('message')}"
            )

        output = data.get("output", {})
        embeddings_data = output.get("embeddings", [])
        embeddings_data.sort(key=lambda x: x.get("text_index", 0))

        results: List[SparseVector] = []
        for emb in embeddings_data:
            sparse_data = emb.get("sparse_embedding", [])
            indices = [item["index"] for item in sparse_data]
            values = [item["value"] for item in sparse_data]
            results.append(SparseVector(indices=indices, values=values))

        return results

    async def health_check(self, api_key: str) -> bool:
        """Verify key by embedding a short test string."""
        try:
            results = await self.embed_batch(api_key, ["health check"])
            return len(results) == 1 and len(results[0].indices) > 0
        except Exception as e:
            logger.warning("dashscope_health_check_failed", error=str(e))
            return False

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
