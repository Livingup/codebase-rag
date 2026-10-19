"""Git operations service — structured Git data access with file filtering.

Pure data layer wrapping git CLI. No business logic.
All git commands run via asyncio.create_subprocess_exec with per-repo semaphore.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from typing import Dict, List, Optional

from codebase_rag.common.logging import get_logger
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import GitOperationsConfig
from codebase_rag.services.git_operations.errors import (
    GitCommandError,
    GitFileNotFoundError,
    GitNotFoundError,
    GitRefNotFoundError,
    GitRepositoryError,
    GitTimeoutError,
)
from codebase_rag.services.git_operations.file_filter import FileFilter, FilterResult
from codebase_rag.services.git_operations.gray_area_store import GrayAreaStore
from codebase_rag.services.git_operations.models import (
    BranchInfo,
    ChangedFiles,
    ChangedFilesResult,
    FileEntry,
    HeadInfo,
    ModifiedFileEntry,
    RenamedFileEntry,
    RepoValidation,
)

logger = get_logger(__name__)


class GitOperationsService:
    """Encapsulates all Git commands, providing structured data access.

    Args:
        config: Git operations configuration section from AppConfig.
    """

    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        self._file_filter = FileFilter(self._config.file_filter)
        self._gray_area_store = GrayAreaStore(
            self._config.file_filter.uncertain.store_path,
        )
        self._repo_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._git_version: Optional[str] = None
        self._initialized = False

    @property
    def _config(self) -> GitOperationsConfig:
        return self._config_manager.config.git_operations

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Validate git executable and prepare service."""
        logger.info("git_operations_service_initializing")

        version = await self._check_git_version()
        self._git_version = version
        self._initialized = True

        logger.info(
            "git_operations_service_initialized",
            git_version=version,
            git_executable=self._config.git_executable,
        )

    async def shutdown(self) -> None:
        """Release resources."""
        self._repo_semaphores.clear()
        self._initialized = False
        logger.info("git_operations_service_shutdown")

    def is_healthy(self) -> bool:
        return self._initialized

    @property
    def gray_area_store(self) -> GrayAreaStore:
        """Expose gray-area store for L3 classification by workflow."""
        return self._gray_area_store

    # =========================================================================
    # 2.1 Get file tree
    # =========================================================================

    async def get_file_tree(
        self, repo_path: str, ref: str, *, include_uncertain: bool = False,
    ) -> List[FileEntry]:
        """Get filtered file list from a Git ref.

        Args:
            repo_path: Absolute path to the Git repository root.
            ref: Branch name or commit hash.
            include_uncertain: If True, also include gray-area files
                (useful for loading existing data from persistent storage).

        Returns:
            List of FileEntry (files passing the filter).
        """
        stdout = await self._run_git(repo_path, ["ls-tree", "-r", "--full-tree", ref])

        # Parse: "<mode> <type> <hash>\t<path>"
        raw_paths: List[str] = []
        blob_map: Dict[str, str] = {}

        for line in stdout.splitlines():
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            meta, path = parts
            blob_hash = meta.split()[2]
            normalized = path.replace("\\", "/")
            raw_paths.append(normalized)
            blob_map[normalized] = blob_hash

        # Apply file filter
        result = self._file_filter.filter(raw_paths)

        logger.info(
            "get_file_tree_filtered",
            repo=repo_path,
            ref=ref,
            raw=len(raw_paths),
            included=len(result.included),
            excluded=len(result.excluded),
            uncertain=len(result.uncertain),
            include_uncertain=include_uncertain,
            accepted=len(result.included) + (len(result.uncertain) if include_uncertain else 0),
        )

        accepted = result.included
        if include_uncertain:
            accepted = result.included + result.uncertain

        entries = []
        for path in accepted:
            blob = blob_map[path]
            entries.append(FileEntry(
                file_path=path,
                blob_hash=blob,
                file_version_id=compute_file_version_id(path, blob),
            ))

        return entries

    def get_filter_result(self, file_paths: List[str]) -> FilterResult:
        """Expose filter result (included + uncertain) for external gray-area processing."""
        return self._file_filter.filter(file_paths)

    # =========================================================================
    # 2.2 Get changed files
    # =========================================================================

    async def get_changed_files(
        self, repo_path: str, old_ref: str, new_ref: str,
    ) -> ChangedFilesResult:
        """Get files changed between two refs, with L1+L2 filtering.

        Pure renames (R status with identical blob hash) are captured as
        ``RenamedFileEntry`` — no LLM / embedding regeneration needed.
        Renames with content changes are decomposed into delete + add.

        Returns:
            ChangedFilesResult with ``changes`` (L2 included) and
            ``uncertain`` (gray area, needs L3 classification).
        """
        stdout = await self._run_git(
            repo_path, ["diff", "--name-status", f"{old_ref}..{new_ref}"]
        )

        added_paths: List[str] = []
        modified_paths: List[str] = []
        deleted_paths: List[str] = []
        # (old_path, new_path) pairs for R status — resolved after blob check
        rename_pairs: List[tuple] = []

        for line in stdout.splitlines():
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0][0]  # First char: A, M, D, R, C, etc.

            if status == "A":
                added_paths.append(parts[1].replace("\\", "/"))
            elif status == "M":
                modified_paths.append(parts[1].replace("\\", "/"))
            elif status == "D":
                deleted_paths.append(parts[1].replace("\\", "/"))
            elif status == "R":
                old_path = parts[1].replace("\\", "/")
                new_path = parts[2].replace("\\", "/")
                rename_pairs.append((old_path, new_path))

        # Resolve rename pairs: check blob hashes to distinguish pure renames
        # from renames-with-content-change.
        pure_renames: List[tuple] = []  # (old_path, new_path, blob_hash)
        for old_path, new_path in rename_pairs:
            old_blob = await self.get_blob_hash(repo_path, old_ref, old_path)
            new_blob = await self.get_blob_hash(repo_path, new_ref, new_path)
            if old_blob and new_blob and old_blob == new_blob:
                # Pure rename: same content, only path changed
                pure_renames.append((old_path, new_path, old_blob))
            else:
                # Content also changed — decompose to D + A
                deleted_paths.append(old_path)
                added_paths.append(new_path)

        # Filter all non-rename paths (L1 blacklist + L2 whitelist)
        all_paths = set(added_paths + modified_paths + deleted_paths)
        # For renames, filter by new_path (the path going forward)
        rename_new_paths = [new_p for _, new_p, _ in pure_renames]
        all_paths.update(rename_new_paths)
        filter_result = self._file_filter.filter(list(all_paths))
        included_set = set(filter_result.included)
        uncertain_set = set(filter_result.uncertain)

        async def _build_entries(
            paths: List[str], path_set: set, ref: str,
        ) -> List[FileEntry]:
            entries: List[FileEntry] = []
            for path in paths:
                if path not in path_set:
                    continue
                blob = await self.get_blob_hash(repo_path, ref, path)
                if blob:
                    entries.append(FileEntry(
                        file_path=path,
                        blob_hash=blob,
                        file_version_id=compute_file_version_id(path, blob),
                    ))
            return entries

        async def _build_modified(
            paths: List[str], path_set: set,
        ) -> List[ModifiedFileEntry]:
            entries: List[ModifiedFileEntry] = []
            for path in paths:
                if path not in path_set:
                    continue
                old_blob = await self.get_blob_hash(repo_path, old_ref, path)
                new_blob = await self.get_blob_hash(repo_path, new_ref, path)
                if old_blob and new_blob:
                    entries.append(ModifiedFileEntry(
                        file_path=path,
                        old_blob_hash=old_blob,
                        new_blob_hash=new_blob,
                        old_file_version_id=compute_file_version_id(path, old_blob),
                        new_file_version_id=compute_file_version_id(path, new_blob),
                    ))
            return entries

        def _build_renamed(
            renames: List[tuple], path_set: set,
        ) -> List[RenamedFileEntry]:
            entries: List[RenamedFileEntry] = []
            for old_path, new_path, blob_hash in renames:
                if new_path not in path_set:
                    continue
                entries.append(RenamedFileEntry(
                    old_file_path=old_path,
                    new_file_path=new_path,
                    blob_hash=blob_hash,
                    old_file_version_id=compute_file_version_id(old_path, blob_hash),
                    new_file_version_id=compute_file_version_id(new_path, blob_hash),
                ))
            return entries

        # Build included entries
        inc_added = await _build_entries(added_paths, included_set, new_ref)
        inc_modified = await _build_modified(modified_paths, included_set)
        inc_deleted = await _build_entries(deleted_paths, included_set, old_ref)
        inc_renamed = _build_renamed(pure_renames, included_set)

        # Build uncertain entries
        unc_added = await _build_entries(added_paths, uncertain_set, new_ref)
        unc_modified = await _build_modified(modified_paths, uncertain_set)
        unc_deleted = await _build_entries(deleted_paths, uncertain_set, old_ref)
        unc_renamed = _build_renamed(pure_renames, uncertain_set)

        logger.info(
            "get_changed_files",
            repo=repo_path,
            old_ref=old_ref[:8],
            new_ref=new_ref[:8],
            included_add=len(inc_added),
            included_mod=len(inc_modified),
            included_del=len(inc_deleted),
            included_ren=len(inc_renamed),
            uncertain_add=len(unc_added),
            uncertain_mod=len(unc_modified),
            uncertain_del=len(unc_deleted),
            uncertain_ren=len(unc_renamed),
            excluded=len(filter_result.excluded),
        )

        return ChangedFilesResult(
            changes=ChangedFiles(
                added=inc_added, modified=inc_modified, deleted=inc_deleted,
                renamed=inc_renamed,
            ),
            uncertain=ChangedFiles(
                added=unc_added, modified=unc_modified, deleted=unc_deleted,
                renamed=unc_renamed,
            ),
            excluded_count=len(filter_result.excluded),
        )

    # =========================================================================
    # 2.3 Get file diff
    # =========================================================================

    async def get_file_diff(
        self, repo_path: str, old_ref: str, new_ref: str, file_path: str
    ) -> str:
        """Get unified diff for a single file between two refs."""
        return await self._run_git(
            repo_path, ["diff", f"{old_ref}..{new_ref}", "--", file_path]
        )

    # =========================================================================
    # 2.4 Get file content
    # =========================================================================

    async def get_file_content(self, repo_path: str, blob_hash: str) -> str:
        """Get file content by blob hash (UTF-8 decoded)."""
        try:
            return await self._run_git(repo_path, ["show", blob_hash])
        except GitCommandError as e:
            if "not a valid object name" in e.stderr or "does not exist" in e.stderr:
                raise GitFileNotFoundError(blob_hash, blob_hash) from e
            raise

    # =========================================================================
    # 2.5 Get HEAD info
    # =========================================================================

    async def get_head_info(self, repo_path: str) -> HeadInfo:
        """Get current HEAD commit hash and branch name."""
        commit_hash = (
            await self._run_git(repo_path, ["rev-parse", "HEAD"])
        ).strip()

        try:
            symbolic = (
                await self._run_git(repo_path, ["symbolic-ref", "--short", "HEAD"])
            ).strip()
            return HeadInfo(
                commit_hash=commit_hash,
                branch_name=symbolic,
                is_detached=False,
            )
        except GitCommandError:
            # Detached HEAD
            return HeadInfo(
                commit_hash=commit_hash,
                branch_name=None,
                is_detached=True,
            )

    # =========================================================================
    # 2.6 Get all local branches
    # =========================================================================

    async def get_all_branches(self, repo_path: str) -> List[BranchInfo]:
        """Get all local branches with their tip commit hashes."""
        stdout = await self._run_git(
            repo_path,
            ["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"],
        )

        branches: List[BranchInfo] = []
        for line in stdout.splitlines():
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                branches.append(BranchInfo(name=parts[0], commit_hash=parts[1]))

        return branches

    # =========================================================================
    # 2.7 Get root commit (deterministic project identity)
    # =========================================================================

    async def get_root_commit(self, repo_path: str) -> Optional[str]:
        """Return the deterministic root commit hash of the repository.

        Used as a stable, cross-clone identity anchor for the repository —
        independent of remote URL, branch / tag refs, working directory, or
        local config.  Any two clones of the same repository (regardless of
        URL changes, renames, mirrors, or migrations) share the same root
        commit, making this the only Git-native value that yields cross-user
        consistent project identification without external coordination.

        For repositories with multiple root commits (e.g. merged subtrees,
        ``git replace`` grafts), the lexicographically smallest hash is
        returned so the result is fully deterministic.

        Returns:
            Hex-encoded SHA-1 of the chosen root commit, or ``None`` if the
            repository has no committed history yet (e.g. freshly
            ``git init``'d with no commits).
        """
        try:
            stdout = await self._run_git(
                repo_path, ["rev-list", "--max-parents=0", "HEAD"],
            )
        except (GitCommandError, GitRefNotFoundError):
            # Empty repo: HEAD points to an unborn branch and rev-list fails
            return None

        roots = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not roots:
            return None

        # Deterministic ordering: lexicographically smallest root commit hash
        return min(roots)

    # =========================================================================
    # 2.8 Get blob hash for a single file
    # =========================================================================

    async def get_blob_hash(
        self, repo_path: str, ref: str, file_path: str
    ) -> Optional[str]:
        """Get blob hash for a specific file at a given ref.

        Returns None if the file does not exist at that ref.
        """
        clean_path = file_path.replace("\\", "/").strip().lstrip("/")
        try:
            stdout = await self._run_git(
                repo_path, ["ls-tree", ref, "--", clean_path]
            )
        except GitCommandError:
            return None

        stdout = stdout.strip()
        if not stdout:
            return None

        # "<mode> <type> <hash>\t<path>"
        parts = stdout.split("\t", 1)
        if len(parts) != 2:
            return None
        return parts[0].split()[2]

    # =========================================================================
    # 2.9 Get blob hash for a working directory file
    # =========================================================================

    async def get_workdir_blob_hash(
        self, repo_path: str, file_path: str,
    ) -> str:
        """Compute blob hash for a working directory file via git hash-object.

        Unlike get_blob_hash() which queries committed refs, this operates
        on the actual file in the working directory (potentially uncommitted).

        The returned hash is identical to what git ls-tree would show
        after the file is committed (assuming content doesn't change).

        Args:
            repo_path: Absolute path to the Git repository root.
            file_path: File path relative to the repository root.

        Returns:
            Git blob SHA-1 hash string.

        Raises:
            GitFileNotFoundError: If the file does not exist in the working directory.
            GitCommandError: If git hash-object fails.
        """
        clean_path = file_path.replace("\\", "/").strip().lstrip("/")
        abs_path = os.path.join(repo_path, clean_path)
        if not os.path.exists(abs_path):
            raise GitFileNotFoundError(clean_path, "working directory")

        stdout = await self._run_git(
            repo_path, ["hash-object", "--", clean_path],
        )
        blob_hash = stdout.strip()
        if not blob_hash:
            raise GitCommandError(
                f"git hash-object returned empty for {clean_path}", 1, "",
            )
        return blob_hash

    # =========================================================================
    # 2.10 Validate repository
    # =========================================================================

    async def validate_repository(self, repo_path: str) -> RepoValidation:
        """Check if a path is a valid Git repository."""
        try:
            git_dir = (
                await self._run_git(repo_path, ["rev-parse", "--git-dir"])
            ).strip()
            root_dir = (
                await self._run_git(repo_path, ["rev-parse", "--show-toplevel"])
            ).strip()
            return RepoValidation(
                is_valid=True,
                git_dir=git_dir,
                root_dir=root_dir,
            )
        except (GitCommandError, GitRepositoryError) as e:
            return RepoValidation(is_valid=False, error=str(e))

    # =========================================================================
    # 2.11 Compute file_version_id (also used internally)
    # =========================================================================

    # Exposed as module-level function below

    # =========================================================================
    # Internal: git command executor
    # =========================================================================

    async def _run_git(self, repo_path: str, args: List[str]) -> str:
        """Unified git command executor.

        - Builds command: [git_executable, -C, repo_path] + args
        - Per-repo semaphore for concurrency control
        - Timeout enforcement
        - Stdout/stderr capture
        """
        sem = self._get_repo_semaphore(repo_path)
        cmd = [self._config.git_executable, "-C", repo_path] + args
        cmd_str = " ".join(cmd)

        async with sem:
            start = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._config.command_timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise GitTimeoutError(cmd_str, self._config.command_timeout)

            elapsed = time.monotonic() - start
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            logger.debug(
                "git_command_executed",
                command=cmd_str,
                elapsed_ms=round(elapsed * 1000, 1),
                returncode=proc.returncode,
                stdout_lines=stdout.count("\n"),
            )

            if proc.returncode != 0:
                # Detect specific error types
                if "not a git repository" in stderr.lower():
                    raise GitRepositoryError(f"Not a Git repository: {repo_path}")
                if "unknown revision" in stderr.lower() or "bad revision" in stderr.lower():
                    # Extract ref from args
                    ref = args[1] if len(args) > 1 else "unknown"
                    raise GitRefNotFoundError(ref)

                raise GitCommandError(cmd_str, proc.returncode, stderr)

            return stdout

    def _get_repo_semaphore(self, repo_path: str) -> asyncio.Semaphore:
        """Get or create a per-repo semaphore for concurrency control."""
        if repo_path not in self._repo_semaphores:
            self._repo_semaphores[repo_path] = asyncio.Semaphore(
                self._config.max_concurrency
            )
        return self._repo_semaphores[repo_path]

    async def _check_git_version(self) -> str:
        """Verify git executable exists and version >= 2.x."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._config.git_executable, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except FileNotFoundError:
            raise GitNotFoundError(
                f"Git executable not found: {self._config.git_executable}"
            )
        except asyncio.TimeoutError:
            raise GitNotFoundError("Git version check timed out")

        version_str = stdout_bytes.decode("utf-8").strip()
        # Parse "git version 2.x.x ..."
        match = re.search(r"(\d+)\.(\d+)", version_str)
        if not match:
            raise GitNotFoundError(f"Cannot parse git version: {version_str}")

        major = int(match.group(1))
        if major < 2:
            raise GitNotFoundError(
                f"Git version >= 2.x required, got: {version_str}"
            )

        return version_str


# =============================================================================
# Module-level utility
# =============================================================================


def compute_file_version_id(file_path: str, blob_hash: str) -> str:
    """Compute file_version_id = sha256(filePath + blobHash).

    Args:
        file_path: Repository-relative path (/ separator).
        blob_hash: Git blob SHA-1 hash.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    # Normalize to / separator
    normalized = file_path.replace("\\", "/")
    return hashlib.sha256(
        (normalized + blob_hash).encode("utf-8")
    ).hexdigest()
