"""Move-heal residue guards: ghost .git, offline volumes, hook re-injection.

Covers the parts of the multi-copy move-heal algorithm that keep a moved
repository from leaving residue behind:
  - ManagementApiService: a ghost ``.git`` (only ``hooks/``) counts as a
    vanished copy; a copy on an offline volume never triggers a heal.
  - EventDetectionService: hook injection / periodic repair must never
    create directories at a path that no longer exists.
  - WorkflowOrchestratorService: ``get_repo_key_for_project`` prefers a
    copy whose ``.git`` still exists.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codebase_rag.common.paths import normalize_repo_key
from codebase_rag.services.event_detection.service import EventDetectionService
from codebase_rag.services.management_api.models import ProjectRecord
from codebase_rag.services.management_api.service import (
    ManagementApiService,
    _derive_project_id_from_root_commit,
    _git_metadata_present,
)
from codebase_rag.services.workflow.service import WorkflowOrchestratorService

_ROOT_COMMIT = "r" * 40
_PROJECT_ID = _derive_project_id_from_root_commit(_ROOT_COMMIT)


def _make_repo(tmp: str, name: str) -> str:
    repo = Path(tmp) / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return str(repo)


def _make_heal_service(records=()) -> ManagementApiService:
    service = object.__new__(ManagementApiService)
    service._projects = {}
    for record in records:
        repo_key = record.repo_key or normalize_repo_key(record.git_repo_path)
        service._projects[repo_key] = ProjectRecord(
            project_id=record.project_id,
            git_repo_path=record.git_repo_path,
            display_name=record.display_name,
            repo_key=repo_key,
        )
    service._in_flight_heals = {}
    service._initialized = True
    service._git_ops = SimpleNamespace(
        get_root_commit=AsyncMock(return_value=_ROOT_COMMIT),
    )
    service._event_detection = SimpleNamespace(
        unregister_project=AsyncMock(return_value=True),
        unregister_repo=AsyncMock(return_value=True),
    )
    service._workflow = SimpleNamespace(
        unregister_project=Mock(),
        unregister_repo=Mock(),
    )
    service._memory = SimpleNamespace(
        destroy_all_instances=AsyncMock(return_value=0),
        destroy_repo_instances=AsyncMock(return_value=0),
    )
    service._save_registry = Mock()
    service.register_project = AsyncMock(
        return_value=SimpleNamespace(project_id=_PROJECT_ID),
    )
    return service


class GhostAndOfflineHealTest(unittest.IsolatedAsyncioTestCase):
    async def test_ghost_git_directory_counts_as_vanished_copy(self) -> None:
        """A .git left behind with only hooks/ must not block the heal."""
        with tempfile.TemporaryDirectory() as tmp:
            ghost = Path(tmp) / "ghost"
            (ghost / ".git" / "hooks").mkdir(parents=True)
            incoming = _make_repo(tmp, "incoming")
            service = _make_heal_service([
                ProjectRecord(_PROJECT_ID, str(ghost), "ghost"),
            ])

            healed = await service.heal_moved_project(_PROJECT_ID, incoming)

            self.assertTrue(healed)
            service.register_project.assert_awaited_once_with(
                incoming, display_name="ghost",
            )

    async def test_offline_volume_never_triggers_heal(self) -> None:
        """An unreachable volume must not be read as a deleted copy."""
        with tempfile.TemporaryDirectory() as tmp:
            gone = str(Path(tmp) / "gone")
            incoming = _make_repo(tmp, "incoming")
            service = _make_heal_service([
                ProjectRecord(_PROJECT_ID, gone, "gone"),
            ])

            with patch(
                "codebase_rag.services.management_api.service"
                "._path_volume_available",
                return_value=False,
            ):
                healed = await service.heal_moved_project(_PROJECT_ID, incoming)

            self.assertFalse(healed)
            service.register_project.assert_not_awaited()

    async def test_new_clone_is_not_healed_while_all_copies_are_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = _make_repo(tmp, "live")
            clone = _make_repo(tmp, "clone")
            service = _make_heal_service([
                ProjectRecord(_PROJECT_ID, live, "live"),
            ])

            healed = await service.heal_moved_project(_PROJECT_ID, clone)

            self.assertFalse(healed)
            service.register_project.assert_not_awaited()


class GitMetadataProbeTest(unittest.TestCase):
    def test_worktree_pointer_file_counts_as_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_git_dir = Path(tmp) / "real" / ".git"
            real_git_dir.mkdir(parents=True)
            (real_git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            worktree = Path(tmp) / "worktree"
            worktree.mkdir()
            (worktree / ".git").write_text(
                f"gitdir: {real_git_dir}\n", encoding="utf-8",
            )

            self.assertTrue(_git_metadata_present(str(worktree / ".git")))

    def test_directory_without_head_is_not_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ghost = Path(tmp) / ".git"
            (ghost / "hooks").mkdir(parents=True)
            self.assertFalse(_git_metadata_present(str(ghost)))


class HookResidueGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_inject_hooks_never_creates_a_missing_repo(self) -> None:
        service = object.__new__(EventDetectionService)
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "was-here")

            await service.inject_hooks(_PROJECT_ID, missing)

            self.assertFalse(Path(missing).exists())

    async def test_hook_check_skips_a_missing_repo(self) -> None:
        service = object.__new__(EventDetectionService)
        service._projects_lock = asyncio.Lock()
        service.check_hooks_status = AsyncMock()
        service.inject_hooks = AsyncMock()
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "was-here")
            service._projects = {
                normalize_repo_key(missing): SimpleNamespace(
                    project_id=_PROJECT_ID, git_repo_path=missing,
                ),
            }

            await service._check_and_repair_hooks()

            service.check_hooks_status.assert_not_awaited()
            service.inject_hooks.assert_not_awaited()
            self.assertFalse(Path(missing).exists())


class WorkflowRepoKeyPreferenceTest(unittest.TestCase):
    def test_prefers_a_copy_whose_git_still_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dead = normalize_repo_key(str(Path(tmp) / "gone"))
            live = normalize_repo_key(_make_repo(tmp, "live"))
            service = object.__new__(WorkflowOrchestratorService)
            service._project_repos = {dead: _PROJECT_ID, live: _PROJECT_ID}

            self.assertEqual(service.get_repo_key_for_project(_PROJECT_ID), live)

    def test_falls_back_when_every_copy_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dead = normalize_repo_key(str(Path(tmp) / "gone"))
            service = object.__new__(WorkflowOrchestratorService)
            service._project_repos = {dead: _PROJECT_ID}

            self.assertEqual(service.get_repo_key_for_project(_PROJECT_ID), dead)

