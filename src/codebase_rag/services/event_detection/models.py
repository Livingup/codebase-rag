"""Data models for event detection service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, List, Optional
from enum import Enum


class EventType(str, Enum):
    """Type of Git HEAD change event."""

    BRANCH_SWITCH = "BRANCH_SWITCH"
    CONTENT_COMMIT = "CONTENT_COMMIT"


class EventSource(str, Enum):
    """Source that triggered the event detection."""

    HOOK = "hook"
    WATCHER = "watcher"


class HookType(str, Enum):
    """Supported Git hook types."""

    POST_COMMIT = "post-commit"
    POST_MERGE = "post-merge"
    POST_CHECKOUT = "post-checkout"
    POST_REWRITE = "post-rewrite"


class HookInstallStatus(str, Enum):
    """Installation status of a single hook script."""

    INSTALLED = "installed"
    MISSING = "missing"
    MODIFIED = "modified"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class HookEvent:
    """Raw event received from a Git hook script."""

    project_id: str
    hook_type: str
    args: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class GitEvent:
    """Structured event published to the Workflow Orchestrator."""

    event_id: str
    project_id: str
    event_type: EventType
    source: EventSource
    branch_name: Optional[str]
    commit_hash: str
    previous_branch_name: Optional[str]
    previous_commit_hash: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class HookStatus:
    """Installation status of all managed hooks for a project."""

    post_commit: HookInstallStatus = HookInstallStatus.MISSING
    post_merge: HookInstallStatus = HookInstallStatus.MISSING
    post_checkout: HookInstallStatus = HookInstallStatus.MISSING
    post_rewrite: HookInstallStatus = HookInstallStatus.MISSING


@dataclass
class ProjectHeadState:
    """Tracked HEAD state for a registered project."""

    project_id: str
    git_repo_path: str
    branch_name: Optional[str] = None
    commit_hash: Optional[str] = None
    watcher_active: bool = False
    last_event_time: Optional[datetime] = None


@dataclass
class ProjectHeadStatus:
    """Full status of a monitored project (for external queries)."""

    project_id: str
    git_repo_path: str
    branch_name: Optional[str]
    commit_hash: Optional[str]
    hooks_status: HookStatus
    watcher_active: bool
    last_event_time: Optional[datetime]


# Type alias for event subscriber callbacks
GitEventCallback = Callable[[GitEvent], Awaitable[None]]
