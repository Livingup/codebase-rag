"""Memory Qdrant Service — in-memory repo-copy-specific vector search.

Manages per-repository-copy in-memory Qdrant instances with LRU caching.
All semantic search happens here, not on Qdrant Server.

Instance identity is the canonical repo_key of the local Git repository
copy (plus branch@commit), so multiple copies of the same project —
registered under one project_id — never share an active instance and
never topple each other's search results.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Set, Tuple

from qdrant_client import AsyncQdrantClient, models

from codebase_rag.common.logging import get_logger
from codebase_rag.common.types import SparseVector
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import MemoryQdrantConfig
from codebase_rag.services.memory_qdrant.errors import (
    CacheFullError,
    InstanceNotFoundError,
    MemoryInstanceError,
    NoActiveInstanceError,
)
from codebase_rag.services.memory_qdrant.models import (
    ActiveInstanceInfo,
    CacheEntry,
    SearchFilters,
    SearchResult,
)

logger = get_logger(__name__)


# Internal handle: cache key string
InstanceHandle = str


def _cache_key(repo_key: str, branch_name: str, commit_hash: str) -> str:
    return f"{repo_key}:{branch_name}@{commit_hash}"


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    """Retrieve an unobserved task exception to suppress asyncio warnings."""
    if not task.cancelled():
        task.exception()


class _CachedInstance:
    """Internal wrapper for an in-memory Qdrant instance."""

    __slots__ = ("client", "repo_key", "project_id", "branch_name",
                 "commit_hash", "last_accessed", "last_search_time",
                 "point_count")

    def __init__(
        self,
        client: AsyncQdrantClient,
        repo_key: str,
        project_id: str,
        branch_name: str,
        commit_hash: str,
    ) -> None:
        self.client = client
        self.repo_key = repo_key
        self.project_id = project_id
        self.branch_name = branch_name
        self.commit_hash = commit_hash
        self.last_accessed = datetime.now(timezone.utc)
        self.last_search_time: Optional[datetime] = None
        self.point_count = 0

    def touch(self) -> None:
        self.last_accessed = datetime.now(timezone.utc)

    def touch_search(self) -> None:
        """Mark this instance as recently used for search."""
        now = datetime.now(timezone.utc)
        self.last_accessed = now
        self.last_search_time = now


class MemoryQdrantService:
    """In-memory repo-copy-specific Qdrant instances with LRU caching + hybrid search.

    Args:
        config: Memory Qdrant configuration section from AppConfig.
    """

    # Type: async callback that rebuilds the memory instance for a repo copy.
    # Receives the repo_key; expected to call create_instance +
    # upsert_points + set_active_instance.
    RebuildHandler = Callable[[str], Awaitable[None]]

    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        # LRU cache: OrderedDict preserves insertion/access order
        self._cache: OrderedDict[str, _CachedInstance] = OrderedDict()
        # Staging handles currently loading and protected from cache cleanup
        self._loading: set[str] = set()
        # Active instance per repository copy: repo_key → cache_key
        self._active: Dict[str, str] = {}
        # One shared lazy rebuild task per repo_key
        self._in_flight_rebuilds: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._initialized = False
        # Lazy rebuild: called when search finds no active instance
        self._rebuild_handler: Optional[MemoryQdrantService.RebuildHandler] = None
        self._idle_sweep_task: Optional[asyncio.Task] = None

    @property
    def _config(self) -> MemoryQdrantConfig:
        return self._config_manager.config.memory_qdrant

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        logger.info("memory_qdrant_service_initializing")
        self._initialized = True

        # Start idle sweep if timeout configured
        idle_min = self._config.cache.idle_timeout_minutes
        if idle_min > 0:
            self._idle_sweep_task = asyncio.create_task(self._idle_sweep_loop())

        logger.info(
            "memory_qdrant_service_initialized",
            max_branches=self._config.cache.max_branches,
            idle_timeout_minutes=idle_min,
        )

    async def shutdown(self) -> None:
        """Release all in-memory instances."""
        if self._idle_sweep_task is not None:
            self._idle_sweep_task.cancel()
            try:
                await self._idle_sweep_task
            except asyncio.CancelledError:
                pass
            self._idle_sweep_task = None

        tasks = list(self._in_flight_rebuilds.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._in_flight_rebuilds.clear()

        async with self._lock:
            for key, inst in self._cache.items():
                await inst.client.close()
                logger.info("instance_released", cache_key=key)
            self._cache.clear()
            self._active.clear()
            self._loading.clear()
        self._initialized = False
        logger.info("memory_qdrant_service_shutdown")

    def is_healthy(self) -> bool:
        return self._initialized

    def set_rebuild_handler(self, handler: RebuildHandler) -> None:
        """Register a callback for lazy rebuild when search finds no active instance.

        The handler receives the ``repo_key`` of the requesting local
        repository copy and is expected to create the in-memory instance
        for that copy, load data from Qdrant Server, and call
        ``set_active_instance``.
        """
        self._rebuild_handler = handler
        logger.info("rebuild_handler_registered")

    # =========================================================================
    # 2.1 Create in-memory instance
    # =========================================================================

    async def create_instance(
        self,
        repo_key: str,
        branch_name: str,
        commit_hash: str,
        *,
        project_id: Optional[str] = None,
        staging: bool = False,
    ) -> InstanceHandle:
        """Create an in-memory Qdrant instance and register in LRU cache.

        Args:
            repo_key: Canonical identity key of the local repository copy.
            branch_name / commit_hash: HEAD snapshot the instance serves.
            project_id: Data-partition id kept as instance metadata (used
                by project-level destroys).  Defaults to ``repo_key``.
            staging: Staging instances get a unique key and are protected
                from eviction until promoted.

        Returns:
            InstanceHandle (cache key string) for subsequent operations.
        """
        self._check_initialized()
        base_key = _cache_key(repo_key, branch_name, commit_hash)
        key = f"{base_key}#staging:{uuid.uuid4()}" if staging else base_key

        async with self._lock:
            # Already cached? Only non-staging instances reuse the stable branch@commit key.
            if not staging and key in self._cache:
                self._cache[key].touch()
                self._cache.move_to_end(key)
                logger.info("instance_cache_hit", cache_key=key)
                return key

            # Evict if cache full. A staged replacement must never evict the
            # same copy's current active instance before successful promote.
            await self._evict_if_needed(
                protected_repo_key=repo_key if staging else None,
                allow_over_capacity=staging,
            )

            # Create in-memory client
            client = AsyncQdrantClient(location=":memory:")
            inst_cfg = self._config.instance

            # Distance mapping
            distance_map = {
                "cosine": models.Distance.COSINE,
                "euclid": models.Distance.EUCLID,
                "dot": models.Distance.DOT,
            }
            distance = distance_map.get(
                inst_cfg.dense_vector.distance.lower(), models.Distance.COSINE
            )

            # HNSW config (enabled for in-memory search)
            hnsw = None
            if inst_cfg.hnsw_config.enabled:
                hnsw = models.HnswConfigDiff(
                    m=inst_cfg.hnsw_config.m,
                    ef_construct=inst_cfg.hnsw_config.ef_construct,
                )

            vectors_config = {
                inst_cfg.dense_vector.name: models.VectorParams(
                    size=inst_cfg.dense_vector.size,
                    distance=distance,
                    hnsw_config=hnsw,
                ),
            }
            sparse_vectors_config = {
                inst_cfg.sparse_vector.name: models.SparseVectorParams(),
            }

            await client.create_collection(
                collection_name=inst_cfg.collection_name,
                vectors_config=vectors_config,
                sparse_vectors_config=sparse_vectors_config,
            )

            # Register in cache
            cached = _CachedInstance(
                client, repo_key,
                project_id if project_id is not None else repo_key,
                branch_name, commit_hash,
            )
            self._cache[key] = cached
            if staging:
                self._loading.add(key)

            logger.info(
                "instance_created",
                cache_key=key,
                staging=staging,
                dense_dim=inst_cfg.dense_vector.size,
            )
            return key

    # =========================================================================
    # 2.2 Upsert points
    # =========================================================================

    async def upsert_points(
        self, handle: InstanceHandle, points: List[models.PointStruct]
    ) -> bool:
        """Write Points into an in-memory instance."""
        inst = self._get_instance(handle)
        coll = self._config.instance.collection_name

        try:
            await inst.client.upsert(collection_name=coll, points=points)
            inst.point_count += len(points)
            inst.touch()
            logger.debug(
                "memory_upsert_points",
                handle=handle,
                points_count=len(points),
                total_points=inst.point_count,
            )
            return True
        except Exception as e:
            logger.error(
                "memory_upsert_points_failed",
                handle=handle,
                points_count=len(points),
                error=str(e),
            )
            raise MemoryInstanceError(f"Upsert failed on {handle}: {e}") from e

    # =========================================================================
    # 2.3 Delete points by file_version_id
    # =========================================================================

    async def delete_points_by_file_version(
        self, handle: InstanceHandle, file_version_ids: List[str]
    ) -> bool:
        """Delete Points matching any of the given file_version_ids."""
        inst = self._get_instance(handle)
        coll = self._config.instance.collection_name

        try:
            await inst.client.delete(
                collection_name=coll,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="file_version_id",
                                match=models.MatchAny(any=file_version_ids),
                            ),
                        ]
                    )
                ),
            )
            inst.touch()
            return True
        except Exception as e:
            raise MemoryInstanceError(
                f"Delete by file_version failed on {handle}: {e}"
            ) from e

    # =========================================================================
    # 2.4 Get all file_version_ids
    # =========================================================================

    async def get_all_file_version_ids(self, handle: InstanceHandle) -> Set[str]:
        """Scroll through instance to extract all unique file_version_ids."""
        inst = self._get_instance(handle)
        coll = self._config.instance.collection_name
        fvids: Set[str] = set()
        offset = None

        while True:
            records, next_offset = await inst.client.scroll(
                collection_name=coll,
                limit=100,
                offset=offset,
                with_vectors=False,
                with_payload=["file_version_id"],
            )
            for r in records:
                fvid = r.payload.get("file_version_id")
                if fvid:
                    fvids.add(fvid)
            if next_offset is None:
                break
            offset = next_offset

        inst.touch()
        return fvids

    # =========================================================================
    # 2.5 Hybrid search (RRF fusion)
    # =========================================================================

    async def hybrid_search(
        self,
        repo_key: str,
        query_dense: List[float],
        query_sparse: SparseVector,
        top_k: Optional[int] = None,
        filters: Optional[SearchFilters] = None,
    ) -> List[SearchResult]:
        """Hybrid search with Prefetch + RRF fusion on the copy's active instance."""
        self._check_initialized()
        inst, handle = await self._get_active_instance_or_rebuild(repo_key)
        coll = self._config.instance.collection_name
        search_cfg = self._config.search

        k = min(top_k or search_cfg.default_top_k, search_cfg.max_top_k)
        prefetch_limit = search_cfg.prefetch_limit

        # Build Qdrant filter
        qdrant_filter = self._build_filter(filters)

        dense_name = self._config.instance.dense_vector.name
        sparse_name = self._config.instance.sparse_vector.name

        start = time.monotonic()

        # Prefetch 1: dense search
        dense_results = await inst.client.query_points(
            collection_name=coll,
            query=query_dense,
            using=dense_name,
            limit=prefetch_limit,
            query_filter=qdrant_filter,
            with_payload=True,
        )

        # Prefetch 2: sparse search
        sparse_results = await inst.client.query_points(
            collection_name=coll,
            query=models.SparseVector(
                indices=query_sparse.indices,
                values=query_sparse.values,
            ),
            using=sparse_name,
            limit=prefetch_limit,
            query_filter=qdrant_filter,
            with_payload=True,
        )

        dense_hit_count = len(dense_results.points)
        sparse_hit_count = len(sparse_results.points)

        # RRF fusion
        fused = self._rrf_fusion(
            dense_results.points,
            sparse_results.points,
            search_cfg.dense_weight,
            search_cfg.sparse_weight,
            search_cfg.rrf_k,
        )

        # Return sorted top_k — no threshold filtering at search layer
        results = []
        for point_id, score, payload in fused[:k]:
            results.append(SearchResult(
                score=score,
                file_version_id=payload.get("file_version_id", ""),
                file_path=payload.get("file_path", ""),
                entity_type=payload.get("entity_type", ""),
                entity_name=payload.get("entity_name", ""),
                business_description=payload.get("business_description", ""),
            ))

        elapsed = time.monotonic() - start
        logger.info(
            "hybrid_search",
            repo_key=repo_key,
            top_k=k,
            dense_hits=dense_hit_count,
            sparse_hits=sparse_hit_count,
            fused_candidates=len(fused),
            results=len(results),
            top_score=round(results[0].score, 4) if results else 0,
            elapsed_ms=round(elapsed * 1000, 1),
        )

        inst.touch_search()
        return results

    # =========================================================================
    # 2.6 Dense-only search
    # =========================================================================

    async def dense_search(
        self,
        repo_key: str,
        query_dense: List[float],
        top_k: Optional[int] = None,
        filters: Optional[SearchFilters] = None,
    ) -> List[SearchResult]:
        """Dense-only vector search on the copy's active instance (no RRF)."""
        self._check_initialized()
        inst, handle = await self._get_active_instance_or_rebuild(repo_key)
        coll = self._config.instance.collection_name
        search_cfg = self._config.search

        k = min(top_k or search_cfg.default_top_k, search_cfg.max_top_k)
        qdrant_filter = self._build_filter(filters)
        dense_name = self._config.instance.dense_vector.name

        results_raw = await inst.client.query_points(
            collection_name=coll,
            query=query_dense,
            using=dense_name,
            limit=k,
            query_filter=qdrant_filter,
            with_payload=True,
        )

        results = []
        for pt in results_raw.points:
            score = pt.score if hasattr(pt, "score") else 0.0
            p = pt.payload or {}
            results.append(SearchResult(
                score=score,
                file_version_id=p.get("file_version_id", ""),
                file_path=p.get("file_path", ""),
                entity_type=p.get("entity_type", ""),
                entity_name=p.get("entity_name", ""),
                business_description=p.get("business_description", ""),
            ))

        inst.touch_search()
        return results

    # =========================================================================
    # 2.7 Active instance management
    # =========================================================================

    async def set_active_instance(
        self, repo_key: str, branch_name: str, commit_hash: str
    ) -> None:
        """Set a cached instance as the active search target for a repo copy. O(1)."""
        key = _cache_key(repo_key, branch_name, commit_hash)
        async with self._lock:
            if key not in self._cache:
                raise InstanceNotFoundError(
                    f"Instance not in cache: {key}. Create it first."
                )
            self._active[repo_key] = key
            self._cache[key].touch()
            self._cache.move_to_end(key)

        logger.info(
            "active_instance_set",
            repo_key=repo_key,
            cache_key=key,
            point_count=self._cache[key].point_count,
        )

    async def get_active_instance_info(
        self, repo_key: str
    ) -> Optional[ActiveInstanceInfo]:
        """Return info about the active instance for a repo copy, or None."""
        key = self._active.get(repo_key)
        if not key or key not in self._cache:
            return None

        inst = self._cache[key]
        # Rough memory estimate: dense_dim * 4 bytes * points + overhead
        dim = self._config.instance.dense_vector.size
        mem_mb = (dim * 4 * inst.point_count) / (1024 * 1024) * 1.5  # 1.5x overhead

        return ActiveInstanceInfo(
            branch_name=inst.branch_name,
            commit_hash=inst.commit_hash,
            point_count=inst.point_count,
            memory_usage_mb=round(mem_mb, 2),
        )

    async def set_active_handle(self, handle: InstanceHandle) -> None:
        """Promote an already-loaded cached handle as the active search target."""
        async with self._lock:
            if handle not in self._cache:
                raise InstanceNotFoundError(
                    f"Instance not in cache: {handle}. Create it first."
                )
            inst = self._cache[handle]
            self._loading.discard(handle)
            active_key = _cache_key(inst.repo_key, inst.branch_name, inst.commit_hash)
            if handle != active_key:
                existing = self._cache.pop(active_key, None)
                if existing is not None:
                    await existing.client.close()
                    logger.info("instance_replaced", cache_key=active_key)
                self._cache[active_key] = self._cache.pop(handle)
                handle = active_key
            self._active[inst.repo_key] = handle
            inst.touch()
            self._cache.move_to_end(handle)

        logger.info(
            "active_instance_set",
            repo_key=inst.repo_key,
            cache_key=handle,
            point_count=inst.point_count,
        )

    # =========================================================================
    # 2.8 Cache query
    # =========================================================================

    async def get_cached_instance(
        self, repo_key: str, branch_name: str, commit_hash: str
    ) -> Optional[InstanceHandle]:
        """Check if a specific branch@commit instance is cached for a repo copy."""
        key = _cache_key(repo_key, branch_name, commit_hash)
        async with self._lock:
            if key in self._cache:
                self._cache[key].touch()
                self._cache.move_to_end(key)
                logger.info("cache_query_hit", cache_key=key)
                return key
        logger.info("cache_query_miss", cache_key=key)
        return None

    async def get_cache_status(self, repo_key: str) -> List[CacheEntry]:
        """Return cache entries for a repository copy."""
        entries: List[CacheEntry] = []
        for key, inst in self._cache.items():
            if inst.repo_key != repo_key:
                continue
            entries.append(CacheEntry(
                branch_name=inst.branch_name,
                commit_hash=inst.commit_hash,
                point_count=inst.point_count,
                last_accessed=inst.last_accessed,
                is_active=(self._active.get(repo_key) == key),
            ))
        return entries

    # =========================================================================
    # 2.9 Destroy all instances for a project
    # =========================================================================

    async def destroy_all_instances(self, project_id: str) -> int:
        """Close and remove all cached instances for a project (all copies).

        Used when a project partition is unregistered or rebranded.

        Returns:
            Number of instances destroyed.
        """
        async with self._lock:
            targets = [
                (key, inst) for key, inst in self._cache.items()
                if inst.project_id == project_id
            ]
            keys_to_remove = [key for key, _ in targets]
            removed = await self._destroy_keys_locked(keys_to_remove)
            for _, inst in targets:
                self._active.pop(inst.repo_key, None)

        logger.info(
            "all_instances_destroyed",
            project_id=project_id,
            count=removed,
        )
        return removed

    async def destroy_repo_instances(self, repo_key: str) -> int:
        """Close and remove all cached instances for one repository copy.

        Used when a single local copy is unregistered; other copies of
        the same project keep their instances.
        """
        async with self._lock:
            keys_to_remove = [
                key for key, inst in self._cache.items()
                if inst.repo_key == repo_key
            ]
            removed = await self._destroy_keys_locked(keys_to_remove)
            self._active.pop(repo_key, None)

        logger.info(
            "repo_instances_destroyed",
            repo_key=repo_key,
            count=removed,
        )
        return removed

    async def destroy_instance(self, handle: InstanceHandle) -> bool:
        """Close and remove one cached instance by handle."""
        async with self._lock:
            if handle not in self._cache:
                return False
            inst = self._cache[handle]
            await self._destroy_keys_locked([handle])
            if self._active.get(inst.repo_key) == handle:
                self._active.pop(inst.repo_key, None)

        logger.info("instance_destroyed", cache_key=handle)
        return True

    async def destroy_inactive_instances(self, repo_key: str) -> int:
        """Close inactive instances of a repo copy, preserving any still loading."""
        async with self._lock:
            active_key = self._active.get(repo_key)
            skipped_loading = sum(
                1
                for key, inst in self._cache.items()
                if (
                    inst.repo_key == repo_key
                    and key != active_key
                    and key in self._loading
                )
            )
            keys_to_remove = [
                key for key, inst in self._cache.items()
                if (
                    inst.repo_key == repo_key
                    and key != active_key
                    and key not in self._loading
                )
            ]
            removed = await self._destroy_keys_locked(keys_to_remove)

        logger.info(
            "inactive_instances_destroyed",
            repo_key=repo_key,
            count=removed,
            skipped_loading=skipped_loading,
        )
        return removed

    # =========================================================================
    # 2.10 Update cache commit hash
    # =========================================================================

    async def update_cache_commit(
        self,
        repo_key: str,
        branch_name: str,
        old_commit_hash: str,
        new_commit_hash: str,
    ) -> bool:
        """In-place update of cache key when commit hash changes."""
        old_key = _cache_key(repo_key, branch_name, old_commit_hash)
        new_key = _cache_key(repo_key, branch_name, new_commit_hash)

        async with self._lock:
            if old_key not in self._cache:
                return False

            inst = self._cache.pop(old_key)
            inst.commit_hash = new_commit_hash
            inst.touch()
            self._cache[new_key] = inst

            # Update active reference if needed
            if self._active.get(repo_key) == old_key:
                self._active[repo_key] = new_key

        logger.info(
            "cache_commit_updated",
            repo_key=repo_key,
            branch=branch_name,
            old_commit=old_commit_hash[:8],
            new_commit=new_commit_hash[:8],
        )
        return True

    # =========================================================================
    # Utility
    # =========================================================================

    async def get_total_memory_usage_mb(self) -> float:
        """Estimate total memory usage across all cached instances."""
        dim = self._config.instance.dense_vector.size
        total_points = sum(inst.point_count for inst in self._cache.values())
        return round((dim * 4 * total_points) / (1024 * 1024) * 1.5, 2)

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _check_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "MemoryQdrantService is not initialized. Call initialize() first."
            )

    def _get_instance(self, handle: InstanceHandle) -> _CachedInstance:
        """Get cached instance by handle, raise if not found."""
        inst = self._cache.get(handle)
        if inst is None:
            raise InstanceNotFoundError(f"Instance not found or evicted: {handle}")
        return inst

    async def _destroy_keys_locked(self, keys: List[str]) -> int:
        """Close and remove cached instances. Caller must hold ``_lock``."""
        removed = 0
        for key in keys:
            inst = self._cache.pop(key, None)
            self._loading.discard(key)
            if inst is None:
                continue
            await inst.client.close()
            logger.info("instance_destroyed", cache_key=key)
            removed += 1
        return removed

    async def _run_rebuild(
        self,
        repo_key: str,
        handler: RebuildHandler,
    ) -> None:
        """Run one repo copy's lazy rebuild and release its single-flight slot."""
        try:
            await handler(repo_key)
        finally:
            if self._in_flight_rebuilds.get(repo_key) is asyncio.current_task():
                del self._in_flight_rebuilds[repo_key]

    async def _get_active_instance_or_rebuild(
        self, repo_key: str,
    ) -> Tuple[_CachedInstance, str]:
        """Get the active instance for a repo copy, with lazy rebuild fallback.

        If no active instance exists but a rebuild handler is registered,
        invoke it to recreate the instance from Qdrant Server, then retry.
        Concurrent requests for one copy share the same rebuild task.
        """
        inst, key = self._try_get_active(repo_key)
        if inst is not None:
            return inst, key

        # No active instance — attempt lazy rebuild
        handler = self._rebuild_handler
        if handler is not None:
            task = self._in_flight_rebuilds.get(repo_key)
            if task is None:
                logger.info(
                    "lazy_rebuild_triggered",
                    repo_key=repo_key,
                )
                task = asyncio.create_task(
                    self._run_rebuild(repo_key, handler),
                    name=f"lazy_rebuild:{repo_key}",
                )
                task.add_done_callback(_consume_task_exception)
                self._in_flight_rebuilds[repo_key] = task
            else:
                logger.info(
                    "lazy_rebuild_joined",
                    repo_key=repo_key,
                )

            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if task.cancelled() and (current is None or current.cancelling() == 0):
                    raise NoActiveInstanceError(
                        f"No active instance for repository '{repo_key}' "
                        "and lazy rebuild was cancelled."
                    ) from None
                raise
            except Exception as e:
                logger.warning(
                    "lazy_rebuild_failed",
                    repo_key=repo_key,
                    error=str(e)[:200],
                )
                raise NoActiveInstanceError(
                    f"No active instance for repository '{repo_key}' "
                    f"and lazy rebuild failed: {e}"
                ) from e

            # Retry after rebuild
            inst, key = self._try_get_active(repo_key)
            if inst is not None:
                logger.info(
                    "lazy_rebuild_success",
                    repo_key=repo_key,
                    cache_key=key,
                )
                return inst, key

        raise NoActiveInstanceError(
            f"No active instance for repository '{repo_key}'."
        )

    def _try_get_active(self, repo_key: str) -> Tuple[Optional[_CachedInstance], str]:
        """Try to get the active instance. Returns (None, '') if not found."""
        key = self._active.get(repo_key, "")
        if not key:
            return None, ""
        inst = self._cache.get(key)
        if inst is None:
            # Active reference points to evicted instance — clean up stale ref
            del self._active[repo_key]
            return None, ""
        return inst, key

    async def _evict_if_needed(
        self,
        protected_repo_key: Optional[str] = None,
        *,
        allow_over_capacity: bool = False,
    ) -> None:
        """Evict LRU instance if cache is full. Must hold lock.

        Eviction priority (loading staging instances are never eligible):
          1. Oldest non-active instance that is not loading
          2. Oldest idle active instance (search idle >= idle_timeout_minutes)

        ``protected_repo_key`` prevents staging reloads from evicting the same
        copy's active instance before a replacement has loaded successfully.
        When no safe victim exists, ``allow_over_capacity`` lets a staging
        replacement exceed the configured cache limit. Promotion does not
        proactively shrink the cache; normal cleanup/eviction still applies.
        """
        if len(self._cache) < self._config.cache.max_branches:
            return

        active_keys = set(self._active.values())
        protected_active_key = (
            self._active.get(protected_repo_key)
            if protected_repo_key is not None
            else None
        )

        # Priority 1: Find oldest non-active entry that is not still loading
        for key in self._cache:
            if key not in active_keys and key not in self._loading:
                await self._evict_instance(key)
                return

        # Priority 2: Find oldest idle active instance
        idle_min = self._config.cache.idle_timeout_minutes
        if idle_min > 0:
            now = datetime.now(timezone.utc)
            for key in self._cache:
                if (
                    key in self._loading
                    or key not in active_keys
                    or key == protected_active_key
                ):
                    continue
                inst = self._cache[key]
                idle_since = inst.last_search_time or inst.last_accessed
                idle_seconds = (now - idle_since).total_seconds()
                if idle_seconds >= idle_min * 60:
                    # Remove from active before evicting
                    self._active.pop(inst.repo_key, None)
                    await self._evict_instance(key)
                    return

        if allow_over_capacity:
            logger.warning(
                "cache_over_capacity_allowed_for_staging",
                cache_size=len(self._cache),
                max_branches=self._config.cache.max_branches,
                protected_repo_key=protected_repo_key,
            )
            return

        raise CacheFullError(
            f"Cache full ({len(self._cache)} instances) and all are active. "
            f"Cannot evict."
        )

    async def _evict_instance(self, key: str) -> None:
        """Evict a single instance by cache key. Must hold lock."""
        inst = self._cache.pop(key)
        self._loading.discard(key)
        await inst.client.close()
        logger.info(
            "instance_evicted",
            cache_key=key,
            repo_key=inst.repo_key,
            branch=inst.branch_name,
            points=inst.point_count,
        )

    async def _idle_sweep_loop(self) -> None:
        """Periodically evict active instances that have been idle too long.

        Frees memory for repository copies that are registered but not
        actively searched. The instance will be lazily rebuilt from
        Qdrant Server on next search.
        """
        idle_min = self._config.cache.idle_timeout_minutes
        # Check every 1/3 of the timeout period, minimum 60s
        check_interval = max(idle_min * 60 / 3, 60)

        try:
            while True:
                await asyncio.sleep(check_interval)
                await self._sweep_idle_instances()
        except asyncio.CancelledError:
            return

    async def _sweep_idle_instances(self) -> None:
        """Evict all active instances that exceed idle timeout."""
        idle_min = self._config.cache.idle_timeout_minutes
        if idle_min <= 0:
            return

        now = datetime.now(timezone.utc)
        threshold_s = idle_min * 60

        async with self._lock:
            idle_keys = []
            for key, inst in self._cache.items():
                # Only sweep active instances; non-active are already LRU-managed
                if self._active.get(inst.repo_key) != key:
                    continue
                idle_since = inst.last_search_time or inst.last_accessed
                if (now - idle_since).total_seconds() >= threshold_s:
                    idle_keys.append(key)

            for key in idle_keys:
                inst = self._cache[key]
                self._active.pop(inst.repo_key, None)
                await self._evict_instance(key)
                logger.info(
                    "idle_sweep_evicted",
                    repo_key=inst.repo_key,
                    branch=inst.branch_name,
                    idle_minutes=idle_min,
                )

    def _build_filter(self, filters: Optional[SearchFilters]) -> Optional[models.Filter]:
        """Build Qdrant Filter from SearchFilters."""
        if not filters:
            return None

        conditions: List[models.Condition] = []

        if filters.entity_types:
            conditions.append(models.FieldCondition(
                key="entity_type",
                match=models.MatchAny(any=filters.entity_types),
            ))
        if filters.file_paths:
            canon_fps = [fp.replace("\\", "/").strip().lstrip("/") for fp in filters.file_paths]
            conditions.append(models.FieldCondition(
                key="file_path",
                match=models.MatchAny(any=canon_fps),
            ))
        if filters.entity_names:
            conditions.append(models.FieldCondition(
                key="entity_name",
                match=models.MatchAny(any=filters.entity_names),
            ))

        if not conditions:
            return None

        return models.Filter(must=conditions)

    @staticmethod
    def _rrf_fusion(
        dense_points: list,
        sparse_points: list,
        dense_weight: float,
        sparse_weight: float,
        rrf_k: int,
    ) -> List[Tuple]:
        """Reciprocal Rank Fusion.

        Returns list of (point_id, fused_score, payload) sorted by score desc.
        """
        scores: Dict[str, float] = {}
        payloads: Dict[str, dict] = {}

        # Dense ranking
        for rank, pt in enumerate(dense_points):
            pid = str(pt.id)
            scores[pid] = scores.get(pid, 0.0) + dense_weight / (rrf_k + rank + 1)
            if pid not in payloads:
                payloads[pid] = pt.payload or {}

        # Sparse ranking
        for rank, pt in enumerate(sparse_points):
            pid = str(pt.id)
            scores[pid] = scores.get(pid, 0.0) + sparse_weight / (rrf_k + rank + 1)
            if pid not in payloads:
                payloads[pid] = pt.payload or {}

        # Sort by fused score
        fused = [(pid, score, payloads[pid]) for pid, score in scores.items()]
        fused.sort(key=lambda x: x[1], reverse=True)
        return fused
