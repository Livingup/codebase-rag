"""Gitignore compatibility tests, including differential checks against Git."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from codebase_rag.services.git_operations.file_filter import FileFilter


def test_excluded_parent_directory_cannot_be_reincluded_by_file_negation():
    ff = FileFilter(
        exclude_patterns=[],
        extra_ignore_lines=["assets/raw/", "!assets/raw/keep.png"],
    )
    assert ff.is_ignored("assets/raw/keep.png")
    assert ff.is_ignored("assets/raw/drop.png")


def test_file_negation_works_when_parent_directory_is_not_excluded():
    ff = FileFilter(
        exclude_patterns=[],
        extra_ignore_lines=["assets/*", "!assets/keep.png"],
    )
    assert not ff.is_ignored("assets/keep.png")
    assert ff.is_ignored("assets/drop.png")


def test_backslash_escape_is_not_rewritten_as_a_path_separator():
    ff = FileFilter(exclude_patterns=[r"foo\ bar"])
    assert ff.is_ignored("foo bar")
    assert not ff.is_ignored("foo/bar")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_supported_cases_match_git_check_ignore(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    cases = [
        (["assets/raw/", "!assets/raw/keep.png"], "assets/raw/keep.png"),
        ([r"foo\ bar"], "foo bar"),
        (["assets/*", "!assets/keep.png"], "assets/keep.png"),
        ([r"\#literal"], "#literal"),
    ]
    for rules, relative_path in cases:
        (tmp_path / ".gitignore").write_text("\n".join(rules) + "\n", encoding="utf-8")
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "-q", "--", relative_path],
            check=False,
        )
        git_ignored = result.returncode == 0
        actual = FileFilter(exclude_patterns=rules).is_ignored(relative_path)
        assert actual == git_ignored, (rules, relative_path, result.returncode, actual)
