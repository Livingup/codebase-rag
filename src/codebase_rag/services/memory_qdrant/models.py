"""Data models for Memory Qdrant Service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from codebase_rag.common.types import FileType


@dataclass(frozen=True)
class SearchFilters:
    """Optional filters for vector search."""

    entity_types: Optional[List[str]] = None
    file_paths: Optional[List[str]] = None
    entity_names: Optional[List[str]] = None


@dataclass(frozen=True)
class SearchResult:
    """A single search result from vector search."""

    score: float
    file_version_id: str
    file_path: str
    entity_type: str
    entity_name: str
    business_description: str
    file_type: FileType = "text"


@dataclass(frozen=True)
class ActiveInstanceInfo:
    """Info about the currently active instance for a project."""

    branch_name: str
    commit_hash: str
    point_count: int
    memory_usage_mb: float


@dataclass
class CacheEntry:
    """Status of a cached branch instance."""

    branch_name: str
    commit_hash: str
    point_count: int
    last_accessed: datetime
    is_active: bool
