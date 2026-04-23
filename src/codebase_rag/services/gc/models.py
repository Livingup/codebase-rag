"""Data models for GC service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class GCResult:
    """Result of a single GC run for one project."""

    project_id: str
    scanned_stored_count: int          # file_version_ids in Qdrant
    active_count: int                  # file_version_ids referenced by active branches
    new_orphans_count: int             # newly discovered orphans this run
    expired_orphans_count: int         # orphans past grace period
    deleted_count: int                 # actually deleted this run
    skipped_count: int                 # within grace period, not deleted
    duration_seconds: float


@dataclass(frozen=True)
class OrphanDetail:
    """Detail of a single orphan file_version_id."""

    file_version_id: str
    first_marked_at: datetime
    age_days: float


@dataclass(frozen=True)
class OrphanStatus:
    """Aggregated orphan status for a project."""

    total_orphans: int
    expired_orphans: int
    pending_orphans: int
    oldest_orphan_age_days: float
    orphan_details: List[OrphanDetail] = field(default_factory=list)


@dataclass(frozen=True)
class GCRecord:
    """Historical record of a GC execution."""

    gc_id: str
    project_id: str
    trigger: str                       # "scheduled" | "manual"
    started_at: datetime
    result: GCResult


@dataclass(frozen=True)
class CleanupResult:
    """Result of cleaning up all data for an unregistered project."""

    deleted_file_version_ids: int
    deleted_points: int


# =============================================================================
# Exceptions
# =============================================================================


class GCError(Exception):
    """Base exception for GC service."""


class GCScanError(GCError):
    """Active reference scan failed (git or qdrant error)."""


class GCDeleteError(GCError):
    """Orphan deletion failed."""


class GCLockError(GCError):
    """Same project already has a GC running."""


class OrphanStoreError(GCError):
    """Orphan record file read/write failed."""
