"""Global type definitions shared across services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional, Union

FileType = Literal["text", "image"]

# Fixed token cost per inline image in gemini-embedding-2
IMAGE_TOKEN_COST: int = 258

# Maximum raw image size in bytes (20 MB, safely within 30 MB base64 JSON API cap)
MAX_IMAGE_BYTES: int = 20 * 1024 * 1024


@dataclass(frozen=True)
class MultimodalEmbedInput:
    """Input item for gemini-embedding-2 supporting text + optional image."""

    text: str
    image_bytes: Optional[bytes] = None
    mime_type: Optional[str] = None


EmbedInputItem = Union[str, MultimodalEmbedInput]


@dataclass(frozen=True)
class SparseVector:
    """Qdrant-compatible sparse vector representation (legacy/optional)."""

    indices: List[int]
    values: List[float]


@dataclass(frozen=True)
class EmbeddingResult:
    """Dense embedding result for a single knowledge point."""

    dense_vector: List[float]
    sparse_vector: Optional[SparseVector] = None


@dataclass(frozen=True)
class TokenCount:
    """Token count result."""

    total_tokens: int


@dataclass
class EmbeddingUsage:
    """Usage statistics for an embedding API call."""

    total_tokens: int = 0
    request_count: int = 0

