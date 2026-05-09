from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional

from codebase_rag.services.qdrant_storage.models import PointData

KnowledgeSyncStatus = Literal["ready", "published", "cancelled"]
KnowledgeSyncSource = Literal[
    "client_submit",
    "commit_event",
    "initialization_backfill",
    "compensation_retry",
    "rename_clone",
]


@dataclass
class KnowledgeSyncJob:
    project_id: str
    file_path: str
    blob_hash: str
    file_version_id: str
    source: KnowledgeSyncSource
    points: List[PointData]
    status: KnowledgeSyncStatus = "ready"
    old_file_version_id: Optional[str] = None
    retry_count: int = 0
    last_error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class KnowledgeSyncResult:
    file_version_id: str
    points_count: int
    published: bool
    skipped_existing: bool = False
    cancelled_stale: bool = False
    error: str = ""


class KnowledgeSyncError(Exception):
    pass
