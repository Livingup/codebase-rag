"""Git operations service — structured Git data access with gitignore-style file filtering.

Pure data layer wrapping git CLI. No business logic.
All git commands run via asyncio.create_subprocess_exec with per-repo semaphore.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

from codebase_rag.common.logging import get_logger
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import AppConfig, GitOperationsConfig
from codebase_rag.services.git_operations.errors import (
    GitCommandError,
    GitFileNotFoundError,
    GitNotFoundError,
    GitRefNotFoundError,
    GitRepositoryError,
    GitTimeoutError,
)
from codebase_rag.services.git_operations.file_filter import (
    FileFilter,
    FilterResult,
    classify_file_modality,
    compile_gitignore_lines,
)
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

_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"


def _parse_lfs_pointer(pointer_bytes: bytes) -> tuple[str, Optional[int]]:
    """Return the expected SHA-256 OID and optional size from an LFS pointer."""
    try:
        pointer_text = pointer_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("Invalid Git LFS pointer encoding") from e
    oid: Optional[str] = None
    size: Optional[int] = None
    for line in pointer_text.splitlines():
        if line.startswith("oid sha256:"):
            oid = line.removeprefix("oid sha256:").strip()
        elif line.startswith("size "):
            try:
                size = int(line.removeprefix("size ").strip())
            except ValueError as e:
                raise ValueError("Invalid Git LFS pointer size") from e
    if not oid or not re.fullmatch(r"[0-9a-fA-F]{64}", oid):
        raise ValueError("Git LFS pointer is missing a valid sha256 OID")
    return oid.lower(), size


def _lfs_content_matches(raw_bytes: bytes, oid: str, size: Optional[int]) -> bool:
    """Verify that materialized bytes are exactly the object named by the pointer."""
    if size is not None and len(raw_bytes) != size:
        return False
    return hashlib.sha256(raw_bytes).hexdigest() == oid


class GitOperationsService:
    """Encapsulates all Git commands, providing structured data access.

    Args:
        config_manager: Runtime configuration manager.
    """

    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        self._file_filter = FileFilter(self._config.file_filter)
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
        self._config_manager.register_reconciler("git_operations", self.reconcile)

        logger.info(
            "git_operations_service_initialized",
            git_version=version,
            git_executable=self._config.git_executable,
        )

    async def reconcile(self, old_config: AppConfig, new_config: AppConfig) -> None:
        """Reconcile file filter rules on hot reload."""
        self._file_filter.update_config(new_config.git_operations.file_filter)
        logger.info("git_operations_service_reconciled")

    async def shutdown(self) -> None:
        """Release resources."""
        self._config_manager.unregister_reconciler("git_operations")
        self._repo_semaphores.clear()
        self._initialized = False
        logger.info("git_operations_service_shutdown")

    def is_healthy(self) -> bool:
        return self._initialized

    # =========================================================================
    # Per-repository .git/codebase-rag/ignore management
    # =========================================================================

    @staticmethod
    def _resolve_git_dir(repo_path: str) -> Path:
        """Resolve the actual .git directory for a repository (supports worktrees/submodules)."""
        dot_git = Path(repo_path) / ".git"
        if dot_git.is_file():
            try:
                content = dot_git.read_text(encoding="utf-8", errors="replace").strip()
                if content.startswith("gitdir:"):
                    gitdir_raw = content[len("gitdir:") :].strip()
                    gitdir_path = Path(gitdir_raw)
                    if not gitdir_path.is_absolute():
                        gitdir_path = (Path(repo_path) / gitdir_path).resolve()
                    return gitdir_path
            except OSError:
                pass
        return dot_git

    def get_ignore_file_path(self, repo_path: str) -> str:
        """Return absolute path to ``.git/codebase-rag/ignore`` for a repository."""
        git_dir = self._resolve_git_dir(repo_path)
        return str(git_dir / "codebase-rag" / "ignore")

    def get_ignore_content(self, repo_path: str) -> str:
        """Read raw content of ``.git/codebase-rag/ignore`` (returns empty string if absent)."""
        ignore_path = Path(self.get_ignore_file_path(repo_path))
        if not ignore_path.is_file():
            return ""
        try:
            return ignore_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def get_ignore_rules(self, repo_path: str) -> List[str]:
        """Return non-empty, non-comment rule lines from ``.git/codebase-rag/ignore``."""
        content = self.get_ignore_content(repo_path)
        rules: List[str] = []
        for raw_line in content.splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#"):
                rules.append(stripped)
        return rules

    def set_ignore_content(self, repo_path: str, content: str) -> List[str]:
        """Atomically overwrite ``.git/codebase-rag/ignore`` and return active rules."""
        ignore_path = Path(self.get_ignore_file_path(repo_path))
        ignore_path.parent.mkdir(parents=True, exist_ok=True)
        normalized = content.replace("\r\n", "\n")
        if normalized and not normalized.endswith("\n"):
            normalized += "\n"
        tmp_path = ignore_path.with_suffix(".tmp")
        tmp_path.write_text(normalized, encoding="utf-8")
        os.replace(tmp_path, ignore_path)
        return self.get_ignore_rules(repo_path)

    def add_ignore_rules(self, repo_path: str, patterns: List[str]) -> List[str]:
        """Append new ignore rules to ``.git/codebase-rag/ignore`` (deduplicated)."""
        existing_content = self.get_ignore_content(repo_path)
        existing_lines = existing_content.splitlines()
        existing_active = {
            line.strip()
            for line in existing_lines
            if line.strip() and not line.strip().startswith("#")
        }
        to_add: List[str] = []
        for pat in patterns:
            for subline in pat.splitlines():
                cleaned = subline.strip()
                if not cleaned:
                    continue
                if cleaned not in existing_active and cleaned not in to_add:
                    to_add.append(cleaned)
        if to_add:
            new_lines = existing_lines + to_add
            self.set_ignore_content(repo_path, "\n".join(new_lines) + "\n")
        return self.get_ignore_rules(repo_path)

    def remove_ignore_rules(self, repo_path: str, patterns: List[str]) -> List[str]:
        """Remove matching rule lines from ``.git/codebase-rag/ignore``."""
        targets = {
            subline.strip()
            for pat in patterns
            for subline in pat.splitlines()
            if subline.strip()
        }
        if not targets:
            return self.get_ignore_rules(repo_path)
        existing_lines = self.get_ignore_content(repo_path).splitlines()
        kept_lines = [line for line in existing_lines if line.strip() not in targets]
        self.set_ignore_content(
            repo_path,
            ("\n".join(kept_lines) + "\n") if kept_lines else "",
        )
        return self.get_ignore_rules(repo_path)

    def is_file_ignored(self, repo_path: str, file_path: str) -> bool:
        """Check whether a repository file is ignored by global or project ignore rules."""
        clean_path = normalize_repo_relative_path(repo_path, file_path)
        repo_lines = self.get_ignore_content(repo_path).splitlines()
        repo_rules = compile_gitignore_lines(repo_lines) if repo_lines else None
        return self._file_filter.is_ignored(clean_path, repo_rules=repo_rules)

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
            include_uncertain: Retained for call-site compatibility (no-op).

        Returns:
            List of FileEntry (files passing global + repo ignore rules).
        """
        stdout = await self._run_git(repo_path, ["ls-tree", "-r", "--full-tree", ref])

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

        repo_ignore_lines = self.get_ignore_content(repo_path).splitlines()
        result = self._file_filter.filter(raw_paths, repo_ignore_lines=repo_ignore_lines)

        logger.info(
            "get_file_tree_filtered",
            repo=repo_path,
            ref=ref,
            raw=len(raw_paths),
            included=len(result.included),
            excluded=len(result.excluded),
        )

        entries = []
        for path in result.included:
            blob = blob_map[path]
            entries.append(FileEntry(
                file_path=path,
                blob_hash=blob,
                file_version_id=compute_file_version_id(path, blob),
            ))

        return entries

    def get_filter_result(
        self, file_paths: List[str], repo_path: Optional[str] = None,
    ) -> FilterResult:
        """Apply global and optional repo-level ignore filtering."""
        repo_lines = (
            self.get_ignore_content(repo_path).splitlines()
            if repo_path
            else None
        )
        return self._file_filter.filter(file_paths, repo_ignore_lines=repo_lines)

    # =========================================================================
    # 2.2 Get changed files
    # =========================================================================

    async def get_changed_files(
        self, repo_path: str, old_ref: str, new_ref: str,
    ) -> ChangedFilesResult:
        """Get files changed between two refs, with global + per-repo ignore filtering.

        Pure renames (R status with identical blob hash) are captured as
        ``RenamedFileEntry`` — no LLM / embedding regeneration needed.
        Renames with content changes are decomposed into delete + add.
        """
        stdout = await self._run_git(
            repo_path, ["diff", "--name-status", f"{old_ref}..{new_ref}"]
        )

        added_paths: List[str] = []
        modified_paths: List[str] = []
        deleted_paths: List[str] = []
        rename_pairs: List[tuple] = []

        for line in stdout.splitlines():
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0][0]

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

        pure_renames: List[tuple] = []
        for old_path, new_path in rename_pairs:
            old_blob = await self.get_blob_hash(repo_path, old_ref, old_path)
            new_blob = await self.get_blob_hash(repo_path, new_ref, new_path)
            if old_blob and new_blob and old_blob == new_blob:
                pure_renames.append((old_path, new_path, old_blob))
            else:
                deleted_paths.append(old_path)
                added_paths.append(new_path)

        all_paths = set(added_paths + modified_paths + deleted_paths)
        rename_new_paths = [new_p for _, new_p, _ in pure_renames]
        all_paths.update(rename_new_paths)
        repo_ignore_lines = self.get_ignore_content(repo_path).splitlines()
        filter_result = self._file_filter.filter(
            list(all_paths), repo_ignore_lines=repo_ignore_lines,
        )
        included_set = set(filter_result.included)

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

        inc_added = await _build_entries(added_paths, included_set, new_ref)
        inc_modified = await _build_modified(modified_paths, included_set)
        inc_deleted = await _build_entries(deleted_paths, included_set, old_ref)
        inc_renamed = _build_renamed(pure_renames, included_set)

        logger.info(
            "get_changed_files",
            repo=repo_path,
            old_ref=old_ref[:8],
            new_ref=new_ref[:8],
            included_add=len(inc_added),
            included_mod=len(inc_modified),
            included_del=len(inc_deleted),
            included_ren=len(inc_renamed),
            excluded=len(filter_result.excluded),
        )

        return ChangedFilesResult(
            changes=ChangedFiles(
                added=inc_added, modified=inc_modified, deleted=inc_deleted,
                renamed=inc_renamed,
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
    # 2.4 Get file content (text & binary + Git LFS resolution)
    # =========================================================================

    async def get_file_content(self, repo_path: str, blob_hash: str) -> str:
        """Get file content by blob hash (UTF-8 decoded)."""
        try:
            return await self._run_git(repo_path, ["show", blob_hash])
        except GitCommandError as e:
            if "not a valid object name" in e.stderr or "does not exist" in e.stderr:
                raise GitFileNotFoundError(blob_hash, blob_hash) from e
            raise

    async def get_file_bytes(
        self,
        repo_path: str,
        blob_hash: str,
        file_path: Optional[str] = None,
    ) -> bytes:
        """Get raw file bytes by blob hash, automatically resolving Git LFS pointers.

        If the Git blob is a Git LFS pointer (``version https://git-lfs.github.com/spec/v1``):
        1. Reads the matching object from the repository's local LFS object store.
        2. Uses the working tree file only when its SHA-256 and size match the pointer.
        3. Otherwise pipes the pointer through ``git lfs smudge`` and verifies the result.
        """
        try:
            raw = await self._run_git_bytes(repo_path, ["cat-file", "blob", blob_hash])
        except GitCommandError as e:
            if "not a valid object name" in e.stderr or "does not exist" in e.stderr:
                raise GitFileNotFoundError(file_path or blob_hash, blob_hash) from e
            raise

        if raw.startswith(_LFS_POINTER_PREFIX):
            raw = await self._resolve_lfs_pointer_bytes(repo_path, raw, file_path=file_path)

        return raw

    async def get_workdir_file_bytes(self, repo_path: str, file_path: str) -> bytes:
        """Read raw bytes of a file in the working tree, resolving Git LFS pointers if needed."""
        clean_path = normalize_repo_relative_path(repo_path, file_path)
        abs_path = os.path.join(repo_path, clean_path)
        if not os.path.isfile(abs_path):
            raise GitFileNotFoundError(clean_path, "working directory")

        raw = await asyncio.to_thread(Path(abs_path).read_bytes)
        if raw.startswith(_LFS_POINTER_PREFIX):
            raw = await self._resolve_lfs_pointer_bytes(repo_path, raw, file_path=clean_path)
        return raw

    async def _resolve_lfs_pointer_bytes(
        self,
        repo_path: str,
        pointer_bytes: bytes,
        file_path: Optional[str] = None,
    ) -> bytes:
        """Resolve a Git LFS pointer without mixing bytes from another file version."""
        try:
            oid, expected_size = _parse_lfs_pointer(pointer_bytes)
        except ValueError as e:
            raise GitCommandError(
                command="git lfs pointer", returncode=1, stderr=str(e),
            ) from e

        # 1. Prefer the exact object from the local LFS object store.
        try:
            common_dir_raw = await self._run_git_bytes(
                repo_path, ["rev-parse", "--git-common-dir"],
            )
            common_dir_text = common_dir_raw.decode("utf-8").strip()
            common_dir = Path(common_dir_text)
            if not common_dir.is_absolute():
                common_dir = Path(repo_path) / common_dir
            object_path = (
                common_dir / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
            )
            if object_path.is_file():
                object_bytes = await asyncio.to_thread(object_path.read_bytes)
                if _lfs_content_matches(object_bytes, oid, expected_size):
                    return object_bytes
                logger.warning(
                    "git_lfs_local_object_hash_mismatch",
                    repo=repo_path, file_path=file_path, oid=oid,
                )
        except Exception as e:
            logger.debug(
                "git_lfs_local_object_lookup_failed",
                repo=repo_path, file_path=file_path, error=str(e)[:200],
            )

        # 2. Use working tree bytes only if they are the exact pointed-to version.
        if file_path:
            clean_path = normalize_repo_relative_path(repo_path, file_path)
            abs_path = os.path.join(repo_path, clean_path)
            if os.path.isfile(abs_path):
                try:
                    wt_bytes = await asyncio.to_thread(Path(abs_path).read_bytes)
                    if (
                        wt_bytes
                        and not wt_bytes.startswith(_LFS_POINTER_PREFIX)
                        and _lfs_content_matches(wt_bytes, oid, expected_size)
                    ):
                        return wt_bytes
                except OSError:
                    pass

        # 3. Fall back to smudge and verify that the returned bytes match the OID.
        smudge_args = ["lfs", "smudge"]
        if file_path:
            smudge_args.append(normalize_repo_relative_path(repo_path, file_path))
        try:
            smudged = await self._run_git_bytes(
                repo_path, smudge_args, stdin_bytes=pointer_bytes,
            )
            if (
                smudged
                and not smudged.startswith(_LFS_POINTER_PREFIX)
                and _lfs_content_matches(smudged, oid, expected_size)
            ):
                return smudged
            logger.warning(
                "git_lfs_smudged_content_mismatch",
                repo=repo_path, file_path=file_path, oid=oid,
            )
        except Exception as e:
            logger.warning(
                "git_lfs_smudge_failed",
                repo=repo_path,
                file_path=file_path,
                error=str(e)[:200],
            )

        raise GitCommandError(
            command="git lfs smudge",
            returncode=1,
            stderr=(
                f"文件 {file_path or ''} 对应的 Git LFS 对象 {oid} 无法在本地准确还原，"
                "请先执行 git lfs pull 检出该版本的真实文件。"
            ),
        )

    @staticmethod
    def detect_file_modality(
        file_path: str,
        raw_bytes: Optional[bytes] = None,
    ) -> Tuple[Literal["text", "image", "unsupported_binary"], Optional[str]]:
        """Detect whether a file is text, supported image (.png/.jpg/.jpeg), or unsupported binary."""
        return classify_file_modality(file_path, raw_bytes=raw_bytes)

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
        """Return the deterministic root commit hash of the repository."""
        try:
            stdout = await self._run_git(
                repo_path, ["rev-list", "--max-parents=0", "HEAD"],
            )
        except (GitCommandError, GitRefNotFoundError):
            return None

        roots = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not roots:
            return None

        return min(roots)

    # =========================================================================
    # 2.8 Get blob hash for a single file
    # =========================================================================

    async def get_blob_hash(
        self, repo_path: str, ref: str, file_path: str, *, strict: bool = False,
    ) -> Optional[str]:
        """Get a committed blob; strict callers distinguish read errors from absence."""
        clean_path = normalize_repo_relative_path(repo_path, file_path)
        try:
            stdout = await self._run_git(
                repo_path, ["ls-tree", ref, "--", clean_path]
            )
        except GitCommandError:
            if strict:
                raise
            return None

        stdout = stdout.strip()
        if not stdout:
            return None

        parts = stdout.split("\t", 1)
        if strict:
            fields = parts[0].split()
            if (
                len(stdout.splitlines()) != 1
                or len(parts) != 2
                or not parts[1]
                or len(fields) != 3
                or not re.fullmatch(r"[0-7]{6}", fields[0])
                or fields[1] != "blob"
                or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", fields[2])
            ):
                raise GitCommandError(
                    command="git ls-tree", returncode=1,
                    stderr=f"无法解析文件 {clean_path} 的 Git blob 记录，或目标不是文件。",
                )
            return fields[2]
        if len(parts) != 2:
            return None
        return parts[0].split()[2]

    # =========================================================================
    # 2.9 Get blob hash for a working directory file
    # =========================================================================

    async def get_workdir_blob_hash(
        self, repo_path: str, file_path: str,
    ) -> str:
        """Compute blob hash for a working directory file via git hash-object --filters.

        Using ``--filters`` ensures that files tracked via Git LFS or clean filters
        yield the exact same blob hash in the working directory as ``git ls-tree``
        after commit. Falls back to plain ``hash-object`` if ``--filters`` fails.
        """
        clean_path = normalize_repo_relative_path(repo_path, file_path)
        abs_path = os.path.join(repo_path, clean_path)
        if not os.path.exists(abs_path):
            raise GitFileNotFoundError(clean_path, "working directory")

        try:
            stdout = await self._run_git(
                repo_path, ["hash-object", "--filters", "--", clean_path],
            )
        except GitCommandError:
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
    # Internal: git command executor
    # =========================================================================

    async def _run_git_bytes(
        self,
        repo_path: str,
        args: List[str],
        stdin_bytes: Optional[bytes] = None,
    ) -> bytes:
        """Execute a git command and return raw stdout bytes without UTF-8 corruption."""
        sem = self._get_repo_semaphore(repo_path)
        cmd = [self._config.git_executable, "-C", repo_path] + args
        cmd_str = " ".join(cmd)

        async with sem:
            start = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes),
                    timeout=self._config.command_timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise GitTimeoutError(cmd_str, self._config.command_timeout)

            elapsed = time.monotonic() - start
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            logger.debug(
                "git_command_bytes_executed",
                command=cmd_str,
                elapsed_ms=round(elapsed * 1000, 1),
                returncode=proc.returncode,
                stdout_bytes=len(stdout_bytes),
            )

            if proc.returncode != 0:
                if "not a git repository" in stderr.lower():
                    raise GitRepositoryError(f"Not a Git repository: {repo_path}")
                if "unknown revision" in stderr.lower() or "bad revision" in stderr.lower():
                    ref = args[1] if len(args) > 1 else "unknown"
                    raise GitRefNotFoundError(ref)

                raise GitCommandError(cmd_str, proc.returncode, stderr)

            return stdout_bytes

    async def _run_git(self, repo_path: str, args: List[str]) -> str:
        """Unified git command executor returning UTF-8 decoded stdout."""
        stdout_bytes = await self._run_git_bytes(repo_path, args)
        return stdout_bytes.decode("utf-8", errors="replace")

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


def normalize_repo_relative_path(repo_path: str, file_path: str) -> str:
    """Normalize an incoming file path to a canonical repository-relative POSIX path."""
    if not file_path:
        return ""

    norm_file = os.path.normpath(file_path)
    norm_repo = os.path.normpath(repo_path)

    if os.path.isabs(norm_file):
        try:
            rel = os.path.relpath(norm_file, norm_repo)
            if not rel.startswith(".."):
                return rel.replace("\\", "/").strip().lstrip("/")
        except ValueError:
            pass

    clean_file = file_path.replace("\\", "/").strip()
    clean_repo = repo_path.replace("\\", "/").strip().rstrip("/")
    if clean_file.lower().startswith(clean_repo.lower() + "/"):
        clean_file = clean_file[len(clean_repo) + 1 :]

    return clean_file.replace("\\", "/").strip().lstrip("/")


def compute_file_version_id(file_path: str, blob_hash: str) -> str:
    """Compute file_version_id = sha256(filePath + blobHash)."""
    normalized = file_path.replace("\\", "/")
    return hashlib.sha256(
        (normalized + blob_hash).encode("utf-8")
    ).hexdigest()

