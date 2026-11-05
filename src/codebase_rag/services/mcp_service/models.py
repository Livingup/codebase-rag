"""Data models for MCP Service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


from codebase_rag.common.types import FileType


@dataclass(frozen=True)
class SearchResultPoint:
    """A single reranked search result returned to the MCP client.

    Contains only fields the client cares about:
      - score: LLM reranker relevance score (0.0~1.0)
      - file_path: absolute path (project_path + relative)
      - business_description: full business description text
      - file_type: "text" | "image"
    """

    score: float
    file_path: str
    business_description: str
    file_type: FileType = "text"


@dataclass
class RerankerScoreEntry:
    """A single score entry submitted by the reranker agent."""

    id: str
    score: float


@dataclass
class CandidatePoint:
    """A candidate point from Stage 1 vector search, used as reranker input."""

    id: str                 # file_version_id — unique point identifier
    file_path: str          # relative path from Qdrant
    business_description: str
    vector_score: float     # original vector similarity score
    file_type: FileType = "text"


# =============================================================================
# Exceptions
# =============================================================================


class McpServiceError(Exception):
    """Base exception for MCP service."""


class InvalidGitRepoPathError(McpServiceError):
    """project_path is not a Git repository root (no .git entry)."""


class ProjectNotRegisteredError(McpServiceError):
    """project_id file not found at the given project_path."""


class ProjectNotIndexedError(McpServiceError):
    """Project has no active in-memory instance (not yet indexed)."""


class RerankerError(McpServiceError):
    """Reranker agent failed."""


# =============================================================================
# Knowledge query/submission result models
# =============================================================================


@dataclass
class FileKnowledgeResult:
    """Result of querying knowledge for a single file."""

    file_path: str
    descriptions: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class AddFileSuccess:
    """Successful result for a single file in add_file_knowledge."""

    file_path: str
    points_count: int
    outcome: Literal["accepted", "unchanged"] = "accepted"


@dataclass
class AddFileFailure:
    """Failure result for a single file in add_file_knowledge."""

    file_path: str
    error: str


@dataclass
class AddKnowledgeResult:
    """Aggregated result of add_file_knowledge across all files."""

    succeeded: List[AddFileSuccess] = field(default_factory=list)
    failed: List[AddFileFailure] = field(default_factory=list)
