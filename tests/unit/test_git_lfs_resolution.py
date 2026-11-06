"""Regression tests for exact Git LFS object resolution."""

from __future__ import annotations

import hashlib

import pytest

from codebase_rag.services.git_operations.service import GitOperationsService


def _pointer(content: bytes) -> bytes:
    oid = hashlib.sha256(content).hexdigest()
    return (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {len(content)}\n"
    ).encode("utf-8")


def _service(run_git_bytes):
    service = object.__new__(GitOperationsService)
    service._run_git_bytes = run_git_bytes
    return service


@pytest.mark.asyncio
async def test_local_lfs_object_wins_over_different_worktree_content(tmp_path):
    expected = b"HISTORICAL-IMAGE"
    pointer = _pointer(expected)
    oid = hashlib.sha256(expected).hexdigest()
    object_path = tmp_path / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(expected)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "arch.png").write_bytes(b"CURRENT-DIFFERENT-IMAGE")

    async def run_git_bytes(repo_path, args, stdin_bytes=None):
        assert args == ["rev-parse", "--git-common-dir"]
        return b".git"

    service = _service(run_git_bytes)
    actual = await service._resolve_lfs_pointer_bytes(
        str(tmp_path), pointer, file_path="docs/arch.png",
    )
    assert actual == expected


@pytest.mark.asyncio
async def test_mismatched_worktree_is_not_used_and_smudge_is_verified(tmp_path):
    expected = b"HISTORICAL-IMAGE"
    pointer = _pointer(expected)
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "arch.png").write_bytes(b"CURRENT-DIFFERENT-IMAGE")
    smudge_calls = 0

    async def run_git_bytes(repo_path, args, stdin_bytes=None):
        nonlocal smudge_calls
        if args == ["rev-parse", "--git-common-dir"]:
            return b".git"
        assert args[:2] == ["lfs", "smudge"]
        assert stdin_bytes == pointer
        smudge_calls += 1
        return expected

    service = _service(run_git_bytes)
    actual = await service._resolve_lfs_pointer_bytes(
        str(tmp_path), pointer, file_path="docs/arch.png",
    )
    assert actual == expected
    assert smudge_calls == 1


@pytest.mark.asyncio
async def test_exact_worktree_content_can_be_used_without_smudge(tmp_path):
    expected = b"HISTORICAL-IMAGE"
    pointer = _pointer(expected)
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "arch.png").write_bytes(expected)

    async def run_git_bytes(repo_path, args, stdin_bytes=None):
        assert args == ["rev-parse", "--git-common-dir"]
        return b".git"

    service = _service(run_git_bytes)
    actual = await service._resolve_lfs_pointer_bytes(
        str(tmp_path), pointer, file_path="docs/arch.png",
    )
    assert actual == expected
