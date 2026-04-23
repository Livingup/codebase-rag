"""Gemini dense embedding provider.

API Reference:
  - Embeddings: https://ai.google.dev/api/embeddings
  - Tokens: https://ai.google.dev/api/tokens

Endpoints:
  - POST /v1beta/models/{model}:batchEmbedContents?key={API_KEY}
  - POST /v1beta/models/{model}:countTokens?key={API_KEY}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from codebase_rag.common.logging import get_logger
from codebase_rag.services.embedding.providers.base import DenseEmbeddingProvider

logger = get_logger(__name__)


class GeminiDenseProvider(DenseEmbeddingProvider):
    """Gemini dense embedding provider using batchEmbedContents API.

    Args:
        api_base_url: Gemini API base URL.
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
        self._client = httpx.AsyncClient(timeout=request_timeout)

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def embed_batch(self, api_key: str, texts: List[str]) -> List[List[float]]:
        """Generate dense vectors via Gemini batchEmbedContents API.

        Request format:
        {
          "requests": [
            {
              "model": "models/{model_name}",
              "content": { "parts": [{ "text": "..." }] },
              "outputDimensionality": null,
              "taskType": "RETRIEVAL_DOCUMENT"
            }
          ]
        }
        """
        url = (
            f"{self._api_base_url}/v1beta/models/{self._model_name}"
            f":batchEmbedContents"
        )

        requests_payload: List[Dict[str, Any]] = []
        for text in texts:
            req: Dict[str, Any] = {
                "model": f"models/{self._model_name}",
                "content": {"parts": [{"text": text}]},
                "taskType": "RETRIEVAL_DOCUMENT",
            }
            if self._dimension is not None:
                req["outputDimensionality"] = self._dimension
            requests_payload.append(req)

        body = {"requests": requests_payload}

        response = await self._client.post(
            url,
            json=body,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

        embeddings = data.get("embeddings", [])
        if len(embeddings) != len(texts):
            raise ValueError(
                f"Gemini returned {len(embeddings)} embeddings "
                f"for {len(texts)} inputs"
            )

        return [emb["values"] for emb in embeddings]

    async def count_tokens(self, api_key: str, text: str) -> int:
        """Count tokens via Gemini countTokens API.

        Request format:
        {
          "contents": [{ "parts": [{ "text": "..." }] }]
        }
        """
        url = (
            f"{self._api_base_url}/v1beta/models/{self._model_name}"
            f":countTokens"
        )

        body = {
            "contents": [{"parts": [{"text": text}]}],
        }

        response = await self._client.post(
            url,
            json=body,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()
        return data.get("totalTokens", 0)

    async def health_check(self, api_key: str) -> bool:
        """Verify key by counting tokens on a short test string."""
        try:
            tokens = await self.count_tokens(api_key, "health check")
            return tokens > 0
        except Exception as e:
            logger.warning("gemini_health_check_failed", error=str(e))
            return False

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
