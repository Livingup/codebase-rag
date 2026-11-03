from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import defaultdict
from typing import Callable, DefaultDict, List, Optional

from qdrant_client import models

from codebase_rag.common.logging import get_logger
from codebase_rag.config.loader import ConfigManager
from codebase_rag.services.knowledge_sync.models import (
    KnowledgeSyncJob,
    KnowledgeSyncResult,
    KnowledgeSyncSource,
)
from codebase_rag.services.knowledge_sync.store import KnowledgeSyncStore
from codebase_rag.services.memory_qdrant.service import MemoryQdrantService
from codebase_rag.services.qdrant_storage.models import PointData
from codebase_rag.services.qdrant_storage.service import QdrantStorageService

logger = get_logger(__name__)


class KnowledgeSyncService:
    def __init__(
        self,
        config_manager: ConfigManager,
        store: KnowledgeSyncStore,
        qdrant_storage: QdrantStorageService,
        memory_qdrant: MemoryQdrantService,
    ) -> None:
        self._config_manager = config_manager
        self._store = store
        self._qdrant = qdrant_storage
        self._memory = memory_qdrant
        self._version_locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._file_locks: DefaultDict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._retry_availability_provider: Optional[Callable[[], bool]] = None
        self._initialized = False

    async def initialize(self) -> None:
        self._store.ensure_dir()
        self._initialized = True
        logger.info("knowledge_sync_service_initialized")

    async def shutdown(self) -> None:
        self._initialized = False
        logger.info("knowledge_sync_service_shutdown")

    def set_retry_availability_provider(self, provider: Callable[[], bool]) -> None:
        self._retry_availability_provider = provider

    def can_retry_pending(self) -> bool:
        if not self._initialized or self._retry_availability_provider is None:
            return False
        try:
            return self._retry_availability_provider() is True
        except Exception:
            logger.exception("knowledge_sync_retry_availability_failed")
            return False

    async def submit_points(
        self,
        project_id: str,
        file_path: str,
        blob_hash: str,
        file_version_id: str,
        points: List[PointData],
        source: KnowledgeSyncSource,
        old_file_version_id: Optional[str] = None,
        update_latest: bool = True,
        update_memory: bool = True,
        replace_existing_job: bool = False,
        repo_key: Optional[str] = None,
    ) -> KnowledgeSyncResult:
        self._check_initialized()
        file_lock_key = self._file_lock_key(project_id, file_path)
        version_lock_key = self._version_lock_key(project_id, file_version_id)
        async with self._file_locks[file_lock_key]:
            if update_latest:
                self._store.set_latest(project_id, file_path, file_version_id)
                self._store.cancel_stale_file_jobs(
                    project_id, file_path, file_version_id,
                )
            async with self._version_locks[version_lock_key]:
                current = self._store.get_job(project_id, file_version_id)
                if current is not None and not replace_existing_job:
                    if current.status == "cancelled":
                        return KnowledgeSyncResult(
                            file_version_id=file_version_id,
                            points_count=0,
                            published=False,
                            cancelled_stale=True,
                        )
                    result = await self._publish_locked(
                        current,
                        update_memory=update_memory,
                        repo_key=repo_key,
                    )
                    if result.error and update_memory:
                        await self._refresh_memory_best_effort(
                            project_id, file_version_id, current.old_file_version_id,
                            points=current.points, repo_key=repo_key,
                        )
                    return result

                job = KnowledgeSyncJob(
                    project_id=project_id,
                    file_path=file_path,
                    blob_hash=blob_hash,
                    file_version_id=file_version_id,
                    source=source,
                    points=points,
                    old_file_version_id=old_file_version_id,
                )
                self._store.put_job(job)
                result = await self._publish_locked(
                    job, update_memory=update_memory, repo_key=repo_key,
                )
                if result.error and update_memory:
                    await self._refresh_memory_best_effort(
                        project_id, file_version_id, old_file_version_id,
                        points=points, repo_key=repo_key,
                    )
                return result

    async def ensure_published(
        self,
        project_id: str,
        file_version_id: str,
        update_memory: bool = True,
        repo_key: Optional[str] = None,
    ) -> KnowledgeSyncResult:
        self._check_initialized()
        lock_key = self._version_lock_key(project_id, file_version_id)
        async with self._version_locks[lock_key]:
            existing = await self._qdrant.check_existence(project_id, [file_version_id])
            if existing.get(file_version_id, False):
                self._remove_job_best_effort(project_id, file_version_id)
                if update_memory:
                    await self._refresh_memory_best_effort(
                        project_id, file_version_id, repo_key=repo_key,
                    )
                return KnowledgeSyncResult(
                    file_version_id=file_version_id,
                    points_count=0,
                    published=False,
                    skipped_existing=True,
                )
            job = self._store.get_job(project_id, file_version_id)
            if job is None:
                return KnowledgeSyncResult(
                    file_version_id=file_version_id,
                    points_count=0,
                    published=False,
                    error="knowledge_sync_job_not_found",
                )
            result = await self._publish_locked(
                job, update_memory=update_memory, repo_key=repo_key,
            )
            if result.error and update_memory:
                await self._refresh_memory_best_effort(
                    project_id, file_version_id, job.old_file_version_id,
                    points=job.points, repo_key=repo_key,
                )
            return result

    async def try_skip_existing_file_version(
        self,
        project_id: str,
        file_path: str,
        file_version_id: str,
        old_file_version_id: Optional[str] = None,
        update_latest: bool = True,
        update_memory: bool = True,
        repo_key: Optional[str] = None,
    ) -> Optional[KnowledgeSyncResult]:
        """Return a skipped-existing result if Qdrant already has this file version.

        This is a pre-embedding fast path for idempotent client submissions.
        It only handles complete, already published knowledge sets and never
        publishes or reuses pending jobs; callers can fall back to the normal
        submit_points path when ``None`` is returned.
        """
        self._check_initialized()
        file_lock_key = self._file_lock_key(project_id, file_path)
        version_lock_key = self._version_lock_key(project_id, file_version_id)
        async with self._file_locks[file_lock_key]:
            async with self._version_locks[version_lock_key]:
                existing = await self._qdrant.check_existence(project_id, [file_version_id])
                if not existing.get(file_version_id, False):
                    return None

                if update_latest:
                    self._store.set_latest(project_id, file_path, file_version_id)
                    self._store.cancel_stale_file_jobs(
                        project_id, file_path, file_version_id,
                    )
                self._remove_job_best_effort(project_id, file_version_id)
                if update_memory:
                    await self._refresh_memory_best_effort(
                        project_id, file_version_id, old_file_version_id,
                        repo_key=repo_key,
                    )
                return KnowledgeSyncResult(
                    file_version_id=file_version_id,
                    points_count=0,
                    published=False,
                    skipped_existing=True,
                )

    async def retry_ready_jobs(
        self,
        project_id: Optional[str] = None,
        batch_size: int = 10,
    ) -> tuple[int, int]:
        self._check_initialized()
        jobs = (
            self._store.iter_project_jobs(project_id)
            if project_id is not None
            else self._store.iter_all_jobs()
        )
        ready = [j for j in jobs if j.status == "ready"][:batch_size]
        succeeded = 0
        failed = 0
        for job in ready:
            lock_key = self._version_lock_key(job.project_id, job.file_version_id)
            async with self._version_locks[lock_key]:
                current = self._store.get_job(job.project_id, job.file_version_id)
                if current is None or current.status != "ready":
                    continue
                result = await self._publish_locked(current, update_memory=False)
            if result.published or result.skipped_existing:
                succeeded += 1
            elif result.error:
                failed += 1
        return succeeded, failed

    def mark_latest_file_version(
        self,
        project_id: str,
        file_path: str,
        file_version_id: str,
    ) -> None:
        self._check_initialized()
        self._store.set_latest(project_id, file_path, file_version_id)
        self._store.cancel_stale_file_jobs(project_id, file_path, file_version_id)

    def is_stale_file_version(
        self,
        project_id: str,
        file_path: str,
        file_version_id: str,
    ) -> bool:
        self._check_initialized()
        latest = self._store.get_latest(project_id, file_path)
        return latest is not None and latest != file_version_id

    def has_ready_job(self, project_id: str, file_version_id: str) -> bool:
        self._check_initialized()
        job = self._store.get_job(project_id, file_version_id)
        return job is not None and job.status == "ready"

    async def upsert_memory_from_qdrant(
        self,
        project_id: str,
        file_version_id: str,
        old_file_version_id: Optional[str] = None,
        repo_key: Optional[str] = None,
    ) -> None:
        if repo_key is None:
            # No target repository copy — nothing to refresh.
            return
        active_info = await self._memory.get_active_instance_info(repo_key)
        if active_info is None:
            return
        handle = await self._memory.get_cached_instance(
            repo_key, active_info.branch_name, active_info.commit_hash,
        )
        if handle is None:
            return
        if old_file_version_id:
            await self._memory.delete_points_by_file_version(handle, [old_file_version_id])
        records = await self._qdrant.get_file_points(project_id, file_version_id)
        if records:
            await self._memory.upsert_points(handle, self._records_to_point_structs(records))

    async def upsert_memory_points(
        self,
        project_id: str,
        file_version_id: str,
        points_data: List[PointData],
        old_file_version_id: Optional[str] = None,
        repo_key: Optional[str] = None,
    ) -> None:
        if repo_key is None:
            # No target repository copy — nothing to refresh.
            return
        active_info = await self._memory.get_active_instance_info(repo_key)
        if active_info is None:
            return
        handle = await self._memory.get_cached_instance(
            repo_key, active_info.branch_name, active_info.commit_hash,
        )
        if handle is None:
            return
        if old_file_version_id:
            await self._memory.delete_points_by_file_version(handle, [old_file_version_id])
        point_structs = self._point_data_to_point_structs(
            project_id, file_version_id, points_data,
        )
        if point_structs:
            await self._memory.upsert_points(handle, point_structs)

    def rebrand_project_id(self, old_project_id: str, new_project_id: str) -> int:
        return self._store.rebrand_project_id(old_project_id, new_project_id)

    async def _publish_locked(
        self,
        job: KnowledgeSyncJob,
        update_memory: bool,
        repo_key: Optional[str] = None,
    ) -> KnowledgeSyncResult:
        latest = self._store.get_latest(job.project_id, job.file_path)
        if latest is not None and latest != job.file_version_id:
            job.status = "cancelled"
            job.last_error = "cancelled_by_newer_file_version"
            self._store.put_job(job)
            return KnowledgeSyncResult(
                file_version_id=job.file_version_id,
                points_count=0,
                published=False,
                cancelled_stale=True,
            )
        try:
            existing = await self._qdrant.check_existence(
                job.project_id, [job.file_version_id],
            )
            skipped_existing = existing.get(job.file_version_id, False)
            if not skipped_existing:
                confirmed = await self._qdrant.upsert_file_points(
                    job.project_id, job.file_version_id, job.points,
                )
                if confirmed is not True:
                    raise RuntimeError("Knowledge write was not confirmed.")
        except Exception as e:
            job.retry_count += 1
            job.last_error = str(e)[:500]
            job.status = "ready"
            self._store.put_job(job)
            logger.warning(
                "knowledge_sync_publish_failed",
                project_id=job.project_id,
                file_path=job.file_path,
                file_version_id=job.file_version_id[:16],
                retry_count=job.retry_count,
                error=str(e)[:200],
            )
            return KnowledgeSyncResult(
                file_version_id=job.file_version_id,
                points_count=0,
                published=False,
                error=str(e),
                retry_job_saved=True,
            )

        self._remove_job_best_effort(job.project_id, job.file_version_id)
        if update_memory:
            await self._refresh_memory_best_effort(
                job.project_id, job.file_version_id, job.old_file_version_id,
                repo_key=repo_key,
            )
        if not skipped_existing:
            logger.info(
                "knowledge_sync_publish_success",
                project_id=job.project_id,
                file_path=job.file_path,
                file_version_id=job.file_version_id[:16],
                points=len(job.points),
                source=job.source,
            )
        return KnowledgeSyncResult(
            file_version_id=job.file_version_id,
            points_count=0 if skipped_existing else len(job.points),
            published=not skipped_existing,
            skipped_existing=skipped_existing,
        )

    def _remove_job_best_effort(self, project_id: str, file_version_id: str) -> None:
        """Cleanup failure does not invalidate a confirmed persistent result."""
        try:
            self._store.remove_job(project_id, file_version_id)
        except Exception:
            logger.exception(
                "knowledge_sync_job_cleanup_failed",
                project_id=project_id, file_version_id=file_version_id,
            )

    async def _refresh_memory_best_effort(
        self,
        project_id: str,
        file_version_id: str,
        old_file_version_id: Optional[str] = None,
        *,
        points: Optional[List[PointData]] = None,
        repo_key: Optional[str] = None,
    ) -> None:
        """Refresh existing memory only; preserve receipts and cancellation.

        ``repo_key`` selects which local copy's in-memory instance gets
        refreshed; when None the refresh is a no-op (the job receipt and
        persistent result are unaffected).
        """
        try:
            if points is None:
                await self.upsert_memory_from_qdrant(
                    project_id, file_version_id, old_file_version_id,
                    repo_key=repo_key,
                )
            else:
                await self.upsert_memory_points(
                    project_id, file_version_id, points, old_file_version_id,
                    repo_key=repo_key,
                )
        except Exception:
            logger.exception(
                "knowledge_sync_memory_refresh_failed",
                project_id=project_id, file_version_id=file_version_id,
            )

    def _point_data_to_point_structs(
        self,
        project_id: str,
        file_version_id: str,
        points_data: List[PointData],
    ) -> List[models.PointStruct]:
        dense_name = self._config_manager.config.memory_qdrant.instance.dense_vector.name
        sparse_name = self._config_manager.config.memory_qdrant.instance.sparse_vector.name
        knowledge_set_id = self._knowledge_set_id(project_id, file_version_id, points_data)
        chunk_count = len(points_data)
        points: List[models.PointStruct] = []
        for idx, pd in enumerate(points_data):
            points.append(models.PointStruct(
                id=self._point_id(project_id, file_version_id, knowledge_set_id, idx),
                vector={
                    dense_name: pd.dense_vector,
                    sparse_name: models.SparseVector(
                        indices=pd.sparse_vector.indices,
                        values=pd.sparse_vector.values,
                    ),
                },
                payload={
                    "file_version_id": file_version_id,
                    "file_path": pd.file_path,
                    "entity_type": pd.entity_type,
                    "entity_name": pd.entity_name,
                    "business_description": pd.business_description,
                    "project_id": project_id,
                    "blob_hash": pd.blob_hash,
                    "knowledge_set_id": knowledge_set_id,
                    "chunk_index": idx,
                    "chunk_count": chunk_count,
                },
            ))
        return points

    @staticmethod
    def _records_to_point_structs(records: list) -> List[models.PointStruct]:
        points = []
        for rec in records:
            payload = rec.payload or {}
            vectors = {}
            if rec.vector:
                if isinstance(rec.vector, dict):
                    vectors = rec.vector
                elif isinstance(rec.vector, list):
                    vectors["dense"] = rec.vector
            points.append(models.PointStruct(
                id=rec.id,
                vector=vectors if vectors else rec.vector,
                payload=payload,
            ))
        return points

    @staticmethod
    def _knowledge_set_id(
        project_id: str,
        file_version_id: str,
        points_data: List[PointData],
    ) -> str:
        h = hashlib.sha256()
        h.update(project_id.encode("utf-8"))
        h.update(file_version_id.encode("utf-8"))
        for pd in points_data:
            h.update(pd.file_path.encode("utf-8"))
            h.update(pd.blob_hash.encode("utf-8"))
            h.update(pd.business_description.encode("utf-8"))
        return h.hexdigest()

    @staticmethod
    def _point_id(
        project_id: str,
        file_version_id: str,
        knowledge_set_id: str,
        chunk_index: int,
    ) -> str:
        return str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"codebase-rag:memory-point:{project_id}:{file_version_id}:"
            f"{knowledge_set_id}:{chunk_index}",
        ))

    @staticmethod
    def _version_lock_key(project_id: str, file_version_id: str) -> str:
        return f"{project_id}:{file_version_id}"

    @staticmethod
    def _file_lock_key(project_id: str, file_path: str) -> str:
        normalized = file_path.replace("\\", "/")
        return f"{project_id}:{normalized}"

    def _check_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("KnowledgeSyncService is not initialized.")
