"""Global type definitions shared across services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class SparseVector:
    """Qdrant-compatible sparse vector representation."""

    indices: List[int]
    values: List[float]


@dataclass(frozen=True)
class EmbeddingResult:
    """Combined dense + sparse embedding for a single text."""

    dense_vector: List[float]
    sparse_vector: SparseVector


@dataclass(frozen=True)
class TokenCount:
    """Token count result."""

    total_tokens: int


@dataclass
class EmbeddingUsage:
    """Usage statistics for an embedding API call."""

    total_tokens: int = 0
    request_count: int = 0
