"""Data models for Git operations service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class FileEntry:
    """A file in a Git tree with its content-addressed ID."""

    file_path: str          # Repository-relative path (/ separator)
    blob_hash: str          # Git blob SHA-1
    file_version_id: str    # sha256(filePath + blobHash)


@dataclass(frozen=True)
class ModifiedFileEntry:
    """A modified file with old and new versions."""

    file_path: str
    old_blob_hash: str
    new_blob_hash: str
    old_file_version_id: str
    new_file_version_id: str


@dataclass(frozen=True)
class RenamedFileEntry:
    """A file whose path changed but content is identical (same blob_hash).

    Produced when ``git diff --name-status`` reports ``R`` status and the
    blob hash is unchanged.  No LLM / embedding regeneration needed — the
    existing knowledge points can be cloned with updated path metadata.
    """

    old_file_path: str
    new_file_path: str
    blob_hash: str            # identical for old and new
    old_file_version_id: str  # hash(old_file_path + blob_hash)
    new_file_version_id: str  # hash(new_file_path + blob_hash)


@dataclass(frozen=True)
class ChangedFiles:
    """Result of comparing two Git refs."""

    added: List[FileEntry]
    modified: List[ModifiedFileEntry]
    deleted: List[FileEntry]
    renamed: List[RenamedFileEntry] = field(default_factory=list)


@dataclass(frozen=True)
class ChangedFilesResult:
    """Result of get_changed_files after applying global and per-repo ignore rules."""

    changes: ChangedFiles
    excluded_count: int = 0


@dataclass(frozen=True)
class HeadInfo:
    """Current HEAD state of a repository."""

    commit_hash: str
    branch_name: Optional[str]  # None if detached HEAD
    is_detached: bool


@dataclass(frozen=True)
class BranchInfo:
    """A local branch and its tip commit."""

    name: str
    commit_hash: str


@dataclass(frozen=True)
class RepoValidation:
    """Result of validating a Git repository path."""

    is_valid: bool
    git_dir: Optional[str] = None
    root_dir: Optional[str] = None
    error: Optional[str] = None
