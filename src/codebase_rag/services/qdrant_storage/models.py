"""Data models for Qdrant Storage Service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from codebase_rag.common.types import SparseVector


@dataclass
class PointData:
    """Data for a single Point to be stored in Qdrant.

    Represents one chunk of an Agent-generated business description
    for a file version.
    """

    file_path: str
    entity_type: str          # 'module' | 'class' | 'file'
    entity_name: str
    business_description: str
    blob_hash: str
    dense_vector: List[float]
    sparse_vector: SparseVector


@dataclass
class BatchResult:
    """Result of a batch operation (upsert or delete)."""

    succeeded: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)

    @property
    def all_succeeded(self) -> bool:
        return len(self.failed) == 0
