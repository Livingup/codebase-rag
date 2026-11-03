"""Shared path normalization for local Git repository copy identity."""

from __future__ import annotations

import os


def normalize_repo_key(git_repo_path: str) -> str:
    """Canonical identity key for a local Git repository copy.

    Combines normpath (separator/dot cleanup), abspath (absolutize),
    realpath (symlink resolution) and normcase (case folding on Windows)
    so that different textual spellings of the same on-disk location
    collapse to a single key.  The result is itself a valid absolute
    path and can be used directly for Git operations.
    """
    return os.path.normcase(
        os.path.realpath(os.path.abspath(os.path.normpath(git_repo_path)))
    )
