"""Moved-repository auto-heal: MCP contract enforcement + re-registration.

Covers:
  - ManagementApiService.heal_moved_project: hot-path no-op, move detection,
    copy scenario refusal, identity verification, pruned-entry restore,
    single-flight, initialization gate.
  - McpService._read_project_id: git-root contract enforcement.
  - McpService._resolve_project_id: background heal kick-off.
  - McpService.search: synchronous heal + retry on NoActiveInstanceError.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codebase_rag.services.management_api.models import ProjectRecord
from codebase_rag.services.management_api.service import (
    ManagementApiService,
    _derive_project_id_from_root_commit,
)
from codebase_rag.services.memory_qdrant.errors import NoActiveInstanceError
from codebase_rag.services.memory_qdrant.models import SearchResult
from codebase_rag.services.mcp_service.models import (
    InvalidGitRepoPathError,
    ProjectNotIndexedError,
    ProjectNotRegisteredError,
)
from codebase_rag.services.mcp_service.service import McpService


_ROOT_COMMIT = "r" * 40
_PROJECT_ID = _derive_project_id_from_root_commit(_ROOT_COMMIT)
_OTHER_COMMIT = "o" * 40


class _ConfigManager:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            mcp=SimpleNamespace(
                reranker=SimpleNamespace(recall_limit=20),
                knowledge_submission=SimpleNamespace(point_token_limit=8000),
            )
        )


class _Embedding:
    async def embed(self, texts, is_query: bool):
        return [
            SimpleNamespace(
                dense_vector=[0.1, 0.2],
                sparse_vector=SimpleNamespace(indices=[1], values=[1.0]),
            )
            for _ in texts
        ]


def _make_repo(tmp: str, name: str) -> str:
    repo = Path(tmp) / name
    (repo / ".git").mkdir(parents=True)
    # A real repository always carries HEAD; the move-heal liveness probe
    # uses it to tell a live copy from a ghost .git left at a dead path.
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return str(repo)


def _make_plain_dir(tmp: str, name: str) -> str:
    repo = Path(tmp) / name
    repo.mkdir(parents=True)
    return str(repo)


def _make_project_path_with_id(tmp: str, project_id: str) -> str:
    meta_dir = Path(tmp) / ".git" / "codebase-rag"
    meta_dir.mkdir(parents=True)
    (meta_dir / "project_id").write_text(project_id, encoding="utf-8")
    return tmp


class HealMovedProjectTest(unittest.IsolatedAsyncioTestCase):
    def make_service(self) -> ManagementApiService:
        service = object.__new__(ManagementApiService)
        service._projects = {}
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
        service.register_project = AsyncMock(return_value=SimpleNamespace(
            project_id=_PROJECT_ID,
            git_repo_path="registered",
            display_name="repo",
            status="registered",
            branch="main",
            commit_hash="a" * 8,
        ))
        return service

    async def test_hot_path_noop_when_registered_path_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service()
            repo = _make_repo(tmp, "repo")
            service._projects[_PROJECT_ID] = ProjectRecord(
                _PROJECT_ID, repo, "repo",
            )
            healed = await service.heal_moved_project(_PROJECT_ID, repo)
            self.assertFalse(healed)
            service.register_project.assert_not_awaited()

    async def test_heal_reregisters_moved_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service()
            old_path = str(Path(tmp) / "gone")  # moved away, no longer exists
            new_path = _make_repo(tmp, "new-location")
            service._projects[_PROJECT_ID] = ProjectRecord(
                _PROJECT_ID, old_path, "repo",
            )
            healed = await service.heal_moved_project(_PROJECT_ID, new_path)
            self.assertTrue(healed)
            service.register_project.assert_awaited_once_with(
                new_path, display_name="repo",
            )

    async def test_copy_scenario_refuses_heal_when_old_path_still_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service()
            old_path = _make_repo(tmp, "original")
            new_path = _make_repo(tmp, "clone")
            service._projects[_PROJECT_ID] = ProjectRecord(
                _PROJECT_ID, old_path, "repo",
            )
            healed = await service.heal_moved_project(_PROJECT_ID, new_path)
            self.assertFalse(healed)
            service.register_project.assert_not_awaited()

    async def test_identity_mismatch_blocks_heal(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service()
            # incoming repo derives a different identity than requested
            service._git_ops.get_root_commit = AsyncMock(return_value=_OTHER_COMMIT)
            old_path = str(Path(tmp) / "gone")
            new_path = _make_repo(tmp, "different-repo")
            service._projects[_PROJECT_ID] = ProjectRecord(
                _PROJECT_ID, old_path, "repo",
            )
            healed = await service.heal_moved_project(_PROJECT_ID, new_path)
            self.assertFalse(healed)
            service.register_project.assert_not_awaited()

    async def test_pruned_registry_entry_is_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service()
            # registry entry pruned by restart-restore; .git cache still
            # travels with the repository, so the caller resolves the id
            new_path = _make_repo(tmp, "restored")
            healed = await service.heal_moved_project(_PROJECT_ID, new_path)
            self.assertTrue(healed)
            service.register_project.assert_awaited_once_with(
                new_path, display_name="",
            )

    async def test_non_git_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service()
            plain = _make_plain_dir(tmp, "not-a-repo")
            healed = await service.heal_moved_project(_PROJECT_ID, plain)
            self.assertFalse(healed)
            service.register_project.assert_not_awaited()

    async def test_uninitialized_service_never_heals(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service()
            service._initialized = False
            new_path = _make_repo(tmp, "repo")
            healed = await service.heal_moved_project(_PROJECT_ID, new_path)
            self.assertFalse(healed)
            service.register_project.assert_not_awaited()

    async def test_single_flight_dedupes_concurrent_heals(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service()

            async def slow_register(path, display_name=""):
                await asyncio.sleep(0.05)
                return SimpleNamespace(project_id=_PROJECT_ID)

            service.register_project = AsyncMock(side_effect=slow_register)
            old_path = str(Path(tmp) / "gone")
            new_path = _make_repo(tmp, "moved")
            service._projects[_PROJECT_ID] = ProjectRecord(
                _PROJECT_ID, old_path, "repo",
            )

            results = await asyncio.gather(
                service.heal_moved_project(_PROJECT_ID, new_path),
                service.heal_moved_project(_PROJECT_ID, new_path),
            )
            self.assertEqual(results, [True, True])
            service.register_project.assert_awaited_once()


class ReadProjectIdContractTest(unittest.TestCase):
    def test_rejects_plain_directory_with_actionable_error(self):
        service = object.__new__(McpService)
        with tempfile.TemporaryDirectory() as tmp:
            plain = _make_plain_dir(tmp, "subdir")
            with self.assertRaises(InvalidGitRepoPathError) as ctx:
                service._read_project_id(plain)
            message = str(ctx.exception)
            self.assertIn("不是 Git 仓库根目录", message)
            self.assertIn(plain, message)
            self.assertIn("不会自动上溯", message)

    def test_git_file_entry_passes_contract_then_reports_unregistered(self):
        service = object.__new__(McpService)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").write_text(
                "gitdir: ../elsewhere", encoding="utf-8",
            )
            with self.assertRaises(ProjectNotRegisteredError):
                service._read_project_id(tmp)

    def test_registered_root_still_resolves(self):
        service = object.__new__(McpService)
        with tempfile.TemporaryDirectory() as tmp:
            path = _make_project_path_with_id(tmp, "pid-1")
            self.assertEqual(service._read_project_id(path), "pid-1")


class _HealThenOkMemory:
    """hybrid_search fails once with NoActiveInstanceError, then succeeds."""

    def __init__(self) -> None:
        self.hybrid_search_calls = 0

    async def hybrid_search(
        self, project_id: str, query_dense, query_sparse, top_k: int,
    ):
        self.hybrid_search_calls += 1
        if self.hybrid_search_calls == 1:
            raise NoActiveInstanceError("no active instance")
        return [
            SearchResult(
                score=0.9,
                file_version_id="file-version-1",
                file_path="src/example.py",
                entity_type="file",
                entity_name="example.py",
                business_description="example knowledge",
            )
        ]


class _AlwaysFailingMemory:
    def __init__(self) -> None:
        self.hybrid_search_calls = 0

    async def hybrid_search(
        self, project_id: str, query_dense, query_sparse, top_k: int,
    ):
        self.hybrid_search_calls += 1
        raise NoActiveInstanceError("no active instance")


def _make_mcp_service(memory) -> McpService:
    return McpService(
        config_manager=_ConfigManager(),
        embedding=_Embedding(),
        memory_qdrant=memory,
        agent=SimpleNamespace(),
    )


class McpHealIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_project_id_kicks_background_heal(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_mcp_service(SimpleNamespace())
            handler = AsyncMock(return_value=True)
            service.set_move_heal_handler(handler)
            project_path = _make_project_path_with_id(tmp, "pid-1")

            resolved = service._resolve_project_id(project_path)

            self.assertEqual(resolved, "pid-1")
            tasks = list(service._heal_tasks)
            if tasks:
                await asyncio.gather(*tasks)
            handler.assert_awaited_once_with("pid-1", project_path)

    async def test_search_heals_and_retries_on_no_active_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = _HealThenOkMemory()
            service = _make_mcp_service(memory)
            handler = AsyncMock(return_value=True)
            service.set_move_heal_handler(handler)
            project_path = _make_project_path_with_id(tmp, "pid-1")

            results = await service.search(project_path, "find example", top_k=10)

            self.assertEqual(memory.hybrid_search_calls, 2)
            self.assertEqual(len(results), 1)
            # The background kick-off task may not have been scheduled yet
            # (no suspension point inside the mocked search path) — drain
            # it before asserting the handler saw both invocations.
            tasks = list(service._heal_tasks)
            if tasks:
                await asyncio.gather(*tasks)
            self.assertEqual(handler.await_count, 2)  # kick-off + sync retry

    async def test_search_raises_not_indexed_when_heal_not_applicable(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = _AlwaysFailingMemory()
            service = _make_mcp_service(memory)
            handler = AsyncMock(return_value=False)
            service.set_move_heal_handler(handler)
            project_path = _make_project_path_with_id(tmp, "pid-1")

            with self.assertRaises(ProjectNotIndexedError) as ctx:
                await service.search(project_path, "find example", top_k=10)

            self.assertEqual(memory.hybrid_search_calls, 1)  # no retry
            self.assertIn("lazy rebuild failed", str(ctx.exception))
            tasks = list(service._heal_tasks)
            if tasks:
                await asyncio.gather(*tasks)

    async def test_search_without_handler_keeps_legacy_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = _AlwaysFailingMemory()
            service = _make_mcp_service(memory)  # no set_move_heal_handler
            project_path = _make_project_path_with_id(tmp, "pid-1")

            with self.assertRaises(ProjectNotIndexedError):
                await service.search(project_path, "find example", top_k=10)

            self.assertEqual(memory.hybrid_search_calls, 1)


if __name__ == "__main__":
    unittest.main()
