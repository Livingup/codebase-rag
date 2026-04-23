"""GC Service — garbage collection for orphan knowledge points in Qdrant.

Periodically scans Qdrant for file_version_ids not referenced by any active
branch, marks them as orphans, and deletes them after a configurable grace
period.  Uses a mark-then-delay-delete strategy to avoid accidental data loss.

Internal dependencies:
  - 07-Git Operations Service (branch list + file tree)
  - 01-Qdrant Storage Service (scan stored ids + batch delete)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional, Set

from codebase_rag.common.logging import get_logger
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import GCConfig
from codebase_rag.services.gc.models import (
    CleanupResult,
    GCDeleteError,
    GCLockError,
    GCRecord,
    GCResult,
    GCScanError,
    OrphanDetail,
    OrphanStatus,
)
from codebase_rag.services.gc.orphan_store import OrphanStore
from codebase_rag.services.git_operations.service import GitOperationsService
from codebase_rag.services.qdrant_storage.service import QdrantStorageService

logger = get_logger(__name__)


class GCService:
    """Garbage Collection Service for orphan knowledge points.

    Args:
        config: GC configuration section from AppConfig.
        git_ops: Git operations service instance.
        qdrant_storage: Qdrant storage service instance.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        git_ops: GitOperationsService,
        qdrant_storage: QdrantStorageService,
    ) -> None:
        self._config_manager = config_manager
        self._git_ops = git_ops
        self._qdrant = qdrant_storage

        # Orphan record persistence
        self._orphan_store = OrphanStore(self._config.orphan_store.path)

        # Per-project locks to prevent concurrent GC on the same project
        self._project_locks: Dict[str, asyncio.Lock] = {}

        # Concurrency limiter for multi-project parallel GC
        self._semaphore = asyncio.Semaphore(self._config.scan.max_concurrency)

        # Scheduled task
        self._schedule_task: Optional[asyncio.Task] = None

        # GC history (in-memory ring buffer)
        self._history: Deque[GCRecord] = deque(maxlen=50)

        self._initialized = False

    @property
    def _config(self) -> GCConfig:
        return self._config_manager.config.gc

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Load orphan store and start scheduled GC if enabled."""
        logger.info("gc_service_initializing")

        self._orphan_store.load()

        if self._config.schedule.enabled:
            self._schedule_task = asyncio.create_task(self._schedule_loop())

        self._initialized = True
        logger.info(
            "gc_service_initialized",
            schedule_enabled=self._config.schedule.enabled,
            cron=self._config.schedule.cron,
            orphan_count=len(self._orphan_store.records),
        )

    async def shutdown(self) -> None:
        """Cancel scheduler, persist orphan store, release resources."""
        logger.info("gc_service_shutting_down")

        if self._schedule_task is not None:
            self._schedule_task.cancel()
            try:
                await self._schedule_task
            except asyncio.CancelledError:
                pass
            self._schedule_task = None

        # Persist orphan records
        try:
            self._orphan_store.save()
        except Exception:
            logger.exception("gc_shutdown_orphan_save_failed")

        self._project_locks.clear()
        self._initialized = False
        logger.info("gc_service_shutdown")

    # =========================================================================
    # 2.1 Execute full GC
    # =========================================================================

    async def run_gc(
        self,
        project_id: str,
        repo_path: str,
        *,
        force: bool = False,
        trigger: str = "manual",
    ) -> GCResult:
        """Execute full GC for a single project.

        Args:
            project_id: Target project UUID.
            repo_path: Absolute path to the git repository.
            force: If True, skip grace period and delete all orphans immediately.
            trigger: "scheduled" or "manual".

        Returns:
            GCResult with scan/delete statistics.

        Raises:
            GCLockError: If GC is already running for this project.
            GCScanError: If scanning active refs or Qdrant fails.
            GCDeleteError: If orphan deletion fails.
        """
        lock = self._get_project_lock(project_id)
        if lock.locked():
            raise GCLockError(f"GC already running for project {project_id}")

        async with lock:
            return await self._run_gc_impl(
                project_id, repo_path, force=force, trigger=trigger,
            )

    async def _run_gc_impl(
        self,
        project_id: str,
        repo_path: str,
        *,
        force: bool,
        trigger: str,
    ) -> GCResult:
        """Core GC implementation (must hold project lock)."""
        start = time.monotonic()
        gc_id = str(uuid.uuid4())

        logger.info(
            "gc_started",
            gc_id=gc_id,
            project_id=project_id,
            trigger=trigger,
            force=force,
        )

        async with self._semaphore:
            # Step 1: Get active branches
            try:
                branches = await self._git_ops.get_all_branches(repo_path)
            except Exception as e:
                raise GCScanError(
                    f"Failed to get branches for {project_id}: {e}"
                ) from e

            logger.info(
                "gc_branches_found",
                project_id=project_id,
                branch_count=len(branches),
            )

            # Step 2: Collect all active file_version_ids (union across branches)
            active_ids: Set[str] = set()
            try:
                for branch in branches:
                    file_tree = await self._git_ops.get_file_tree(
                        repo_path, branch.name,
                    )
                    for entry in file_tree:
                        active_ids.add(entry.file_version_id)
            except Exception as e:
                raise GCScanError(
                    f"Failed to scan active files for {project_id}: {e}"
                ) from e

            # Step 3: Get stored file_version_ids from Qdrant
            try:
                stored_ids = await self._qdrant.get_all_file_version_ids(project_id)
            except Exception as e:
                raise GCScanError(
                    f"Failed to scan Qdrant for {project_id}: {e}"
                ) from e

            logger.info(
                "gc_scan_complete",
                project_id=project_id,
                stored_count=len(stored_ids),
                active_count=len(active_ids),
            )

            # Step 4: Compute orphan set
            orphan_ids = stored_ids - active_ids

            # Step 5: Mark new orphans, unmark recovered ones
            new_orphans = 0
            project_orphans = self._orphan_store.get_by_project(project_id)

            for fvid in orphan_ids:
                if self._orphan_store.mark(fvid, project_id):
                    new_orphans += 1

            # Unmark file_version_ids that are no longer orphans (re-referenced)
            recovered = 0
            for fvid in list(project_orphans.keys()):
                if fvid not in orphan_ids:
                    self._orphan_store.unmark(fvid)
                    recovered += 1

            if recovered > 0:
                logger.info(
                    "gc_orphans_recovered",
                    project_id=project_id,
                    count=recovered,
                )

            logger.info(
                "gc_orphans_marked",
                project_id=project_id,
                total_orphans=len(orphan_ids),
                new_orphans=new_orphans,
            )

            # Step 6: Filter expired orphans (past grace period)
            grace_days = self._config.policy.orphan_grace_period_days
            now = datetime.utcnow()
            cutoff = now - timedelta(days=grace_days)

            expired_ids: List[str] = []
            skipped = 0

            for fvid in orphan_ids:
                rec = self._orphan_store.get(fvid)
                if rec is None:
                    continue
                if force or rec.first_marked_at < cutoff:
                    expired_ids.append(fvid)
                else:
                    skipped += 1

            # Step 7: Delete expired orphans (batched)
            max_batch = self._config.scan.max_delete_batch_size
            deleted_count = 0

            if expired_ids:
                # Cap at max_delete_batch_size
                to_delete = expired_ids[:max_batch]

                try:
                    batch_result = await self._qdrant.batch_delete_files(
                        project_id, to_delete,
                    )
                    deleted_count = len(batch_result.succeeded)

                    # Remove successfully deleted from orphan store
                    for fvid in batch_result.succeeded:
                        self._orphan_store.remove(fvid)

                    if batch_result.failed:
                        logger.warning(
                            "gc_delete_partial_failure",
                            project_id=project_id,
                            failed_count=len(batch_result.failed),
                        )
                except Exception as e:
                    raise GCDeleteError(
                        f"Failed to delete orphans for {project_id}: {e}"
                    ) from e

                logger.info(
                    "gc_orphans_deleted",
                    project_id=project_id,
                    deleted=deleted_count,
                    remaining_expired=len(expired_ids) - deleted_count,
                )

            # Step 8: Persist orphan records
            self._orphan_store.save()

        duration = time.monotonic() - start

        result = GCResult(
            project_id=project_id,
            scanned_stored_count=len(stored_ids),
            active_count=len(active_ids),
            new_orphans_count=new_orphans,
            expired_orphans_count=len(expired_ids),
            deleted_count=deleted_count,
            skipped_count=skipped,
            duration_seconds=round(duration, 2),
        )

        # Record history
        record = GCRecord(
            gc_id=gc_id,
            project_id=project_id,
            trigger=trigger,
            started_at=now,
            result=result,
        )
        self._history.append(record)

        logger.info(
            "gc_completed",
            gc_id=gc_id,
            project_id=project_id,
            stored=result.scanned_stored_count,
            active=result.active_count,
            new_orphans=result.new_orphans_count,
            expired=result.expired_orphans_count,
            deleted=result.deleted_count,
            skipped=result.skipped_count,
            duration_s=result.duration_seconds,
        )

        return result

    # =========================================================================
    # 2.2 Manual trigger (convenience wrapper)
    # =========================================================================

    async def trigger_gc(
        self,
        project_id: str,
        repo_path: str,
        *,
        force: bool = False,
    ) -> GCResult:
        """Manually trigger GC for a project (exposed via management API)."""
        return await self.run_gc(
            project_id, repo_path, force=force, trigger="manual",
        )

    # =========================================================================
    # 2.3 Query orphan status
    # =========================================================================

    def get_orphan_status(self, project_id: str) -> OrphanStatus:
        """Get aggregated orphan status for a project."""
        project_orphans = self._orphan_store.get_by_project(project_id)

        now = datetime.utcnow()
        grace_days = self._config.policy.orphan_grace_period_days
        cutoff = now - timedelta(days=grace_days)

        details: List[OrphanDetail] = []
        expired = 0
        pending = 0
        oldest_age = 0.0

        for fvid, rec in project_orphans.items():
            age_days = (now - rec.first_marked_at).total_seconds() / 86400.0
            oldest_age = max(oldest_age, age_days)

            if rec.first_marked_at < cutoff:
                expired += 1
            else:
                pending += 1

            details.append(OrphanDetail(
                file_version_id=fvid,
                first_marked_at=rec.first_marked_at,
                age_days=round(age_days, 2),
            ))

        return OrphanStatus(
            total_orphans=len(project_orphans),
            expired_orphans=expired,
            pending_orphans=pending,
            oldest_orphan_age_days=round(oldest_age, 2),
            orphan_details=details,
        )

    # =========================================================================
    # 2.4 Query GC history
    # =========================================================================

    def get_gc_history(
        self,
        project_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[GCRecord]:
        """Get recent GC execution records (in-memory only)."""
        if project_id is not None:
            filtered = [r for r in self._history if r.project_id == project_id]
        else:
            filtered = list(self._history)
        # Most recent first
        return list(reversed(filtered))[:limit]

    # =========================================================================
    # 2.5 Cleanup unregistered project
    # =========================================================================

    async def cleanup_project(self, project_id: str) -> CleanupResult:
        """Delete ALL data for a project from Qdrant. No grace period.

        Called when a project is unregistered from the system.
        """
        logger.info("gc_project_cleanup_started", project_id=project_id)

        try:
            stored_ids = await self._qdrant.get_all_file_version_ids(project_id)
        except Exception as e:
            raise GCScanError(
                f"Failed to scan Qdrant for cleanup of {project_id}: {e}"
            ) from e

        deleted_count = 0
        if stored_ids:
            ids_list = list(stored_ids)
            batch_result = await self._qdrant.batch_delete_files(
                project_id, ids_list,
            )
            deleted_count = len(batch_result.succeeded)

        # Remove orphan records for this project
        orphans_removed = self._orphan_store.remove_project(project_id)
        self._orphan_store.save()

        result = CleanupResult(
            deleted_file_version_ids=deleted_count,
            deleted_points=deleted_count,  # 1:N but we track file_version_ids
        )

        logger.info(
            "gc_project_cleanup_completed",
            project_id=project_id,
            deleted_fvids=deleted_count,
            orphans_removed=orphans_removed,
        )

        return result

    # =========================================================================
    # Internal: scheduled GC loop
    # =========================================================================

    async def _schedule_loop(self) -> None:
        """Simple interval-based scheduler.

        NOTE: Full cron parsing is deferred; we use a fixed interval derived
        from the cron expression (default: 24h).  A proper cron scheduler
        (e.g. APScheduler) can be plugged in later.
        """
        interval_s = self._parse_cron_interval(self._config.schedule.cron)
        logger.info(
            "gc_scheduler_started",
            interval_hours=interval_s / 3600,
        )

        try:
            while True:
                await asyncio.sleep(interval_s)
                logger.info("gc_scheduled_run_starting")
                # Scheduled GC needs project list from external source;
                # for now, GC is triggered per-project by the workflow
                # orchestrator or management API.  The scheduler serves
                # as a reminder/hook point.
        except asyncio.CancelledError:
            return

    @staticmethod
    def _parse_cron_interval(cron: str) -> float:
        """Extract a rough interval in seconds from a simple cron expression.

        Handles common patterns: "0 3 * * *" → 86400s (daily).
        Falls back to 24h for complex expressions.
        """
        parts = cron.strip().split()
        if len(parts) == 5:
            minute, hour, dom, month, dow = parts
            if dom == "*" and month == "*" and dow == "*":
                # Daily at hour:minute
                return 86400.0
        return 86400.0  # default: daily

    # =========================================================================
    # Internal: helpers
    # =========================================================================

    def _get_project_lock(self, project_id: str) -> asyncio.Lock:
        if project_id not in self._project_locks:
            self._project_locks[project_id] = asyncio.Lock()
        return self._project_locks[project_id]
