"""Management API Service — top-level HTTP API entry point.

Coordinates all downstream services: project lifecycle, semantic search,
manual triggers, status queries, and health checks.

Internal dependencies:
  - 05-Event Detection Service (project registration/unregistration)
  - 08-Workflow Orchestrator Service (init, sync, status)
  - 02-Memory Qdrant Service (semantic search)
  - 01-Qdrant Storage Service (storage stats)
  - 07-Git Operations Service (repo validation, HEAD info)
  - 04-Embedding Service (query vectorization for search)
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from codebase_rag.common.logging import get_logger
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import ManagementApiConfig
from codebase_rag.services.embedding.service import EmbeddingService
from codebase_rag.services.event_detection.service import EventDetectionService
from codebase_rag.services.git_operations.service import GitOperationsService
from codebase_rag.services.management_api.models import (
    BatchRegisterProjectResponse,
    BatchRegisterResultItem,
    DeleteProjectResponse,
    HealthResponse,
    ManagementApiError,
    ProjectDetailResponse,
    ProjectExistsError,
    ProjectNotFoundError,
    ProjectRecord,
    ProjectResponse,
    ProjectStatusResponse,
    SearchResponse,
    SearchResultItem,
    ServiceUnavailableError,
    TaskResponse,
)
from codebase_rag.services.knowledge_sync.service import KnowledgeSyncService
from codebase_rag.services.memory_qdrant.models import SearchFilters as MemorySearchFilters
from codebase_rag.services.memory_qdrant.service import MemoryQdrantService
from codebase_rag.services.qdrant_storage.service import QdrantStorageService
from codebase_rag.services.workflow.service import WorkflowOrchestratorService

logger = get_logger(__name__)

# Directory inside .git/ for project metadata
_PROJECT_META_DIR = "codebase-rag"
_PROJECT_ID_FILE = "project_id"

# Derivation namespace for deterministic project_id generation.
# Bumping the version segment (e.g. v2) would force a re-derivation across
# all already-registered repositories on next register_project().
_PROJECT_ID_DERIVATION_PREFIX = "codebase-rag:project_id:v1"


def _derive_project_id_from_root_commit(root_commit: str) -> str:
    """Derive a deterministic project_id from a Git root commit hash.

    Uses uuid5 over a namespaced derivation input so the result is stable
    across users / clones / mirrors of the same repository — the only
    Git-native value satisfying "cross-user consistent + push-invariant +
    no external coordination" simultaneously.

    Args:
        root_commit: Hex-encoded SHA-1 of the repository's root commit.

    Returns:
        Hex string UUID (uuid5).
    """
    derivation_input = f"{_PROJECT_ID_DERIVATION_PREFIX}:{root_commit}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, derivation_input))


class ManagementApiService:
    """Top-level API service coordinating all downstream services.

    Args:
        config: Management API configuration section from AppConfig.
        git_ops: Git operations service instance.
        event_detection: Event detection service instance.
        workflow: Workflow orchestrator service instance.
        memory_qdrant: Memory Qdrant service instance.
        qdrant_storage: Qdrant storage service instance.
        embedding: Embedding service instance.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        git_ops: GitOperationsService,
        event_detection: EventDetectionService,
        workflow: WorkflowOrchestratorService,
        memory_qdrant: MemoryQdrantService,
        qdrant_storage: QdrantStorageService,
        embedding: EmbeddingService,
        knowledge_sync: KnowledgeSyncService,
    ) -> None:
        self._config_manager = config_manager
        self._git_ops = git_ops
        self._event_detection = event_detection
        self._workflow = workflow
        self._memory = memory_qdrant
        self._qdrant = qdrant_storage
        self._embedding = embedding
        self._knowledge_sync = knowledge_sync

        # In-memory project registry: project_id → ProjectRecord
        self._projects: Dict[str, ProjectRecord] = {}

        self._initialized = False
        self._start_time: float = 0.0

    @property
    def _config(self) -> ManagementApiConfig:
        return self._config_manager.config.management_api

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Initialize the Management API service and restore persisted projects."""
        logger.info("management_api_service_initializing")
        self._start_time = time.monotonic()
        self._initialized = True

        # Restore previously registered projects from persistent registry
        await self._restore_persisted_projects()

        logger.info("management_api_service_initialized")

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("management_api_service_shutting_down")
        self._projects.clear()
        self._initialized = False
        logger.info("management_api_service_shutdown")

    # =========================================================================
    # 2.1 Project management
    # =========================================================================

    async def register_project(
        self, git_repo_path: str, display_name: str = "",
    ) -> ProjectResponse:
        """Register a project: validate repo, get/create project_id, trigger init.

        Steps:
          1. Validate .git/ exists
          2. Derive project_id deterministically from the Git root commit;
             migrate any data attributed to a stale id (cached from a prior
             derivation rule) to the new id before continuing
          3. Register to event detection service
          4. Register to workflow orchestrator
          5. Trigger full initialization (async)
          6. Return project info
        """
        # Step 1: Validate repo
        validation = await self._git_ops.validate_repository(git_repo_path)
        if not validation.is_valid:
            raise ManagementApiError(
                f"Invalid Git repository: {validation.error}"
            )

        # Step 2: Resolve deterministic project_id (derived from root commit)
        # and migrate any pre-existing data attributed to a stale id.
        project_id = await self._resolve_project_id_with_migration(git_repo_path)

        # Check if already registered with a different path
        existing = self._projects.get(project_id)
        if existing and existing.git_repo_path != git_repo_path:
            # Update path (repo moved)
            existing.git_repo_path = git_repo_path
            logger.info(
                "project_path_updated",
                project_id=project_id,
                new_path=git_repo_path,
            )

        # Step 3: Register to event detection
        await self._event_detection.register_project(project_id, git_repo_path)

        # Step 4: Register to workflow orchestrator
        self._workflow.register_project(project_id, git_repo_path)

        # Get HEAD info
        head_info = await self._git_ops.get_head_info(git_repo_path)

        # Save to registry
        if not display_name:
            display_name = os.path.basename(git_repo_path)
        record = ProjectRecord(
            project_id=project_id,
            git_repo_path=git_repo_path,
            display_name=display_name,
        )
        self._projects[project_id] = record
        self._save_registry()

        # Step 5: Trigger full initialization (via task queue, serial per project)
        branch = head_info.branch_name or "HEAD"
        self._workflow.enqueue_initialization(project_id, branch, head_info.commit_hash)

        logger.info(
            "project_registered",
            project_id=project_id,
            repo_path=git_repo_path,
            branch=branch,
            commit=head_info.commit_hash[:8],
        )

        return ProjectResponse(
            project_id=project_id,
            git_repo_path=git_repo_path,
            display_name=display_name,
            status="registered",
            branch=branch,
            commit_hash=head_info.commit_hash[:8],
        )

    async def batch_register_projects(
        self, projects: List[dict],
    ) -> BatchRegisterProjectResponse:
        """Register multiple projects. Each item is processed independently.

        Partial failures are safe — succeeded projects remain registered.

        Args:
            projects: List of dicts with ``git_repo_path`` and optional ``display_name``.
        """
        results: List[BatchRegisterResultItem] = []
        succeeded = 0
        failed = 0

        for item in projects:
            git_repo_path = item.get("git_repo_path", "")
            display_name = item.get("display_name", "")
            try:
                resp = await self.register_project(
                    git_repo_path=git_repo_path,
                    display_name=display_name,
                )
                results.append(BatchRegisterResultItem(
                    git_repo_path=git_repo_path,
                    success=True,
                    project=resp,
                ))
                succeeded += 1
            except Exception as e:
                results.append(BatchRegisterResultItem(
                    git_repo_path=git_repo_path,
                    success=False,
                    error=str(e),
                ))
                failed += 1
                logger.warning(
                    "batch_register_project_failed",
                    git_repo_path=git_repo_path,
                    error=str(e),
                )

        logger.info(
            "batch_register_complete",
            total=len(projects),
            succeeded=succeeded,
            failed=failed,
        )

        return BatchRegisterProjectResponse(
            total=len(projects),
            succeeded=succeeded,
            failed=failed,
            results=results,
        )

    async def list_projects(self) -> List[ProjectResponse]:
        """List all registered projects."""
        results = []
        for record in self._projects.values():
            status, branch, commit, points = await self._get_project_summary(
                record.project_id, record.git_repo_path,
            )
            results.append(ProjectResponse(
                project_id=record.project_id,
                git_repo_path=record.git_repo_path,
                display_name=record.display_name,
                status=status,
                branch=branch,
                commit_hash=commit,
                knowledge_points=points,
            ))
        return results

    async def get_project(self, project_id: str) -> ProjectDetailResponse:
        """Get detailed info for a single project."""
        record = self._projects.get(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        status, branch, commit, points = await self._get_project_summary(
            project_id, record.git_repo_path,
        )

        # Workflow status
        wf_status = self._workflow.get_workflow_status(project_id)
        wf_dict = None
        if wf_status.current_task:
            wf_dict = {
                "task_type": wf_status.current_task.task_type,
                "project_id": wf_status.current_task.project_id,
            }

        return ProjectDetailResponse(
            project_id=project_id,
            git_repo_path=record.git_repo_path,
            display_name=record.display_name,
            status=status,
            branch=branch,
            commit_hash=commit,
            knowledge_points=points,
            workflow_status=wf_dict,
        )

    async def delete_project(self, project_id: str) -> DeleteProjectResponse:
        """Unregister a project. Preserves Qdrant persistent data for reuse.

        Steps:
          1. Stop event monitoring
          2. Unregister from workflow
          3. Destroy in-memory instances
          4. Remove from registry
        """
        record = self._projects.get(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        # Step 1
        await self._event_detection.unregister_project(project_id)

        # Step 2
        self._workflow.unregister_project(project_id)

        # Step 3
        await self._memory.destroy_all_instances(project_id)

        # Step 4
        self._projects.pop(project_id, None)
        self._save_registry()

        logger.info(
            "project_deleted",
            project_id=project_id,
        )

        return DeleteProjectResponse(
            project_id=project_id,
            deleted_points=0,
        )

    # =========================================================================
    # 2.2 Semantic search
    # =========================================================================

    async def search(
        self,
        project_id: str,
        query: str,
        top_k: int = 10,
        score_threshold: float = 0.0,
        entity_type: Optional[str] = None,
        file_path_prefix: Optional[str] = None,
    ) -> SearchResponse:
        """Execute semantic search on a project's active in-memory instance."""
        record = self._projects.get(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        # Step 1: Embed query
        embeddings = await self._embedding.embed([query])
        if not embeddings:
            raise ServiceUnavailableError("Embedding service returned no results")
        emb = embeddings[0]

        has_sparse = emb.sparse_vector is not None and len(emb.sparse_vector.indices) > 0
        logger.debug(
            "search_embedding_done",
            project_id=project_id,
            dense_dim=len(emb.dense_vector),
            has_sparse=has_sparse,
            sparse_nnz=len(emb.sparse_vector.indices) if has_sparse else 0,
        )

        # Step 2: Build filters
        filters = None
        if entity_type or file_path_prefix:
            entity_types = [entity_type] if entity_type else None
            file_paths = [file_path_prefix] if file_path_prefix else None
            filters = MemorySearchFilters(
                entity_types=entity_types,
                file_paths=file_paths,
            )

        # Step 3: Hybrid search
        results = await self._memory.hybrid_search(
            project_id=project_id,
            query_dense=emb.dense_vector,
            query_sparse=emb.sparse_vector,
            top_k=top_k,
            filters=filters,
        )

        # Step 4: Filter by score threshold and format
        items = []
        below_threshold = 0
        for r in results:
            if r.score < score_threshold:
                below_threshold += 1
                continue
            items.append(SearchResultItem(
                score=round(r.score, 4),
                file_path=r.file_path,
                entity_type=r.entity_type,
                entity_name=r.entity_name,
                business_description=r.business_description,
                file_version_id=r.file_version_id,
            ))

        # Get active branch info
        active = await self._memory.get_active_instance_info(project_id)
        branch = active.branch_name if active else None
        commit = active.commit_hash[:8] if active else None

        logger.info(
            "search_executed",
            project_id=project_id,
            query_preview=query[:50],
            top_k=top_k,
            score_threshold=score_threshold,
            memory_results=len(results),
            below_threshold=below_threshold,
            final_results=len(items),
        )

        return SearchResponse(
            results=items,
            total=len(items),
            branch=branch,
            commit_hash=commit,
        )

    # =========================================================================
    # 2.3 Manual triggers
    # =========================================================================

    async def trigger_rebuild(
        self, project_id: str, force: bool = False,
    ) -> TaskResponse:
        """Trigger full rebuild for a project (async)."""
        record = self._projects.get(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        head = await self._git_ops.get_head_info(record.git_repo_path)
        branch = head.branch_name or "HEAD"
        task_id = str(uuid.uuid4())

        if force:
            await self._memory.destroy_all_instances(project_id)

        self._workflow.enqueue_initialization(project_id, branch, head.commit_hash)

        logger.info(
            "rebuild_triggered",
            project_id=project_id,
            task_id=task_id,
            force=force,
        )

        return TaskResponse(
            task_id=task_id,
            status="started",
            message=f"Full {'force ' if force else ''}rebuild started",
        )

    async def trigger_sync(self, project_id: str) -> TaskResponse:
        """Trigger manual incremental sync for a project."""
        record = self._projects.get(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        head = await self._git_ops.get_head_info(record.git_repo_path)
        task_id = str(uuid.uuid4())

        branch = head.branch_name or "HEAD"
        active = await self._memory.get_active_instance_info(project_id)
        if active and active.commit_hash != head.commit_hash:
            self._workflow.enqueue_content_commit(
                project_id, branch, head.commit_hash, active.commit_hash,
            )
            message = "Incremental sync enqueued"
        else:
            message = "No changes detected, nothing to sync"

        logger.info("sync_triggered", project_id=project_id, task_id=task_id)

        return TaskResponse(
            task_id=task_id,
            status="started",
            message=message,
        )

    # =========================================================================
    # 2.4 Status queries
    # =========================================================================

    async def get_project_status(self, project_id: str) -> ProjectStatusResponse:
        """Get detailed index status for a project."""
        record = self._projects.get(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        active = await self._memory.get_active_instance_info(project_id)
        wf_status = self._workflow.get_workflow_status(project_id)

        # Determine index status
        if wf_status.current_task:
            index_status = "building"
        elif active:
            index_status = "ready"
        else:
            index_status = "idle"  # instance evicted; will lazy-rebuild on next search

        # Build workflow progress dict
        wf_progress = None
        if wf_status.current_task and wf_status.current_task.progress:
            p = wf_status.current_task.progress
            wf_progress = {
                "task_type": wf_status.current_task.task_type,
                "total_files": p.total_files,
                "processed_files": p.processed_files,
                "current_file": p.current_file,
            }

        return ProjectStatusResponse(
            project_id=project_id,
            index_status=index_status,
            branch=active.branch_name if active else None,
            commit_hash=active.commit_hash[:8] if active else None,
            total_knowledge_points=active.point_count if active else 0,
            workflow_progress=wf_progress,
        )

    # =========================================================================
    # 2.5 System endpoints
    # =========================================================================

    def get_health(self) -> HealthResponse:
        """Aggregate health status from all services."""
        services = {
            "git_operations": "healthy" if self._git_ops.is_healthy() else "unhealthy",
            "qdrant_storage": "healthy" if self._qdrant._initialized else "unhealthy",
            "memory_qdrant": "healthy" if self._memory._initialized else "unhealthy",
            "embedding": "healthy" if self._embedding._initialized else "unhealthy",
            "event_detection": "healthy" if self._event_detection._initialized else "unhealthy",
            "workflow": "healthy" if self._workflow._initialized else "unhealthy",
        }

        unhealthy = [k for k, v in services.items() if v == "unhealthy"]
        # Critical services — without these, core functionality is broken
        critical = {"qdrant_storage", "memory_qdrant", "embedding", "git_operations"}

        if not unhealthy:
            status = "healthy"
        elif critical & set(unhealthy):
            status = "unhealthy"
        else:
            status = "degraded"

        uptime = time.monotonic() - self._start_time if self._start_time else 0.0

        return HealthResponse(
            status=status,
            services=services,
            uptime_seconds=round(uptime, 1),
        )

    async def reload_config(self) -> None:
        """Trigger configuration hot reload via ConfigManager.

        Re-reads config.yaml from disk, validates it, and replaces the
        in-memory AppConfig singleton.  All services read config via
        @property from ConfigManager, so they automatically pick up
        the new values on next access.

        Raises:
            Exception: If config file is invalid or reload fails.
        """
        await self._config_manager.reload()
        logger.info("config_reload_triggered_via_api")

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _resolve_project_id_with_migration(
        self, git_repo_path: str,
    ) -> str:
        """Derive project_id from the repo's root commit; migrate stale data.

        The project_id is computed deterministically from the Git root
        commit so two clones / mirrors of the same repository always yield
        the same id without coordination.  The ``.git/codebase-rag/
        project_id`` file is kept as a read-side cache (consumed by
        ``McpService._read_project_id``), but its value is no longer
        authoritative — every register_project() rebuilds the id from
        scratch and migrates Qdrant / workflow / knowledge sync /
        registry state if the cache disagrees.

        Raises:
            ManagementApiError: If the repository has no commits yet, or
                writing the cache file fails.
        """
        # Step 1: Get root commit (deterministic project identity anchor)
        root_commit = await self._git_ops.get_root_commit(git_repo_path)
        if not root_commit:
            raise ManagementApiError(
                f"Repository '{git_repo_path}' has no commits yet. At "
                f"least one commit is required to derive a stable project_id. "
                f"Please make an initial commit and retry."
            )

        # Step 2: Derive deterministic project_id
        new_project_id = _derive_project_id_from_root_commit(root_commit)

        # Step 3: Read cached project_id (if any)
        git_dir = os.path.join(git_repo_path, ".git")
        meta_dir = os.path.join(git_dir, _PROJECT_META_DIR)
        id_file = os.path.join(meta_dir, _PROJECT_ID_FILE)
        cached_id: Optional[str] = None
        if os.path.exists(id_file):
            try:
                with open(id_file, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    cached_id = content
            except OSError as e:
                logger.warning(
                    "project_id_cache_read_failed",
                    path=id_file,
                    error=str(e),
                )

        # Step 4: Migrate if cached id differs from derived id
        if cached_id and cached_id != new_project_id:
            await self._migrate_project_id(cached_id, new_project_id)

        # Step 5: Persist derived id to cache (covers first-register and
        # post-migration cases).  Skipped only when cache already matches.
        if cached_id != new_project_id:
            try:
                os.makedirs(meta_dir, exist_ok=True)
                with open(id_file, "w", encoding="utf-8") as f:
                    f.write(new_project_id)
            except OSError as e:
                raise ManagementApiError(
                    f"Failed to write project_id cache at '{id_file}': {e}"
                ) from e

            logger.info(
                "project_id_resolved",
                project_id=new_project_id,
                root_commit=root_commit[:8],
                migrated_from=cached_id,
                repo_path=git_repo_path,
            )

        return new_project_id

    async def _migrate_project_id(self, old_id: str, new_id: str) -> None:
        """Coordinate cross-service re-attribution of all data tied to old_id.

        Order of operations is chosen so any mid-flight crash can be safely
        retried on the next ``register_project`` call — each step is
        idempotent and a no-op when the source id has nothing to migrate.

        Steps:
          1. Qdrant payload rewrite
          2. Workflow retry/dead-letter files + in-memory dicts
          3. Knowledge sync outbox files
          4. Destroy old in-memory Qdrant instances (rebuilt on next search)
          5. Local project registry (in-memory + persistent file)
        """
        if old_id == new_id:
            return

        logger.info(
            "project_id_migration_started",
            old_project_id=old_id,
            new_project_id=new_id,
        )

        # 1) Persistent points
        await self._qdrant.rebrand_project_id(old_id, new_id)

        # 2) Workflow retry/dead-letter persistence + in-memory state
        self._workflow.rebrand_project_id(old_id, new_id)

        # 3) Knowledge sync outbox and latest-version index
        self._knowledge_sync.rebrand_project_id(old_id, new_id)

        # 4) Drop stale in-memory Qdrant instances; will be rebuilt on demand
        try:
            await self._memory.destroy_all_instances(old_id)
        except Exception:
            logger.exception(
                "memory_instance_destroy_failed_during_rebrand",
                old_project_id=old_id,
                new_project_id=new_id,
            )

        # 5) Local registry (in-memory dict + persisted JSON)
        if old_id in self._projects:
            record = self._projects.pop(old_id)
            record.project_id = new_id
            self._projects[new_id] = record
            self._save_registry()

        logger.info(
            "project_id_migration_done",
            old_project_id=old_id,
            new_project_id=new_id,
        )

    async def _get_project_summary(
        self, project_id: str, repo_path: str,
    ) -> tuple:
        """Get (status, branch, commit_hash_short, knowledge_points) for a project."""
        active = await self._memory.get_active_instance_info(project_id)
        wf_status = self._workflow.get_workflow_status(project_id)

        if wf_status.current_task:
            status = "initializing"
        elif active:
            status = "ready"
        else:
            status = "registered"

        branch = active.branch_name if active else None
        commit = active.commit_hash[:8] if active else None
        points = active.point_count if active else 0

        return status, branch, commit, points

    # =========================================================================
    # Persistent project registry
    # =========================================================================

    def _save_registry(self) -> None:
        """Persist current project registry to disk (atomic write)."""
        path = self._config.registry_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        entries = [
            {
                "project_id": r.project_id,
                "git_repo_path": r.git_repo_path,
                "display_name": r.display_name,
            }
            for r in self._projects.values()
        ]

        # Atomic write: temp file → rename
        dir_name = os.path.dirname(path) or "."
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            logger.debug("registry_saved", count=len(entries))
        except Exception:
            logger.exception("registry_save_failed", path=path)

    def _load_registry(self) -> List[dict]:
        """Load project registry from disk. Returns empty list on any error."""
        path = self._config.registry_path
        if not os.path.exists(path):
            return []

        try:
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                logger.warning("registry_invalid_format", path=path)
                return []
            return entries
        except Exception:
            logger.exception("registry_load_failed", path=path)
            return []

    async def _restore_persisted_projects(self) -> None:
        """Re-register all projects from persistent registry on startup.

        Failed or stale entries (missing repo, invalid git directory) are
        automatically pruned — the registry is re-saved after the loop so
        only successfully restored projects remain on disk.
        """
        entries = self._load_registry()
        if not entries:
            return

        logger.info("registry_restoring", count=len(entries))
        restored = 0
        skipped = 0

        for entry in entries:
            repo_path = entry.get("git_repo_path", "")
            display_name = entry.get("display_name", "")

            if not repo_path:
                continue

            # Validate repo still exists
            git_dir = os.path.join(repo_path, ".git")
            if not os.path.isdir(git_dir):
                logger.warning(
                    "registry_restore_skip_missing_repo",
                    repo_path=repo_path,
                )
                skipped += 1
                continue

            try:
                await self.register_project(repo_path, display_name)
                restored += 1
            except Exception:
                logger.warning(
                    "registry_restore_project_failed",
                    repo_path=repo_path,
                )
                skipped += 1

        # Purge stale entries: re-save so only successfully restored projects
        # remain in the persistent registry.
        if skipped > 0:
            self._save_registry()
            logger.info("registry_pruned_stale_entries", pruned=skipped)

        logger.info("registry_restored", restored=restored, total=len(entries))
