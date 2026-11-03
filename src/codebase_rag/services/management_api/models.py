"""Data models for Management API Service.

Pydantic models for request/response validation + internal data types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# =============================================================================
# Internal data types
# =============================================================================


@dataclass
class ProjectRecord:
    """In-memory record of one registered local repository copy.

    The registry is keyed by ``repo_key`` (canonical normalized path);
    multiple copies of the same project share one ``project_id`` but hold
    distinct repo_keys.
    """

    project_id: str
    git_repo_path: str
    display_name: str
    repo_key: str = ""
    registered_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ProjectRegistrationStatus:
    """Registration facts for MCP callers, not a search-readiness probe."""

    git_repo_path: str
    registered: bool
    initialization_running: bool = False


# =============================================================================
# Request models
# =============================================================================


class RegisterProjectRequest(BaseModel):
    """POST /projects — register a new project."""

    git_repo_path: str
    display_name: str = ""


class BatchRegisterProjectRequest(BaseModel):
    """POST /projects/batch — batch register multiple projects."""

    projects: List[RegisterProjectRequest] = Field(
        ..., min_length=1, max_length=50,
    )


class UnregisterRepoRequest(BaseModel):
    """DELETE /repos — unregister a single local repository copy."""

    git_repo_path: str


class SearchRequest(BaseModel):
    """POST /projects/{project_id}/search — semantic search."""

    query: str
    top_k: int = Field(default=10, ge=1, le=50)
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    filters: Optional[SearchFilters] = None


class SearchFilters(BaseModel):
    """Optional search filters."""

    entity_type: Optional[str] = None
    file_path_prefix: Optional[str] = None


# Fix forward reference
SearchRequest.model_rebuild()


class RebuildRequest(BaseModel):
    """POST /projects/{project_id}/rebuild — trigger full rebuild."""

    force: bool = False


class UpdateConfigRequest(BaseModel):
    """PUT /config — update a config value at runtime."""

    path: str
    value: Any


# =============================================================================
# Response models
# =============================================================================


class ProjectResponse(BaseModel):
    """Single project info in API responses."""

    project_id: str
    git_repo_path: str
    display_name: str
    status: str
    branch: Optional[str] = None
    commit_hash: Optional[str] = None
    knowledge_points: int = 0


class ProjectDetailResponse(ProjectResponse):
    """Extended project info with event/workflow details."""

    hooks_status: Optional[Dict[str, Any]] = None
    watcher_active: bool = False
    last_event_time: Optional[str] = None
    workflow_status: Optional[Dict[str, Any]] = None


class BatchRegisterResultItem(BaseModel):
    """Result for a single project in a batch registration."""

    git_repo_path: str
    success: bool
    project: Optional[ProjectResponse] = None
    error: Optional[str] = None


class BatchRegisterProjectResponse(BaseModel):
    """POST /projects/batch response."""

    total: int
    succeeded: int
    failed: int
    results: List[BatchRegisterResultItem]


class DeleteProjectResponse(BaseModel):
    """DELETE /projects/{project_id} — unregister every copy of a partition.

    Persistent Qdrant points are always preserved for reuse.
    """

    project_id: str
    unregistered_repos: int = 0
    deleted_points: int = 0
    status: str = "unregistered"


class UnregisterRepoResponse(BaseModel):
    """DELETE /repos — unregister one local repository copy.

    Persistent Qdrant points are always preserved for reuse.
    """

    git_repo_path: str
    project_id: str
    deleted_points: int = 0
    status: str = "unregistered"


class SearchResultItem(BaseModel):
    """A single search result."""

    score: float
    file_path: str
    entity_type: str
    entity_name: str
    business_description: str
    file_version_id: str


class SearchResponse(BaseModel):
    """POST /search response."""

    results: List[SearchResultItem]
    total: int
    branch: Optional[str] = None
    commit_hash: Optional[str] = None


class TaskResponse(BaseModel):
    """Async task response for long-running operations."""

    task_id: str
    status: str = "started"
    message: str = ""


class ProjectStatusResponse(BaseModel):
    """GET /projects/{project_id}/status response."""

    project_id: str
    index_status: str
    branch: Optional[str] = None
    commit_hash: Optional[str] = None
    total_files: int = 0
    indexed_files: int = 0
    failed_files: int = 0
    total_knowledge_points: int = 0
    last_sync_time: Optional[str] = None
    workflow_progress: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    """GET /health response."""

    status: str
    services: Dict[str, str]
    version: str = "1.0.0"
    uptime_seconds: float = 0.0


class ClientStatusItem(BaseModel):
    """Single external client availability item for the admin dashboard."""

    id: str
    name: str
    kind: str
    status: str
    provider: Optional[str] = None
    endpoint: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    key_status: List[Dict[str, Any]] = Field(default_factory=list)
    live_probe: Optional[Dict[str, Any]] = None


class ClientStatusResponse(BaseModel):
    """GET /status/clients response used by the local admin dashboard."""

    status: str
    generated_at: str
    live_probe: bool = False
    proxy_environment: Dict[str, Any] = Field(default_factory=dict)
    clients: List[ClientStatusItem] = Field(default_factory=list)


class ConfigResponse(BaseModel):
    """GET /config response (sanitized)."""

    config: Dict[str, Any]


class ReloadConfigResponse(BaseModel):
    """POST /config/reload response."""

    reloaded: bool
    message: str = ""
    requires_restart: bool = False
    restart_reasons: List[str] = []
    reconciled_services: List[str] = []


class UpdateConfigResponse(BaseModel):
    """PUT /config response."""

    updated: bool
    path: str
    old_value: Any = None
    new_value: Any = None


class ErrorDetail(BaseModel):
    """Standard error response body."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """Standard error envelope."""

    error: ErrorDetail


# =============================================================================
# Exceptions
# =============================================================================


class ManagementApiError(Exception):
    """Base exception for Management API service."""


class ProjectNotFoundError(ManagementApiError):
    """Project not registered."""


class ProjectExistsError(ManagementApiError):
    """Project already registered with a different path."""


class InvalidRequestError(ManagementApiError):
    """Request validation failed beyond Pydantic checks."""


class ServiceUnavailableError(ManagementApiError):
    """A required downstream service is not available."""
