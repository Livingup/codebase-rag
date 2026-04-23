"""Git operations error types."""


class GitError(Exception):
    """Base class for all Git operation errors."""


class GitNotFoundError(GitError):
    """Git executable not found or version incompatible."""


class GitRepositoryError(GitError):
    """Target path is not a valid Git repository."""


class GitCommandError(GitError):
    """Git command exited with non-zero status."""

    def __init__(self, command: str, returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"Git command failed (exit {returncode}): {command}\n{stderr}")


class GitTimeoutError(GitError):
    """Git command execution timed out."""

    def __init__(self, command: str, timeout: int) -> None:
        self.command = command
        self.timeout = timeout
        super().__init__(f"Git command timed out after {timeout}s: {command}")


class GitRefNotFoundError(GitError):
    """Specified branch or commit hash does not exist."""

    def __init__(self, ref: str) -> None:
        self.ref = ref
        super().__init__(f"Git ref not found: {ref}")


class GitFileNotFoundError(GitError):
    """Specified file does not exist at the given ref."""

    def __init__(self, file_path: str, ref: str) -> None:
        self.file_path = file_path
        self.ref = ref
        super().__init__(f"File '{file_path}' not found at ref '{ref}'")
