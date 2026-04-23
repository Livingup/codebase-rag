"""Event detection service error types."""


class EventDetectionError(Exception):
    """Base class for all event detection errors."""


class HookInjectionError(EventDetectionError):
    """Hook script injection failed (permissions, .git/hooks/ not writable)."""

    def __init__(self, hook_type: str, repo_path: str, reason: str) -> None:
        self.hook_type = hook_type
        self.repo_path = repo_path
        self.reason = reason
        super().__init__(
            f"Failed to inject {hook_type} hook at {repo_path}: {reason}"
        )


class HookConflictError(EventDetectionError):
    """Existing user hook cannot be safely wrapped."""

    def __init__(self, hook_type: str, repo_path: str) -> None:
        self.hook_type = hook_type
        self.repo_path = repo_path
        super().__init__(
            f"Cannot wrap existing {hook_type} hook at {repo_path}: "
            "user hook format is incompatible"
        )


class WatcherError(EventDetectionError):
    """File system watcher failed to start."""

    def __init__(self, repo_path: str, reason: str) -> None:
        self.repo_path = repo_path
        self.reason = reason
        super().__init__(f"Watcher failed for {repo_path}: {reason}")


class ProjectNotRegisteredError(EventDetectionError):
    """Operation on a project that is not registered."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__(f"Project not registered: {project_id}")


class GitRepoNotFoundError(EventDetectionError):
    """.git/ directory does not exist at the given path."""

    def __init__(self, repo_path: str) -> None:
        self.repo_path = repo_path
        super().__init__(f".git/ not found at: {repo_path}")
