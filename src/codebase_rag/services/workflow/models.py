"""Data models for Workflow Orchestrator Service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


# =============================================================================
# Result models
# =============================================================================


@dataclass(frozen=True)
class BranchSwitchResult:
    """Result of a branch switch workflow."""

    cache_hit: bool
    diff_added: int                    # new file_version_ids added to in-memory
    diff_removed: int                  # file_version_ids removed from in-memory
    diff_unchanged: int                # unchanged file_version_ids
    duration_ms: int


@dataclass(frozen=True)
class FailedFile:
    """A file that failed during knowledge production."""

    file_path: str
    error: str


@dataclass(frozen=True)
class ContentCommitResult:
    """Result of a content commit (knowledge production) workflow."""

    added_files: int
    modified_files: int
    deleted_files: int
    renamed_files: int = 0             # pure renames (cloned, no LLM needed)
    skipped_files: int = 0             # content-addressing dedup skipped
    cache_hit_files: int = 0           # knowledge sync cache hits (skip Agent + Embedding)
    failed_files: List[FailedFile] = field(default_factory=list)
    total_points_written: int = 0
    total_agent_calls: int = 0
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class InitializationResult:
    """Result of a full initialization workflow."""

    total_files: int
    processed_files: int
    skipped_files: int                 # content-addressing dedup skipped
    failed_files: List[FailedFile] = field(default_factory=list)
    total_points: int = 0
    duration_seconds: float = 0.0


# =============================================================================
# Workflow status models
# =============================================================================


@dataclass
class Progress:
    """Progress tracking for long-running workflows."""

    total_files: int = 0
    processed_files: int = 0
    failed_files: int = 0
    current_file: str = ""


@dataclass
class TaskInfo:
    """Information about a workflow task."""

    task_type: str                     # "branch_switch" | "content_commit" | "initialization"
    project_id: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    progress: Optional[Progress] = None


@dataclass
class WorkflowStatus:
    """Current workflow status for a project."""

    current_task: Optional[TaskInfo] = None
    queued_tasks: int = 0
    last_completed: Optional[TaskInfo] = None


# =============================================================================
# Retry queue / Dead letter queue models
# =============================================================================


@dataclass
class FailedFileRecord:
    """A file that failed knowledge production and is pending retry.

    Stored in the retry queue. The background compensation task will
    periodically attempt to re-process these files.
    """

    project_id: str
    file_path: str
    blob_hash: str
    file_version_id: str
    is_new: bool                       # True = added file, False = modified file
    old_file_version_id: Optional[str] = None  # for modified files only
    retry_count: int = 0
    last_error: str = ""
    created_at: float = 0.0           # time.time() when first recorded


@dataclass
class DeadLetterRecord:
    """A file permanently removed from the retry queue.

    Reasons: ``max_retries_exceeded`` or ``blob_not_found``.
    Kept for diagnostics — no further processing attempted.
    """

    project_id: str
    file_path: str
    blob_hash: str
    file_version_id: str
    reason: str                        # "max_retries_exceeded" | "blob_not_found"
    last_error: Optional[str] = None
    retry_count: int = 0
    created_at: float = 0.0           # when first failed
    archived_at: float = 0.0          # when moved to dead letter


# =============================================================================
# Exceptions
# =============================================================================


class WorkflowError(Exception):
    """Workflow execution failed (unrecoverable)."""


class FileProcessingError(WorkflowError):
    """Single file processing failed."""


class InitializationError(WorkflowError):
    """Full initialization failed."""


class WorkflowConflictError(WorkflowError):
    """Workflow conflict — same project already has an active workflow."""
