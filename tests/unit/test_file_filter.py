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


def test_global_and_repository_rules_are_combined():
    ff = FileFilter(
        exclude_patterns=["node_modules/**", "*.lock"],
        extra_ignore_lines=["assets/raw/", "!assets/raw/keep.png"],
    )
    assert ff.is_ignored("node_modules/pkg/index.js")
    assert ff.is_ignored("package.lock")
    assert ff.is_ignored("assets/raw/drop.png")
    assert ff.is_ignored("assets/raw/keep.png")
    assert not ff.is_ignored("docs/arch.png")


def test_default_deny_with_directory_and_nested_extension_whitelist():
    ff = FileFilter(
        exclude_patterns=[],
        extra_ignore_lines=["*", "!*/", "!**/*.ts"],
    )
    assert not ff.is_ignored("src/shared/performance/performanceMonitor.ts")
    assert not ff.is_ignored("src/a/b.ts")
    assert not ff.is_ignored("./src/a/b.ts")
    assert not ff.is_ignored("src\\a\\b.ts")
    assert ff.is_ignored("src/a/b.js")
    assert ff.is_ignored("README.md")


def test_default_deny_with_exact_file_whitelist():
    ff = FileFilter(
        exclude_patterns=[],
        extra_ignore_lines=["*", "!*/", "!src/a/b.ts"],
    )
    assert not ff.is_ignored("src/a/b.ts")
    assert ff.is_ignored("src/a/c.ts")
    assert ff.is_ignored("a.ts")


def test_whitelist_can_still_be_overridden_by_later_test_and_build_exclusions():
    ff = FileFilter(
        exclude_patterns=[],
        extra_ignore_lines=[
            "*",
            "!*/",
            "!**/*.ts",
            "!**/*.js",
            "**/*.test.*",
            "dist/",
        ],
    )
    assert not ff.is_ignored("src/a/b.ts")
    assert ff.is_ignored("src/a/b.test.ts")
    assert ff.is_ignored("dist/a.js")


def test_build_directory_cannot_be_rescued_by_extension_negation():
    for rules in [
        ["!**/*.js", "dist/"],
        ["dist/", "!**/*.js"],
        ["*", "!*/", "dist/", "!**/*.js"],
    ]:
        ff = FileFilter(exclude_patterns=[], extra_ignore_lines=rules)
        assert ff.is_ignored("dist/a.js"), rules


def test_global_rules_can_be_overridden_by_repo_whitelist():
    from codebase_rag.services.git_operations.file_filter import compile_gitignore_lines

    ff = FileFilter(
        exclude_patterns=["tests/**", "**/*.test.*", "*.md"],
    )
    repo_rules = compile_gitignore_lines(
        ["*", "!*/", "!**/*.ts", "!/AGENTS.md", "** /tests/".replace(" ", ""), "**/*.test.*"]
    )
    assert not ff.is_ignored("src/shared/performance/performanceMonitor.ts", repo_rules=repo_rules)
    assert not ff.is_ignored("AGENTS.md", repo_rules=repo_rules)
    assert ff.is_ignored("README.md", repo_rules=repo_rules)
    assert ff.is_ignored("src/shared/performance/performanceMonitor.test.ts", repo_rules=repo_rules)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_supported_cases_match_git_check_ignore(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    cases = [
        (["assets/raw/", "!assets/raw/keep.png"], "assets/raw/keep.png"),
        ([r"foo\ bar"], "foo bar"),
        (["assets/*", "!assets/keep.png"], "assets/keep.png"),
        ([r"\#literal"], "#literal"),
        (["*", "!*/", "!**/*.ts"], "src/shared/performance/performanceMonitor.ts"),
        (["*", "!*/", "!src/a/b.ts"], "src/a/b.ts"),
        (["*", "!*/", "!src/a/b.ts"], "src/a/c.ts"),
        (["*", "!**/*.ts"], "src/a/b.ts"),
        (["*", "!*/", "!**/*.ts", "**/*.test.*"], "src/a/b.test.ts"),
        (["*", "!*/", "!**/*.ts", "**/*.test.*"], "src/a/b.ts"),
        (["!**/*.js", "dist/"], "dist/a.js"),
        (["dist/", "!**/*.js"], "dist/a.js"),
        (["*", "!src/", "!src/shared/a.ts"], "src/shared/a.ts"),
        (["*", "!src/", "!src/shared/", "!src/shared/a.ts"], "src/shared/a.ts"),
        (["src/**", "!src/shared/", "!src/shared/a.ts"], "src/shared/a.ts"),
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
