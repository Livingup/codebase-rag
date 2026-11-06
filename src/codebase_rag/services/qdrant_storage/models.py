"""Data models for Qdrant Storage Service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from codebase_rag.common.types import FileType


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
    file_type: FileType = "text"

