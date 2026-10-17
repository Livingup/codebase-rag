from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from qdrant_client import models

from codebase_rag.common.types import SparseVector
from codebase_rag.config.schema import (
    HnswConfig,
    MemoryCacheConfig,
    MemoryInstanceConfig,
    MemoryQdrantConfig,
    VectorConfig,
)
from codebase_rag.services.memory_qdrant.errors import CacheFullError, NoActiveInstanceError
from codebase_rag.services.memory_qdrant.service import MemoryQdrantService


def _point(project_id: str) -> models.PointStruct:
    return models.PointStruct(
        id=1,
        vector={
            "dense": [1.0, 0.0],
            "sparse": models.SparseVector(indices=[1], values=[1.0]),
        },
        payload={
            "file_version_id": f"version-{project_id}",
            "file_path": f"{project_id}/example.py",
        },
    )


class _ConfigManager:
    def __init__(self, max_branches: int = 2) -> None:
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


class MemoryQdrantConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = MemoryQdrantService(_ConfigManager())
        await self.service.initialize()

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()

    async def test_same_project_requests_share_one_rebuild(self) -> None:
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(project_id: str) -> None:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            handle = await self.service.create_instance(
                project_id, "main", "commit-1", staging=True,
            )
            await self.service.set_active_handle(handle)

        self.service.set_rebuild_handler(handler)
        requests = [
            asyncio.create_task(
                self.service._get_active_instance_or_rebuild("project-1")
            )
            for _ in range(5)
        ]
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        results = await asyncio.gather(*requests)

        self.assertEqual(calls, 1)
        self.assertEqual(len(self.service._in_flight_rebuilds), 0)
        self.assertEqual({key for _, key in results}, {"project-1:main@commit-1"})

    async def test_rebuild_failure_is_not_cached_and_next_request_retries(self) -> None:
        calls = 0

        async def handler(project_id: str) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("rebuild boom")

        self.service.set_rebuild_handler(handler)
        results = await asyncio.gather(
            *(
                self.service._get_active_instance_or_rebuild("project-1")
                for _ in range(3)
            ),
            return_exceptions=True,
        )

        self.assertEqual(calls, 1)
        self.assertTrue(all(isinstance(result, NoActiveInstanceError) for result in results))
        self.assertEqual(self.service._in_flight_rebuilds, {})

        with self.assertRaises(NoActiveInstanceError):
            await self.service._get_active_instance_or_rebuild("project-1")
        self.assertEqual(calls, 2)
        self.assertEqual(self.service._in_flight_rebuilds, {})

    async def test_cancelled_waiter_does_not_cancel_shared_rebuild(self) -> None:
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(project_id: str) -> None:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            handle = await self.service.create_instance(
                project_id, "main", "commit-1", staging=True,
            )
            await self.service.set_active_handle(handle)

        self.service.set_rebuild_handler(handler)
        cancelled_request = asyncio.create_task(
            self.service._get_active_instance_or_rebuild("project-1")
        )
        successful_request = asyncio.create_task(
            self.service._get_active_instance_or_rebuild("project-1")
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        cancelled_request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled_request

        release.set()
        _, key = await successful_request
        self.assertEqual(key, "project-1:main@commit-1")
        self.assertEqual(calls, 1)
        self.assertEqual(self.service._in_flight_rebuilds, {})

    async def test_different_projects_rebuild_in_parallel(self) -> None:
        calls = {"project-1": 0, "project-2": 0}
        started = {"project-1": asyncio.Event(), "project-2": asyncio.Event()}
        release = {"project-1": asyncio.Event(), "project-2": asyncio.Event()}

        async def handler(project_id: str) -> None:
            calls[project_id] += 1
            started[project_id].set()
            await release[project_id].wait()
            handle = await self.service.create_instance(
                project_id, "main", f"commit-{project_id}", staging=True,
            )
            await self.service.set_active_handle(handle)

        self.service.set_rebuild_handler(handler)
        requests = [
            asyncio.create_task(
                self.service._get_active_instance_or_rebuild(project_id)
            )
            for project_id in ("project-1", "project-2")
        ]
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in started.values())), timeout=5,
        )
        self.assertEqual(calls, {"project-1": 1, "project-2": 1})

        for event in release.values():
            event.set()
        await asyncio.wait_for(asyncio.gather(*requests), timeout=5)

    async def test_loading_instances_are_not_evicted_when_cache_is_full(self) -> None:
        handles = [
            await self.service.create_instance(
                f"project-{index}", "main", f"commit-{index}", staging=True,
            )
            for index in (1, 2)
        ]
        third = await self.service.create_instance(
            "project-3", "main", "commit-3", staging=True,
        )

        self.assertEqual(len(self.service._cache), 3)
        self.assertTrue(set(handles + [third]).issubset(self.service._cache))
        self.assertEqual(self.service._loading, set(handles + [third]))

    async def test_inactive_non_loading_instance_is_evicted_before_active(self) -> None:
        active = await self.service.create_instance(
            "project-1", "main", "commit-1",
        )
        await self.service.set_active_instance("project-1", "main", "commit-1")
        inactive = await self.service.create_instance(
            "project-2", "main", "commit-2",
        )
        await self.service.create_instance(
            "project-3", "main", "commit-3",
        )

        self.assertNotIn(inactive, self.service._cache)
        self.assertIn(active, self.service._cache)
        self.assertEqual(self.service._active["project-1"], active)

    async def test_destroy_inactive_skips_loading_but_destroys_old_instances(self) -> None:
        self.service._config.cache.max_branches = 3
        active = await self.service.create_instance(
            "project-1", "main", "commit-1",
        )
        await self.service.set_active_instance("project-1", "main", "commit-1")
        old = await self.service.create_instance(
            "project-1", "dev", "commit-2",
        )
        loading = await self.service.create_instance(
            "project-1", "feature", "commit-3", staging=True,
        )

        removed = await self.service.destroy_inactive_instances("project-1")

        self.assertEqual(removed, 1)
        self.assertIn(active, self.service._cache)
        self.assertNotIn(old, self.service._cache)
        self.assertIn(loading, self.service._cache)
        self.assertIn(loading, self.service._loading)

    async def test_promote_or_destroy_clears_loading_marker(self) -> None:
        loading = await self.service.create_instance(
            "project-1", "main", "commit-1", staging=True,
        )
        await self.service.set_active_handle(loading)
        self.assertNotIn(loading, self.service._loading)
        self.assertNotIn(loading, self.service._cache)

        second_loading = await self.service.create_instance(
            "project-1", "feature", "commit-2", staging=True,
        )
        await self.service.destroy_instance(second_loading)
        self.assertNotIn(second_loading, self.service._loading)

    async def test_shutdown_cancels_inflight_rebuild_and_wakes_waiter(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(project_id: str) -> None:
            await self.service.create_instance(
                project_id, "main", "commit-1", staging=True,
            )
            started.set()
            await release.wait()

        self.service.set_rebuild_handler(handler)
        request = asyncio.create_task(
            self.service._get_active_instance_or_rebuild("project-1")
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        await self.service.shutdown()
        with self.assertRaises(NoActiveInstanceError):
            await request
        self.assertEqual(self.service._in_flight_rebuilds, {})
        self.assertEqual(self.service._loading, set())

    async def test_original_two_project_burst_has_one_rebuild_per_project(self) -> None:
        calls = {"project-1": 0, "project-2": 0}
        started = {"project-1": asyncio.Event(), "project-2": asyncio.Event()}
        release = asyncio.Event()

        async def handler(project_id: str) -> None:
            calls[project_id] += 1
            handle = await self.service.create_instance(
                project_id, "main", f"commit-{project_id}", staging=True,
            )
            started[project_id].set()
            await release.wait()
            await self.service.upsert_points(handle, [_point(project_id)])
            await self.service.set_active_handle(handle)
            await self.service.destroy_inactive_instances(project_id)

        self.service.set_rebuild_handler(handler)
        requests = []
        for project_id in calls:
            for index in range(3):
                if index == 0:
                    query = self.service.dense_search(project_id, [1.0, 0.0])
                else:
                    query = self.service.hybrid_search(
                        project_id, [1.0, 0.0],
                        SparseVector(indices=[1], values=[1.0]),
                    )
                requests.append(asyncio.create_task(query))

        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in started.values())), timeout=5,
        )
        self.assertEqual(calls, {"project-1": 1, "project-2": 1})
        self.assertEqual(len(self.service._loading), 2)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*requests), timeout=5)

        self.assertEqual(len(results), 6)
        for project_id, project_results in zip(
            ["project-1"] * 3 + ["project-2"] * 3, results, strict=True,
        ):
            self.assertEqual(len(project_results), 1)
            self.assertEqual(project_results[0].file_path, f"{project_id}/example.py")
        self.assertEqual(self.service._in_flight_rebuilds, {})
        self.assertEqual(self.service._loading, set())
        self.assertEqual(set(self.service._active), {"project-1", "project-2"})

    async def test_idle_eviction_skips_loading_and_preserves_protected_active(self) -> None:
        active = await self.service.create_instance("project-1", "main", "old")
        await self.service.set_active_handle(active)
        loading = await self.service.create_instance(
            "project-1", "main", "new", staging=True,
        )
        self.service._config.cache.idle_timeout_minutes = 1
        old_time = datetime.now(UTC) - timedelta(minutes=2)
        self.service._cache[active].last_accessed = old_time
        self.service._cache[loading].last_accessed = old_time

        replacement = await self.service.create_instance(
            "project-1", "main", "newer", staging=True,
        )
        self.assertEqual(set(self.service._cache), {active, loading, replacement})
        self.assertEqual(self.service._active["project-1"], active)

    async def test_non_staging_creation_fails_instead_of_evicting_loading(self) -> None:
        handles = {
            await self.service.create_instance(
                project_id, "main", "commit", staging=True,
            )
            for project_id in ("project-1", "project-2")
        }
        with self.assertRaises(CacheFullError):
            await self.service.create_instance("project-3", "main", "commit")
        self.assertEqual(set(self.service._cache), handles)
        self.assertEqual(self.service._loading, handles)

    async def test_promote_cleanup_does_not_destroy_another_loading_chain(self) -> None:
        first = await self.service.create_instance(
            "project-1", "main", "first", staging=True,
        )
        second = await self.service.create_instance(
            "project-1", "main", "second", staging=True,
        )
        await self.service.upsert_points(first, [_point("project-1")])
        await self.service.set_active_handle(first)
        self.assertEqual(await self.service.destroy_inactive_instances("project-1"), 0)
        # A workflow-like chain can finish loading after its peer promotes.
        await self.service.upsert_points(second, [_point("project-1")])
        await self.service.set_active_handle(second)
        self.assertEqual(await self.service.destroy_inactive_instances("project-1"), 1)
        self.assertEqual(self.service._loading, set())
        self.assertEqual(set(self.service._cache), {"project-1:main@second"})

    async def test_destroy_all_clears_only_target_project_loading_markers(self) -> None:
        first = await self.service.create_instance(
            "project-1", "main", "commit", staging=True,
        )
        second = await self.service.create_instance(
            "project-2", "main", "commit", staging=True,
        )
        self.assertEqual(await self.service.destroy_all_instances("project-1"), 1)
        self.assertNotIn(first, self.service._cache)
        self.assertEqual(self.service._loading, {second})


if __name__ == "__main__":
    unittest.main()
