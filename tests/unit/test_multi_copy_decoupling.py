"""Multi-copy decoupling acceptance tests.

Covers the "registration and in-memory instances keyed by local repository
copy" design:
  - Two copies of one project (same project_id, different paths) coexist in
    the registry and each holds its own active in-memory instance.
  - Searches never cross-contaminate between copies.
  - unregister_repo removes a single copy; unregister_project removes all
    copies — neither deletes persistent Qdrant points.
  - Hook / Git events route to the originating copy.
  - Move-heal refuses when a live sibling copy exists and heals when all
    registered copies of the project are dead.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from qdrant_client import models

from codebase_rag.common.paths import normalize_repo_key
from codebase_rag.common.types import SparseVector
from codebase_rag.config.schema import (
    HnswConfig,
    MemoryCacheConfig,
    MemoryInstanceConfig,
    MemoryQdrantConfig,
    VectorConfig,
)
from codebase_rag.services.event_detection.models import EventSource, HookEvent
from codebase_rag.services.event_detection.service import EventDetectionService
from codebase_rag.services.management_api.models import ProjectRecord
from codebase_rag.services.management_api.service import (
    ManagementApiService,
    _derive_project_id_from_root_commit,
)
from codebase_rag.services.memory_qdrant.service import MemoryQdrantService
from codebase_rag.services.workflow.service import WorkflowOrchestratorService


_ROOT_COMMIT = "r" * 40
_PROJECT_ID = _derive_project_id_from_root_commit(_ROOT_COMMIT)


class _MemoryConfigManager:
    def __init__(self, max_branches: int = 4) -> None:
        self.config = SimpleNamespace(
            memory_qdrant=MemoryQdrantConfig(
                cache=MemoryCacheConfig(
                    max_branches=max_branches,
                    idle_timeout_minutes=0,
                ),
                instance=MemoryInstanceConfig(
                    dense_vector=VectorConfig(size=2),
                    hnsw_config=HnswConfig(enabled=False),
                ),
            )
        )


def _point(file_path: str, point_id: int) -> models.PointStruct:
    return models.PointStruct(
        id=point_id,
        vector={
            "dense": [1.0, 0.0],
            "sparse": models.SparseVector(indices=[1], values=[1.0]),
        },
        payload={
            "file_version_id": f"version-{point_id}",
            "file_path": file_path,
        },
    )


def _make_repo(tmp: str, name: str) -> str:
    repo = Path(tmp) / name
    (repo / ".git").mkdir(parents=True)
    return str(repo)


class MultiCopyMemoryIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = MemoryQdrantService(_MemoryConfigManager())
        await self.service.initialize()

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()

    async def test_two_copies_hold_separate_active_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy_a = normalize_repo_key(_make_repo(tmp, "copy-a"))
            copy_b = normalize_repo_key(_make_repo(tmp, "copy-b"))

            handle_a = await self.service.create_instance(
                copy_a, "main", "c1", project_id=_PROJECT_ID,
            )
            await self.service.upsert_points(handle_a, [_point("a/example.py", 1)])
            await self.service.set_active_instance(copy_a, "main", "c1")

            handle_b = await self.service.create_instance(
                copy_b, "main", "c1", project_id=_PROJECT_ID,
            )
            await self.service.upsert_points(handle_b, [_point("b/example.py", 2)])
            await self.service.set_active_instance(copy_b, "main", "c1")

            # Same project_id, two independent active instances.
            self.assertEqual(
                set(self.service._active), {copy_a, copy_b},
            )
            for inst in self.service._cache.values():
                self.assertEqual(inst.project_id, _PROJECT_ID)

            results_a = await self.service.hybrid_search(
                copy_a, [1.0, 0.0], SparseVector(indices=[1], values=[1.0]),
            )
            results_b = await self.service.hybrid_search(
                copy_b, [1.0, 0.0], SparseVector(indices=[1], values=[1.0]),
            )

            # Searches never cross-contaminate between copies.
            self.assertEqual([r.file_path for r in results_a], ["a/example.py"])
            self.assertEqual([r.file_path for r in results_b], ["b/example.py"])

    async def test_destroy_repo_instances_leaves_sibling_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy_a = normalize_repo_key(_make_repo(tmp, "copy-a"))
            copy_b = normalize_repo_key(_make_repo(tmp, "copy-b"))

            for repo_key, point_id in ((copy_a, 1), (copy_b, 2)):
                handle = await self.service.create_instance(
                    repo_key, "main", "c1", project_id=_PROJECT_ID,
                )
                await self.service.upsert_points(handle, [_point("x.py", point_id)])
                await self.service.set_active_instance(repo_key, "main", "c1")

            removed = await self.service.destroy_repo_instances(copy_a)

            self.assertEqual(removed, 1)
            self.assertNotIn(copy_a, self.service._active)
            results_b = await self.service.hybrid_search(
                copy_b, [1.0, 0.0], SparseVector(indices=[1], values=[1.0]),
            )
            self.assertEqual(len(results_b), 1)


class MultiCopyRegistryTest(unittest.IsolatedAsyncioTestCase):
    def make_service(self, records=()) -> ManagementApiService:
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
        service.register_project = AsyncMock(return_value=SimpleNamespace(
            project_id=_PROJECT_ID,
        ))
        return service

    async def test_unregister_repo_removes_only_the_target_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy_a = _make_repo(tmp, "copy-a")
            copy_b = _make_repo(tmp, "copy-b")
            service = self.make_service([
                ProjectRecord(_PROJECT_ID, copy_a, "a"),
                ProjectRecord(_PROJECT_ID, copy_b, "b"),
            ])

            response = await service.unregister_repo(copy_a)

            self.assertEqual(response.project_id, _PROJECT_ID)
            self.assertEqual(response.deleted_points, 0)
            self.assertNotIn(normalize_repo_key(copy_a), service._projects)
            self.assertIn(normalize_repo_key(copy_b), service._projects)
            service._event_detection.unregister_repo.assert_awaited_once_with(copy_a)
            service._workflow.unregister_repo.assert_called_once_with(copy_a)
            service._memory.destroy_repo_instances.assert_awaited_once_with(
                normalize_repo_key(copy_a),
            )

    async def test_unregister_project_removes_all_copies_and_preserves_points(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy_a = _make_repo(tmp, "copy-a")
            copy_b = _make_repo(tmp, "copy-b")
            service = self.make_service([
                ProjectRecord(_PROJECT_ID, copy_a, "a"),
                ProjectRecord(_PROJECT_ID, copy_b, "b"),
            ])

            response = await service.unregister_project(_PROJECT_ID)

            self.assertEqual(response.unregistered_repos, 2)
            self.assertEqual(response.deleted_points, 0)
            self.assertEqual(service._projects, {})
            service._event_detection.unregister_project.assert_awaited_once_with(
                _PROJECT_ID,
            )
            service._workflow.unregister_project.assert_called_once_with(_PROJECT_ID)
            service._memory.destroy_all_instances.assert_awaited_once_with(
                _PROJECT_ID,
            )

    async def test_heal_refuses_when_a_sibling_copy_is_still_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_copy = _make_repo(tmp, "live")
            dead_path = str(Path(tmp) / "gone")
            incoming = _make_repo(tmp, "incoming")
            service = self.make_service([
                ProjectRecord(_PROJECT_ID, live_copy, "live"),
                ProjectRecord(_PROJECT_ID, dead_path, "dead"),
            ])

            healed = await service.heal_moved_project(_PROJECT_ID, incoming)

            self.assertFalse(healed)
            service.register_project.assert_not_awaited()

    async def test_heal_reregisters_when_all_registered_copies_are_dead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dead_a = str(Path(tmp) / "gone-a")
            dead_b = str(Path(tmp) / "gone-b")
            incoming = _make_repo(tmp, "incoming")
            service = self.make_service([
                ProjectRecord(_PROJECT_ID, dead_a, "a"),
                ProjectRecord(_PROJECT_ID, dead_b, "b"),
            ])

            healed = await service.heal_moved_project(_PROJECT_ID, incoming)

            self.assertTrue(healed)
            service.register_project.assert_awaited_once_with(incoming)


class RegistryRestoreMultiCopyTest(unittest.IsolatedAsyncioTestCase):
    async def test_restore_keeps_both_entries_of_one_project(self) -> None:
        import json

        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as tmp:
            copy_a = _make_repo(tmp, "copy-a")
            copy_b = _make_repo(tmp, "copy-b")
            registry_path = str(Path(tmp) / "registry.json")
            Path(registry_path).write_text(
                json.dumps([
                    {
                        "project_id": _PROJECT_ID,
                        "git_repo_path": copy_a,
                        "display_name": "a",
                    },
                    {
                        "project_id": _PROJECT_ID,
                        "git_repo_path": copy_b,
                        "display_name": "b",
                    },
                ]),
                encoding="utf-8",
            )

            service = object.__new__(ManagementApiService)
            service._config_manager = SimpleNamespace(
                config=SimpleNamespace(
                    management_api=SimpleNamespace(registry_path=registry_path),
                ),
            )
            service._projects = {}
            service._in_flight_heals = {}
            service._initialized = True
            service.register_project = AsyncMock()

            await service._restore_persisted_projects()

            # Both copies are re-registered — no toppling between entries
            # of the same project_id.
            self.assertEqual(service.register_project.await_count, 2)
            awaited_paths = [
                call.args[0] for call in service.register_project.await_args_list
            ]
            self.assertEqual(awaited_paths, [copy_a, copy_b])


class EventRoutingTest(unittest.IsolatedAsyncioTestCase):
    def make_event_detection(self) -> EventDetectionService:
        service = object.__new__(EventDetectionService)
        service._projects = {}
        service._projects_lock = asyncio.Lock()
        service._dedup_timestamps = {}
        service._subscribers = []
        service._watchers = {}
        service._initialized = True
        return service

    async def test_hook_event_routes_to_the_originating_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy_a = _make_repo(tmp, "copy-a")
            copy_b = _make_repo(tmp, "copy-b")
            service = self.make_event_detection()
            from codebase_rag.services.event_detection.models import ProjectHeadState
            for path in (copy_a, copy_b):
                service._projects[normalize_repo_key(path)] = ProjectHeadState(
                    project_id=_PROJECT_ID,
                    git_repo_path=path,
                    branch_name="main",
                    commit_hash="c1",
                    repo_key=normalize_repo_key(path),
                )

            routed = []
            service._on_raw_notification = AsyncMock(side_effect=lambda key, src: routed.append(key))

            await service._on_hook_event(HookEvent(
                project_id=_PROJECT_ID,
                hook_type="post-commit",
                git_repo_path=copy_b,
            ))

            self.assertEqual(routed, [normalize_repo_key(copy_b)])
            service._on_raw_notification.assert_awaited_once_with(
                normalize_repo_key(copy_b), EventSource.HOOK,
            )

    async def test_hook_event_without_repo_path_is_dropped_when_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy_a = _make_repo(tmp, "copy-a")
            copy_b = _make_repo(tmp, "copy-b")
            service = self.make_event_detection()
            from codebase_rag.services.event_detection.models import ProjectHeadState
            for path in (copy_a, copy_b):
                service._projects[normalize_repo_key(path)] = ProjectHeadState(
                    project_id=_PROJECT_ID,
                    git_repo_path=path,
                    branch_name="main",
                    commit_hash="c1",
                    repo_key=normalize_repo_key(path),
                )

            service._on_raw_notification = AsyncMock()

            await service._on_hook_event(HookEvent(
                project_id=_PROJECT_ID,
                hook_type="post-commit",
                # legacy hook payload without repo_path
            ))

            service._on_raw_notification.assert_not_awaited()

    async def test_workflow_event_task_carries_the_originating_repo_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            copy_a = _make_repo(tmp, "copy-a")
            copy_b = _make_repo(tmp, "copy-b")
            service = object.__new__(WorkflowOrchestratorService)
            service._project_repos = {
                normalize_repo_key(copy_a): _PROJECT_ID,
                normalize_repo_key(copy_b): _PROJECT_ID,
            }
            enqueued = []

            class _Queue:
                def enqueue(self, task):
                    enqueued.append(task)
                    return 0

            service._task_queue = _Queue()

            from codebase_rag.services.event_detection.models import (
                EventType,
                GitEvent,
            )
            await service.on_git_event(GitEvent(
                event_id="e1",
                project_id=_PROJECT_ID,
                event_type=EventType.CONTENT_COMMIT,
                source=EventSource.HOOK,
                branch_name="main",
                commit_hash="c2",
                previous_branch_name="main",
                previous_commit_hash="c1",
                git_repo_path=copy_b,
            ))

            self.assertEqual(len(enqueued), 1)
            self.assertEqual(enqueued[0].repo_key, normalize_repo_key(copy_b))
            self.assertEqual(enqueued[0].project_id, _PROJECT_ID)


if __name__ == "__main__":
    unittest.main()
