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

import asyncio
import json
import os
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from codebase_rag.common.logging import get_logger
from codebase_rag.common.paths import normalize_repo_key
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import ManagementApiConfig
from codebase_rag.services.agent.service import AgentService
from codebase_rag.services.embedding.service import EmbeddingService
from codebase_rag.services.event_detection.service import EventDetectionService
from codebase_rag.services.git_operations.service import GitOperationsService
from codebase_rag.services.management_api.models import (
    BatchRegisterProjectResponse,
    BatchRegisterResultItem,
    ClientStatusItem,
    ClientStatusResponse,
    DeleteProjectResponse,
    HealthResponse,
    InvalidRequestError,
    ManagementApiError,
    ProjectDetailResponse,
    ProjectExistsError,
    ProjectNotFoundError,
    ProjectRecord,
    ProjectRegistrationStatus,
    ProjectResponse,
    ProjectStatusResponse,
    SearchResponse,
    SearchResultItem,
    ServiceUnavailableError,
    TaskResponse,
    UnregisterRepoResponse,
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


def _consume_task_exception(task: asyncio.Task) -> None:
    """Retrieve an unobserved task exception to suppress asyncio warnings."""
    if not task.cancelled():
        task.exception()


# Liveness states of a registered repository copy.
#   alive   — the copy is still where it was registered.
#   gone    — the copy vanished while its volume is online (moved/deleted).
#   unknown — the volume is offline, so absence proves nothing.
_REPO_COPY_ALIVE = "alive"
_REPO_COPY_GONE = "gone"
_REPO_COPY_UNKNOWN = "unknown"


def _git_metadata_present(git_path: str) -> bool:
    """Return True when ``git_path`` holds real Git metadata.

    A ghost ``.git`` directory — only ``hooks/`` left behind by a hook
    injection aimed at a path that no longer existed — carries no ``HEAD``
    and must not be mistaken for a live repository copy.  Worktrees and
    submodules store a ``gitdir: <path>`` pointer file instead of a
    directory, so both shapes are handled here.
    """
    try:
        if os.path.isdir(git_path):
            return os.path.exists(os.path.join(git_path, "HEAD"))
        if os.path.isfile(git_path):
            with open(git_path, "r", encoding="utf-8", errors="replace") as f:
                pointer = f.readline().strip()
            if not pointer.startswith("gitdir:"):
                return False
            target = pointer.split(":", 1)[1].strip()
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(git_path), target)
            return os.path.exists(os.path.join(target, "HEAD"))
    except OSError:
        return False
    return False


def _path_volume_available(path: str) -> bool:
    """Return True when the volume holding ``path`` is currently online."""
    drive, _ = os.path.splitdrive(path)
    if drive:
        return os.path.exists(drive + os.sep)
    return os.path.exists(os.sep)


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
        agent: AgentService,
        knowledge_sync: KnowledgeSyncService,
    ) -> None:
        self._config_manager = config_manager
        self._git_ops = git_ops
        self._event_detection = event_detection
        self._workflow = workflow
        self._memory = memory_qdrant
        self._qdrant = qdrant_storage
        self._embedding = embedding
        self._agent = agent
        self._knowledge_sync = knowledge_sync

        # In-memory project registry: repo_key → ProjectRecord.
        # Multiple local copies of one project (same project_id) hold
        # distinct repo_keys and coexist as separate entries.
        self._projects: Dict[str, ProjectRecord] = {}

        # One shared moved-repo heal task per (project_id, incoming repo_key):
        # two copies of the same project may move at the same time and must
        # not join each other's heal.
        self._in_flight_heals: Dict[Tuple[str, str], asyncio.Task] = {}

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

        # Cancel in-flight moved-repo heals; shielded waiters already
        # received their result or cancellation.
        for task in list(self._in_flight_heals.values()):
            task.cancel()
        self._in_flight_heals.clear()

        self._initialized = False
        logger.info("management_api_service_shutdown")

    # =========================================================================
    # 2.1 Project management
    # =========================================================================

    async def register_project(
        self, git_repo_path: str, display_name: str = "",
    ) -> ProjectResponse:
        """Register a local repository copy: validate, resolve id, trigger init.

        Steps:
          1. Validate .git/ exists
          2. Derive project_id deterministically from the Git root commit;
             migrate any data attributed to a stale id (cached from a prior
             derivation rule) to the new id before continuing
          3. Register the copy (repo_key) to event detection service
          4. Register the copy to workflow orchestrator
          5. Trigger full initialization (async)
          6. Return project info

        Multiple copies of the same project (same project_id, different
        paths) coexist: each path gets its own registry entry, hooks,
        watcher and in-memory instances.
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

        repo_key = self._normalize_registered_path(git_repo_path)
        existing = self._projects.get(repo_key)
        sibling_copies = [
            record for record in self._projects.values()
            if record.project_id == project_id and record.repo_key != repo_key
        ]

        # Step 3: Register the copy to event detection
        await self._event_detection.register_project(project_id, git_repo_path)

        # Step 4: Register the copy to workflow orchestrator
        self._workflow.register_project(project_id, git_repo_path)

        # Get HEAD info
        head_info = await self._git_ops.get_head_info(git_repo_path)

        # Save to registry
        if not display_name:
            display_name = (
                existing.display_name if existing
                else os.path.basename(git_repo_path)
            )
        if existing is None and sibling_copies:
            logger.info(
                "project_copy_registered",
                project_id=project_id,
                repo_path=git_repo_path,
                existing_copies=len(sibling_copies),
            )
        elif existing is not None and existing.git_repo_path != git_repo_path:
            logger.info(
                "project_path_respelled",
                project_id=project_id,
                old_path=existing.git_repo_path,
                new_path=git_repo_path,
                repo_key=repo_key,
            )
        record = ProjectRecord(
            project_id=project_id,
            git_repo_path=git_repo_path,
            display_name=display_name,
            repo_key=repo_key,
            registered_at=(
                existing.registered_at if existing else datetime.utcnow()
            ),
        )
        self._projects[repo_key] = record
        try:
            self._save_registry(raise_on_error=True)
        except Exception as e:
            raise ManagementApiError(
                f"项目登记未完成：无法保存注册表 {self._config.registry_path}。"
                "请检查服务数据目录的写权限和可用空间后，使用相同仓库路径重试。"
                "部分操作可能已执行，未进行完整回滚。"
                f"原因：{e}"
            ) from e

        # Step 5: Trigger full initialization (via task queue, serial per copy)
        branch = head_info.branch_name or "HEAD"
        self._workflow.enqueue_initialization(
            project_id, git_repo_path, branch, head_info.commit_hash,
        )

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

    async def list_registered_projects(self) -> List[ProjectRecord]:
        """Return a detached registration snapshot without probing indexes."""
        return [replace(record) for record in self._projects.values()]

    @staticmethod
    def _normalize_registered_path(path: str) -> str:
        if not isinstance(path, str) or not path.strip() or not os.path.isabs(path):
            raise InvalidRequestError("git_repo_path 必须为非空的仓库根目录绝对路径。")
        return normalize_repo_key(path)

    async def get_project_registration_status(
        self, git_repo_path: str,
    ) -> ProjectRegistrationStatus:
        """Report registration and a running initialization, not readiness."""
        normalized = self._normalize_registered_path(git_repo_path)
        record = self._projects.get(normalized)
        if record is None:
            return ProjectRegistrationStatus(
                git_repo_path=normalized, registered=False,
            )
        task = self._workflow.get_workflow_status(record.repo_key).current_task
        return ProjectRegistrationStatus(
            git_repo_path=record.git_repo_path,
            registered=True,
            initialization_running=task is not None and task.task_type == "initialization",
        )

    # =========================================================================
    # 2.2 Moved-repository auto-heal
    # =========================================================================

    async def heal_moved_project(self, project_id: str, incoming_path: str) -> bool:
        """Re-register a repository copy whose path moved to a new location.

        MCP read paths always receive the caller's current Git repo root.
        When that root carries this project's identity while the registration
        of the same path is missing and older entries of the project point at
        paths that no longer exist, the repository was moved and the stale
        entries are healed here automatically — reusing the normal
        ``register_project`` flow so the registry entry, event detection,
        hooks and workflow state all converge, and missed commits are caught
        up.

        Conservative by design — healing only runs when ALL hold:
          1. ``incoming_path`` is a Git repo root (contract check; the
             service never ascends from subdirectories to guess a root).
          2. The project_id derived from ``incoming_path``'s root commit
             equals ``project_id`` — identity is verified from repository
             content, never from the caller's claim or the ``.git`` cache
             file alone.
          3. Either:
             a. no repository copy of this project is registered at the
                incoming path, no copy sits on an unreachable volume, and
                at least one older copy is really gone (``.git`` metadata
                missing while its volume is online) — a move, not a copy.
                Only those vanished copies are replaced; surviving copies
                keep their entry, hooks, watcher and in-memory instance.
             b. the project has no registered copies at all (the entries
                were pruned by a restart-restore while the ``.git`` cache
                file still travels with the repository).

        Copies that are still alive, or that live on an offline volume
        (network share / removable disk), never trigger a heal: adding a
        fresh clone to the index stays an explicit decision.

        Single-flight per (project_id, incoming repo_key): concurrent
        callers for the same moved copy join the same heal task.  The
        pre-check is cheap (registry lookups + ``.git``/volume probes, no
        git subprocess) so read paths can call this on every request.

        Returns:
            True when a re-registration was performed by this call (or a
            joined in-flight heal); False when no heal was applicable.
        """
        if not self._initialized or not incoming_path:
            return False

        # Cheap pre-check (no git subprocess) so the hot path — a copy is
        # already registered at the caller's path — returns immediately.
        if not self._move_heal_precheck(project_id, incoming_path):
            return False

        heal_key = self._move_heal_key(project_id, incoming_path)
        task = self._in_flight_heals.get(heal_key)
        if task is None:
            task = asyncio.create_task(
                self._run_move_heal(project_id, incoming_path),
                name=f"move_heal:{project_id}:{heal_key[1]}",
            )
            task.add_done_callback(_consume_task_exception)
            self._in_flight_heals[heal_key] = task
        else:
            logger.info(
                "move_heal_joined",
                project_id=project_id,
                incoming_path=incoming_path,
            )

        # shield: a cancelled caller must not abort an in-flight heal
        return await asyncio.shield(task)

    def _move_heal_precheck(self, project_id: str, incoming_path: str) -> bool:
        """Synchronous gate deciding whether a heal task should start.

        Cheap by design (no git subprocess): registry lookups plus
        ``.git``/volume probes, so MCP read paths can call it on every
        request.
        """
        try:
            normalized_incoming = self._normalize_registered_path(incoming_path)
        except InvalidRequestError:
            return False

        # A copy already registered at the caller's path — nothing to heal.
        if normalized_incoming in self._projects:
            return False

        copies = [
            record for record in self._projects.values()
            if record.project_id == project_id
        ]
        if copies:
            states = [self._classify_repo_copy(record) for record in copies]
            # A copy on an offline volume (network share / removable disk)
            # must never be read as "deleted".
            if _REPO_COPY_UNKNOWN in states:
                return False
            # No copy vanished: the caller's path is a *new* copy, not a
            # moved one.  Indexing a fresh clone stays an explicit
            # decision.
            if _REPO_COPY_GONE not in states:
                return False
            # At least one copy is gone while the incoming path carries the
            # same identity: exactly those copies moved.  Surviving copies
            # are untouched.

        # Contract check: the caller's path must itself be a Git repo root
        # (.git may be a directory or a worktree file).
        return os.path.exists(os.path.join(incoming_path, ".git"))

    def _classify_repo_copy(self, record: ProjectRecord) -> str:
        """Classify a registered copy as alive / gone / unreachable."""
        git_path = os.path.join(record.git_repo_path, ".git")
        if _git_metadata_present(git_path):
            return _REPO_COPY_ALIVE
        if _path_volume_available(record.git_repo_path):
            return _REPO_COPY_GONE
        return _REPO_COPY_UNKNOWN

    def _move_heal_key(self, project_id: str, incoming_path: str) -> Tuple[str, str]:
        """Single-flight key: one heal task per moved copy."""
        try:
            repo_key = self._normalize_registered_path(incoming_path)
        except InvalidRequestError:
            repo_key = incoming_path
        return (project_id, repo_key)

    async def _run_move_heal(self, project_id: str, incoming_path: str) -> bool:
        """Run one heal and release its single-flight slot."""
        heal_key = self._move_heal_key(project_id, incoming_path)
        try:
            return await self._do_heal_moved_project(project_id, incoming_path)
        finally:
            if self._in_flight_heals.get(heal_key) is asyncio.current_task():
                del self._in_flight_heals[heal_key]

    async def _do_heal_moved_project(self, project_id: str, incoming_path: str) -> bool:
        """Validated heal path: re-check state, verify identity, register."""
        # Re-validate under the single-flight task — state may have changed
        # while waiting to join.
        if not self._move_heal_precheck(project_id, incoming_path):
            return False

        # Identity verification: derive from repository content (root
        # commit).  The .git cache file and the caller's claim are never
        # trusted on their own — a mismatch means no heal, leaving the
        # normal error paths to surface the inconsistency.
        try:
            root_commit = await self._git_ops.get_root_commit(incoming_path)
        except Exception:
            logger.warning(
                "move_heal_root_commit_failed",
                repo_path=incoming_path,
            )
            return False
        if not root_commit:
            return False
        derived_id = _derive_project_id_from_root_commit(root_commit)
        if derived_id != project_id:
            logger.warning(
                "move_heal_identity_mismatch",
                derived_project_id=derived_id,
                expected_project_id=project_id,
                repo_path=incoming_path,
            )
            return False

        # Only copies that really vanished are migrated.  Alive copies keep
        # their entry, hooks, watcher and in-memory instance; copies sitting
        # on an offline volume are left alone until the volume is back.
        dead_copies = [
            record for record in self._projects.values()
            if record.project_id == project_id
            and self._classify_repo_copy(record) == _REPO_COPY_GONE
        ]
        inherited_display_name = (
            dead_copies[0].display_name if dead_copies else ""
        )

        # Step 1: drop the vanished copies from event detection first, so no
        # watcher can re-inject hooks into a directory that no longer exists
        # (that injection is what recreates a ghost .git at a dead path).
        for record in dead_copies:
            try:
                await self._event_detection.unregister_repo(record.git_repo_path)
            except Exception:
                logger.exception(
                    "move_heal_event_unregister_failed",
                    project_id=project_id,
                    repo_path=record.git_repo_path,
                )

        # Step 2: register the moved path through the normal flow (registry
        # entry, hooks, watcher, workflow state, initialization catch-up),
        # keeping the display name of the copy it replaces.
        try:
            await self.register_project(
                incoming_path, display_name=inherited_display_name,
            )
        except Exception:
            logger.exception(
                "move_heal_register_failed",
                repo_path=incoming_path,
            )
            # The vanished copies are not coming back — drop them instead of
            # leaving registry entries that point at dead paths.
            self._drop_dead_copies(dead_copies)
            self._save_registry()
            return False

        # Step 3: purge the vanished copies from workflow / memory /
        # registry — scoped per repo_key, never per project_id, so surviving
        # copies keep their watchers and instances.
        for record in dead_copies:
            try:
                self._workflow.unregister_repo(record.git_repo_path)
            except Exception:
                logger.exception(
                    "move_heal_workflow_unregister_failed",
                    project_id=project_id,
                    repo_path=record.git_repo_path,
                )
            try:
                await self._memory.destroy_repo_instances(record.repo_key)
            except Exception:
                logger.exception(
                    "move_heal_instance_destroy_failed",
                    project_id=project_id,
                    repo_key=record.repo_key,
                )
        self._drop_dead_copies(dead_copies)
        self._save_registry()

        logger.info(
            "moved_project_healed",
            project_id=project_id,
            healed_copies=len(dead_copies),
            new_path=incoming_path,
            display_name=inherited_display_name or None,
        )
        return True

    def _drop_dead_copies(self, dead_copies: List[ProjectRecord]) -> None:
        """Remove already-migrated copies from the in-memory registry."""
        for record in dead_copies:
            self._projects.pop(record.repo_key, None)

    async def list_projects(self) -> List[ProjectResponse]:
        """List all registered repository copies (one entry per copy)."""
        results = []
        for record in self._projects.values():
            status, branch, commit, points = await self._get_project_summary(
                record.project_id, record.repo_key,
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

    def _first_copy(self, project_id: str) -> Optional[ProjectRecord]:
        """Return the first registered copy of a project, if any."""
        for record in self._projects.values():
            if record.project_id == project_id:
                return record
        return None

    async def get_project(self, project_id: str) -> ProjectDetailResponse:
        """Get detailed info for a single project (first registered copy)."""
        record = self._first_copy(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        status, branch, commit, points = await self._get_project_summary(
            project_id, record.repo_key,
        )

        # Workflow status
        wf_status = self._workflow.get_workflow_status(record.repo_key)
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

    async def unregister_project(self, project_id: str) -> DeleteProjectResponse:
        """Unregister every local copy of a project (data partition).

        Persistent Qdrant points are always preserved for reuse — this
        operation only stops local tracking (hooks, watchers, in-memory
        instances, registry entries) for all registered copies.

        Steps:
          1. Stop event monitoring for every copy
          2. Unregister every copy from workflow
          3. Destroy in-memory instances of the project
          4. Remove the registry entries
        """
        copies = [
            record for record in self._projects.values()
            if record.project_id == project_id
        ]
        if not copies:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        # Step 1
        await self._event_detection.unregister_project(project_id)

        # Step 2
        self._workflow.unregister_project(project_id)

        # Step 3
        await self._memory.destroy_all_instances(project_id)

        # Step 4
        for record in copies:
            self._projects.pop(record.repo_key, None)
        self._save_registry()

        logger.info(
            "project_unregistered",
            project_id=project_id,
            unregistered_repos=len(copies),
        )

        return DeleteProjectResponse(
            project_id=project_id,
            unregistered_repos=len(copies),
            deleted_points=0,
        )

    async def unregister_repo(self, git_repo_path: str) -> UnregisterRepoResponse:
        """Unregister a single local repository copy.

        Other copies of the same project (same project_id, different
        paths) keep their tracking.  Persistent Qdrant points are always
        preserved for reuse.
        """
        repo_key = self._normalize_registered_path(git_repo_path)
        record = self._projects.get(repo_key)
        if record is None:
            raise ProjectNotFoundError(
                f"No registered repository at '{git_repo_path}'"
            )

        # Stop event monitoring and workflow tracking for this copy only
        await self._event_detection.unregister_repo(record.git_repo_path)
        self._workflow.unregister_repo(record.git_repo_path)
        await self._memory.destroy_repo_instances(repo_key)

        self._projects.pop(repo_key, None)
        self._save_registry()

        logger.info(
            "repo_unregistered",
            project_id=record.project_id,
            git_repo_path=record.git_repo_path,
        )

        return UnregisterRepoResponse(
            git_repo_path=record.git_repo_path,
            project_id=record.project_id,
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
        """Execute semantic search on a project's first-copy in-memory instance."""
        record = self._first_copy(project_id)
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
            record.repo_key,
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
        active = await self._memory.get_active_instance_info(record.repo_key)
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
        """Trigger full rebuild for a project's first copy (async)."""
        record = self._first_copy(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        head = await self._git_ops.get_head_info(record.git_repo_path)
        branch = head.branch_name or "HEAD"
        task_id = str(uuid.uuid4())

        # Force rebuild is intentionally non-destructive: workflow initialization
        # now loads a staged replacement and promotes it only after success, so
        # the existing active in-memory instance remains searchable during reload.
        self._workflow.enqueue_initialization(
            project_id, record.git_repo_path, branch, head.commit_hash,
        )

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
        """Trigger manual incremental sync for a project's first copy."""
        record = self._first_copy(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        head = await self._git_ops.get_head_info(record.git_repo_path)
        task_id = str(uuid.uuid4())

        branch = head.branch_name or "HEAD"
        active = await self._memory.get_active_instance_info(record.repo_key)
        if active and active.commit_hash != head.commit_hash:
            self._workflow.enqueue_content_commit(
                project_id, record.git_repo_path, branch,
                head.commit_hash, active.commit_hash,
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
        """Get detailed index status for a project (first registered copy)."""
        record = self._first_copy(project_id)
        if not record:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")

        active = await self._memory.get_active_instance_info(record.repo_key)
        wf_status = self._workflow.get_workflow_status(record.repo_key)

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

    def is_admin_ui_enabled(self) -> bool:
        """Return whether the local passwordless admin dashboard is enabled."""
        return self._config.admin_ui.enabled

    async def get_client_status(self, live: bool = False) -> ClientStatusResponse:
        """Return external client and model provider availability for the dashboard."""
        timeout = self._config.client_status.live_probe_timeout
        embedding_status = self._embedding.get_client_status_snapshot()
        qdrant_status = self._qdrant.get_client_status_snapshot()
        llm_status = self._agent.get_llm_provider_status_snapshot()

        probe_results: Dict[str, Optional[Dict[str, Any]]] = {
            "dashscope_embedding": None,
            "gemini_embedding": None,
            "qdrant": None,
        }
        llm_probe_results: Dict[str, Optional[Dict[str, Any]]] = {
            str(item["id"]): None for item in llm_status
        }
        live_probe = live or self._config.client_status.live_probe_enabled
        if live_probe:
            dashscope_probe, gemini_probe, qdrant_probe, llm_probe_results = await asyncio.gather(
                self._embedding.probe_sparse_provider(timeout),
                self._embedding.probe_dense_provider(timeout),
                self._qdrant.probe_client(timeout),
                self._agent.probe_llm_providers(timeout),
            )
            probe_results = {
                "dashscope_embedding": dashscope_probe,
                "gemini_embedding": gemini_probe,
                "qdrant": qdrant_probe,
            }

        return self._build_client_status_response(
            embedding_status=embedding_status,
            qdrant_status=qdrant_status,
            llm_status=llm_status,
            probe_results=probe_results,
            llm_probe_results=llm_probe_results,
            live_probe=live_probe,
        )

    async def probe_client_status(self, client_id: str) -> ClientStatusResponse:
        timeout = self._config.client_status.live_probe_timeout
        embedding_status = self._embedding.get_client_status_snapshot()
        qdrant_status = self._qdrant.get_client_status_snapshot()
        llm_status = self._agent.get_llm_provider_status_snapshot()
        llm_provider_names = {str(item["id"]) for item in llm_status}
        valid_client_ids = {
            "dashscope_embedding",
            "gemini_embedding",
            "qdrant",
            *(f"llm_provider:{provider_name}" for provider_name in llm_provider_names),
        }
        if client_id not in valid_client_ids:
            raise InvalidRequestError(f"Client status item is not available for probing: {client_id}")

        probe_results: Dict[str, Optional[Dict[str, Any]]] = {
            "dashscope_embedding": None,
            "gemini_embedding": None,
            "qdrant": None,
        }
        llm_probe_results: Dict[str, Optional[Dict[str, Any]]] = {
            str(item["id"]): None for item in llm_status
        }

        if client_id == "dashscope_embedding":
            probe_results["dashscope_embedding"] = await self._embedding.probe_sparse_provider(timeout)
        elif client_id == "gemini_embedding":
            probe_results["gemini_embedding"] = await self._embedding.probe_dense_provider(timeout)
        elif client_id == "qdrant":
            probe_results["qdrant"] = await self._qdrant.probe_client(timeout)
        else:
            provider_name = client_id.split(":", 1)[1]
            llm_probe_results[provider_name] = await self._agent.probe_llm_provider(provider_name, timeout)

        return self._build_client_status_response(
            embedding_status=embedding_status,
            qdrant_status=qdrant_status,
            llm_status=llm_status,
            probe_results=probe_results,
            llm_probe_results=llm_probe_results,
            live_probe=True,
        )

    def _build_client_status_response(
        self,
        embedding_status: Dict[str, Any],
        qdrant_status: Dict[str, Any],
        llm_status: List[Dict[str, Any]],
        probe_results: Dict[str, Optional[Dict[str, Any]]],
        llm_probe_results: Dict[str, Optional[Dict[str, Any]]],
        live_probe: bool,
    ) -> ClientStatusResponse:
        clients = [
            self._build_embedding_client_item(
                data=embedding_status["sparse"],
                client_id="dashscope_embedding",
                name="DashScope embedding",
                expected_provider="dashscope",
                proxy_policy="不读取 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY（trust_env=False）",
                live_probe=probe_results["dashscope_embedding"],
            ),
            self._build_embedding_client_item(
                data=embedding_status["dense"],
                client_id="gemini_embedding",
                name="Gemini dense embedding",
                expected_provider="gemini",
                proxy_policy="HTTPX 默认可能读取环境代理；未配置代理变量时直连",
                live_probe=probe_results["gemini_embedding"],
            ),
            self._build_qdrant_client_item(
                data=qdrant_status,
                live_probe=probe_results["qdrant"],
            ),
        ]
        clients.extend(
            self._build_llm_provider_client_item(
                data=item,
                live_probe=llm_probe_results.get(str(item["id"])),
            )
            for item in llm_status
        )

        return ClientStatusResponse(
            status=self._aggregate_client_status([client.status for client in clients]),
            generated_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            live_probe=live_probe,
            proxy_environment=self._get_proxy_environment_snapshot(),
            clients=clients,
        )

    async def reload_config(self) -> Dict[str, Any]:
        """Trigger configuration hot reload via ConfigManager.

        Re-reads config.yaml and .env from disk, validates it, invokes registered
        reconcilers across all downstream services, and replaces the in-memory
        AppConfig singleton.

        Returns:
            Dict containing requires_restart, restart_reasons, and reconciled_services.

        Raises:
            Exception: If config file is invalid or reload fails.
        """
        await self._config_manager.reload()
        logger.info("config_reload_triggered_via_api")
        return {
            "requires_restart": bool(self._config_manager.last_restart_reasons),
            "restart_reasons": list(self._config_manager.last_restart_reasons),
            "reconciled_services": list(self._config_manager.last_reconciled_services),
        }

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _build_embedding_client_item(
        self,
        data: Dict[str, Any],
        client_id: str,
        name: str,
        expected_provider: str,
        proxy_policy: str,
        live_probe: Optional[Dict[str, Any]],
    ) -> ClientStatusItem:
        status = data.get("status", "unknown")
        provider = data.get("provider")
        if provider != expected_provider:
            status = "not_configured"
        if live_probe is not None:
            status = live_probe.get("status", "unknown")

        details = {
            "initialized": data.get("initialized", False),
            "model": data.get("model"),
            "dimension": data.get("dimension"),
            "batch_size": data.get("batch_size"),
            "healthy_keys": data.get("healthy_keys", 0),
            "total_keys": data.get("total_keys", 0),
            "request_timeout": data.get("request_timeout"),
            "proxy_policy": proxy_policy,
        }
        if provider != expected_provider:
            details["expected_provider"] = expected_provider
            details["configured_provider"] = provider

        return ClientStatusItem(
            id=client_id,
            name=name,
            kind="embedding",
            provider=provider,
            endpoint=data.get("endpoint"),
            status=status,
            details={k: v for k, v in details.items() if v is not None},
            key_status=data.get("key_status", []),
            live_probe=live_probe,
        )

    @staticmethod
    def _build_qdrant_client_item(
        data: Dict[str, Any],
        live_probe: Optional[Dict[str, Any]],
    ) -> ClientStatusItem:
        status = data.get("status", "unknown")
        if live_probe is not None:
            status = live_probe.get("status", "unknown")
        details = {
            "initialized": data.get("initialized", False),
            "client_created": data.get("client_created", False),
            "collection": data.get("collection"),
            "host": data.get("host"),
            "port": data.get("port"),
            "grpc_port": data.get("grpc_port"),
            "https": data.get("https"),
            "prefer_grpc": data.get("prefer_grpc"),
            "timeout": data.get("timeout"),
            "dense_vector": data.get("dense_vector"),
            "sparse_vector": data.get("sparse_vector"),
            "api_key_configured": data.get("api_key_configured", False),
            "proxy_policy": "未配置项目级代理；不需要 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY",
        }
        return ClientStatusItem(
            id="qdrant",
            name="Qdrant",
            kind="vector_storage",
            provider="qdrant",
            endpoint=data.get("endpoint"),
            status=status,
            details={k: v for k, v in details.items() if v is not None},
            key_status=data.get("key_status", []),
            live_probe=live_probe,
        )

    @staticmethod
    def _build_llm_provider_client_item(
        data: Dict[str, Any],
        live_probe: Optional[Dict[str, Any]],
    ) -> ClientStatusItem:
        status = data.get("status", "unknown")
        if live_probe is not None:
            status = live_probe.get("status", "unknown")
        models = data.get("models", [])
        details = {
            "initialized": data.get("initialized", False),
            "provider_loaded": data.get("provider_loaded", False),
            "api_format": data.get("api_format"),
            "models": ", ".join(
                model.get("api_model") or model.get("id")
                for model in models
                if isinstance(model, dict)
            ),
            "model_count": len(models) if isinstance(models, list) else 0,
            "healthy_keys": data.get("healthy_keys", 0),
            "total_keys": data.get("total_keys", 0),
            "request_timeout": data.get("request_timeout"),
            "max_concurrency": data.get("max_concurrency"),
            "is_default_provider": data.get("is_default_provider", False),
            "default_model": data.get("default_model"),
            "probe_policy": "仅点击实时检测时发送一次最小 LLM 消息请求",
        }
        return ClientStatusItem(
            id=f"llm_provider:{data.get('id')}",
            name=f"LLM · {data.get('name')}",
            kind="model_provider",
            provider=str(data.get("id")),
            endpoint=data.get("endpoint"),
            status=status,
            details={k: v for k, v in details.items() if v is not None},
            key_status=data.get("key_status", []),
            live_probe=live_probe,
        )

    @staticmethod
    def _aggregate_client_status(statuses: List[str]) -> str:
        if not statuses:
            return "unknown"
        if any(status in {"unavailable", "error", "timeout", "not_configured"} for status in statuses):
            return "unavailable"
        if any(status == "degraded" for status in statuses):
            return "degraded"
        if all(status == "available" for status in statuses):
            return "available"
        return "unknown"

    @staticmethod
    def _get_proxy_environment_snapshot() -> Dict[str, Any]:
        proxy_names = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        )
        detected = [name for name in proxy_names if os.environ.get(name)]
        return {
            "required": False,
            "detected": bool(detected),
            "detected_variables": detected,
            "message": (
                "检测到代理环境变量，未显式禁用 trust_env 的 HTTP 客户端可能读取"
                if detected
                else "未检测到代理环境变量；HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY 不是必填项"
            ),
        }

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

        # 5) Local registry (in-memory dict keyed by repo_key — keys stay
        # valid, only the record's project_id field needs rewriting).
        for record in self._projects.values():
            if record.project_id == old_id:
                record.project_id = new_id
        self._save_registry()

        logger.info(
            "project_id_migration_done",
            old_project_id=old_id,
            new_project_id=new_id,
        )

    async def _get_project_summary(
        self, project_id: str, repo_key: str,
    ) -> tuple:
        """Get (status, branch, commit_hash_short, knowledge_points) for a copy."""
        active = await self._memory.get_active_instance_info(repo_key)
        wf_status = self._workflow.get_workflow_status(repo_key)

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

    def _save_registry(self, *, raise_on_error: bool = False) -> None:
        """Persist the registry, optionally propagating write failures."""
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
        except Exception as e:
            logger.exception("registry_save_failed", path=path)
            if raise_on_error:
                raise ManagementApiError(f"无法保存注册表 {path}：{e}") from e

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
