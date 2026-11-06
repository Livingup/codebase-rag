"""Workflow Orchestrator Service — core business logic hub.

Receives GitEvent notifications from Event Detection Service, orchestrates
branch-switch and knowledge-production workflows by coordinating:
  - 07-Git Operations Service (file trees, diffs, content)
  - 01-Qdrant Storage Service (persistent CRUD)
  - 02-Memory Qdrant Service (in-memory branch instances)
  - 03-Agent Service (single knowledge_producer agent with tool calling)
  - 04-Embedding Service (vector generation, called inside tool handler)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any, Dict, List, Optional, Set, Union

from qdrant_client import models

from codebase_rag.common.logging import get_logger
from codebase_rag.common.paths import normalize_repo_key
from codebase_rag.common.token_counter import TokenCounter
from codebase_rag.common.types import (
    EmbedInputItem,
    FileType,
    IMAGE_TOKEN_COST,
    MultimodalEmbedInput,
)
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import WorkflowConfig
from codebase_rag.services.agent.errors import AgentTaskComplete
from codebase_rag.services.agent.models import AgentDefinition, ToolSchema
from codebase_rag.services.agent.service import AgentService
from codebase_rag.services.embedding.service import EmbeddingService
from codebase_rag.services.event_detection.models import EventType, GitEvent
from codebase_rag.services.git_operations.errors import GitFileNotFoundError
from codebase_rag.services.git_operations.models import ChangedFiles, RenamedFileEntry
from codebase_rag.services.git_operations.service import GitOperationsService
from codebase_rag.services.knowledge_sync.models import KnowledgeSyncSource
from codebase_rag.services.knowledge_sync.service import KnowledgeSyncService
from codebase_rag.services.memory_qdrant.service import MemoryQdrantService
from codebase_rag.services.qdrant_storage.models import PointData
from codebase_rag.services.qdrant_storage.service import QdrantStorageService
from codebase_rag.services.workflow.models import (
    BranchSwitchResult,
    ContentCommitResult,
    DeadLetterRecord,
    FailedFile,
    FailedFileRecord,
    InitializationResult,
    Progress,
    TaskInfo,
    WorkflowError,
    WorkflowStatus,
)
from codebase_rag.services.workflow.task_queue import ProjectTaskQueue, WorkflowTask

logger = get_logger(__name__)

_KP_AGENT_PREFIX = "knowledge_producer"


class WorkflowOrchestratorService:
    """Core business logic hub — orchestrates branch switch and knowledge production.

    Args:
        config: Workflow configuration section from AppConfig.
        git_ops: Git operations service.
        qdrant_storage: Qdrant persistent storage service.
        memory_qdrant: In-memory Qdrant service.
        agent: Agent service.
        embedding: Embedding service.
        token_counter: Shared token counter utility.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        git_ops: GitOperationsService,
        qdrant_storage: QdrantStorageService,
        memory_qdrant: MemoryQdrantService,
        agent: AgentService,
        embedding: EmbeddingService,
        token_counter: Optional[TokenCounter] = None,
        knowledge_sync: Optional[KnowledgeSyncService] = None,
    ) -> None:
        self._config_manager = config_manager
        self._git_ops = git_ops
        self._qdrant = qdrant_storage
        self._memory = memory_qdrant
        self._agent = agent
        self._embedding = embedding
        self._token_counter = token_counter or TokenCounter()
        self._knowledge_sync = knowledge_sync

        # Task queue: per-project serial, cross-project parallel
        self._task_queue = ProjectTaskQueue()
        self._task_queue.set_executor(self._execute_task)

        # Repository copy registry (set by caller via register_project):
        # repo_key → project_id.  The repo_key (canonical normalized path)
        # doubles as a usable path for Git operations, and one project_id
        # may map from multiple repo_keys (multiple local copies).
        self._project_repos: Dict[str, str] = {}

        # Current task info per repository copy (for status queries)
        self._current_tasks: Dict[str, TaskInfo] = {}
        self._last_completed: Dict[str, TaskInfo] = {}

        # knowledge_producer agent 状态
        self._kp_system_prompt: str = ""
        self._kp_tools: List[ToolSchema] = []
        # 按优先级排序的 agent 名称列表（每个 agent 绑定一个 provider/model）
        self._kp_agent_names: List[str] = []
        # 按优先级排序的去重服务商名称列表（用于 select_provider 容量感知选择）
        self._kp_provider_order: List[str] = []
        # 服务商名称 → 该服务商下所有 agent 名称列表（按优先级排序）
        self._kp_provider_agents: Dict[str, List[str]] = {}

        # Retry queue: project_id → list of FailedFileRecord
        self._retry_queues: Dict[str, List[FailedFileRecord]] = {}
        # Dead letter queue: project_id → list of DeadLetterRecord
        self._dead_letters: Dict[str, List[DeadLetterRecord]] = {}
        # Compensation task global backoff state
        self._comp_consecutive_failures: int = 0
        self._comp_last_round_at: float = 0.0
        self._comp_task: Optional[asyncio.Task] = None

        self._initialized = False

    @property
    def _config(self) -> WorkflowConfig:
        return self._config_manager.config.workflow

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Register agent definitions and initialize task queue."""
        logger.info("workflow_service_initializing")

        await self._register_agents()

        # Start compensation task after agents are registered and ready
        if self._config.compensation.enabled:
            self._comp_task = asyncio.create_task(self._compensation_loop())
            logger.info("compensation_task_started")

        self._initialized = True
        self._config_manager.register_reconciler("workflow", self.reconcile)
        logger.info("workflow_service_initialized")

    def is_compensation_running(self) -> bool:
        """Observe the existing retry worker without starting or changing it."""
        task = self._comp_task
        return bool(
            self._initialized
            and self._config.compensation.enabled
            and task is not None
            and not task.done()
            and task.cancelling() == 0
        )

    async def reconcile(self, old_config: AppConfig, new_config: AppConfig) -> None:
        """Reconcile agent definitions, routing queues, and compensation loop."""
        logger.info("workflow_service_reconciling")

        # 1. Rebuild and re-register agents if workflow prompts or providers changed
        await self._register_agents()

        # 2. Reconcile compensation background task
        old_comp = old_config.workflow.compensation.enabled
        new_comp = new_config.workflow.compensation.enabled

        if not old_comp and new_comp:
            if self._comp_task is None or self._comp_task.done():
                self._comp_task = asyncio.create_task(self._compensation_loop())
                logger.info("compensation_task_started_via_reconcile")
        elif old_comp and not new_comp:
            if self._comp_task is not None and not self._comp_task.done():
                self._comp_task.cancel()
                try:
                    await self._comp_task
                except asyncio.CancelledError:
                    pass
                self._comp_task = None
                logger.info("compensation_task_stopped_via_reconcile")

        logger.info(
            "workflow_service_reconciled",
            kp_providers=list(self._kp_provider_order),
        )

    async def shutdown(self) -> None:
        """Gracefully stop task queue and wait for current tasks."""
        self._config_manager.unregister_reconciler("workflow")
        logger.info("workflow_service_shutting_down")

        # Cancel compensation task
        if self._comp_task is not None and not self._comp_task.done():
            self._comp_task.cancel()
            try:
                await self._comp_task
            except asyncio.CancelledError:
                pass
            self._comp_task = None
            logger.info("compensation_task_stopped")

        await self._task_queue.shutdown(timeout=30.0)

        # Persist retry queues before clearing state
        for project_id in list(self._retry_queues):
            self._save_retry_queue(project_id)
        for project_id in list(self._dead_letters):
            self._save_dead_letter(project_id)

        self._project_repos.clear()
        self._initialized = False
        logger.info("workflow_service_shutdown")

    def register_project(self, project_id: str, repo_path: str) -> None:
        """Register a repository copy's project mapping (called by management API).

        Also loads persisted retry/dead-letter queues so the background
        compensation task can pick them up after service restart.
        """
        self._project_repos[normalize_repo_key(repo_path)] = project_id
        # Load persisted queues (idempotent — skips if already in memory)
        if project_id not in self._retry_queues:
            loaded = self._load_retry_queue(project_id)
            if loaded:
                self._retry_queues[project_id] = loaded
                logger.info(
                    "retry_queue_loaded",
                    project_id=project_id,
                    pending=len(loaded),
                )
        if project_id not in self._dead_letters:
            loaded = self._load_dead_letter(project_id)
            if loaded:
                self._dead_letters[project_id] = loaded

    def unregister_project(self, project_id: str) -> None:
        """Unregister every local copy of a project."""
        stale_keys = [
            repo_key for repo_key, pid in self._project_repos.items()
            if pid == project_id
        ]
        for repo_key in stale_keys:
            del self._project_repos[repo_key]

    def unregister_repo(self, repo_path: str) -> None:
        """Unregister a single local repository copy."""
        self._project_repos.pop(normalize_repo_key(repo_path), None)

    def get_repo_key_for_project(self, project_id: str) -> Optional[str]:
        """Return a live repo_key of a project, if any.

        With multiple local copies of one project, keys of copies that
        moved or were deleted may still linger here; prefer a copy whose
        ``.git`` still exists so background work never targets a dead
        path, and fall back to the first match when none is left.
        """
        fallback: Optional[str] = None
        for repo_key, pid in self._project_repos.items():
            if pid != project_id:
                continue
            if os.path.exists(os.path.join(repo_key, ".git")):
                return repo_key
            if fallback is None:
                fallback = repo_key
        return fallback

    def rebrand_project_id(
        self, old_project_id: str, new_project_id: str,
    ) -> int:
        """Re-attribute workflow state from ``old_project_id`` to ``new_project_id``.

        Migrates everything keyed by project_id within the orchestrator:
          1. Rename retry-queue and dead-letter persistence files on disk.
          2. Re-key in-memory ``_retry_queues`` / ``_dead_letters`` /
             ``_project_repos`` dicts.
          3. Rewrite ``project_id`` field on every ``FailedFileRecord`` /
             ``DeadLetterRecord`` so a future save round-trips the new id.

        Idempotent: a no-op when both ids are equal.

        Returns:
            Total number of records (retry + dead-letter) whose project_id
            field was rewritten.
        """
        if old_project_id == new_project_id:
            return 0

        # Step 1: Rename persistence files (best-effort; missing is fine)
        for old_path, new_path in (
            (self._retry_queue_path(old_project_id),
             self._retry_queue_path(new_project_id)),
            (self._dead_letter_path(old_project_id),
             self._dead_letter_path(new_project_id)),
        ):
            if os.path.exists(old_path):
                try:
                    os.replace(old_path, new_path)
                except OSError:
                    logger.exception(
                        "workflow_rebrand_rename_failed",
                        old_path=old_path,
                        new_path=new_path,
                    )

        # Step 2: Re-key in-memory dicts
        affected = 0
        if old_project_id in self._retry_queues:
            queue = self._retry_queues.pop(old_project_id)
            for r in queue:
                if r.project_id == old_project_id:
                    r.project_id = new_project_id
                    affected += 1
            self._retry_queues[new_project_id] = queue

        if old_project_id in self._dead_letters:
            dl = self._dead_letters.pop(old_project_id)
            for r in dl:
                if r.project_id == old_project_id:
                    r.project_id = new_project_id
                    affected += 1
            self._dead_letters[new_project_id] = dl

        # Registry keys are repo_keys (no project_id inside), so only the
        # mapped project_id values need rewriting.
        for repo_key, pid in list(self._project_repos.items()):
            if pid == old_project_id:
                self._project_repos[repo_key] = new_project_id

        logger.info(
            "workflow_rebrand_project_id_done",
            old_project_id=old_project_id,
            new_project_id=new_project_id,
            affected_records=affected,
        )
        return affected

    # =========================================================================
    # 2.1 Event reception (callback for EventDetectionService.subscribe)
    # =========================================================================

    async def on_git_event(self, event: GitEvent) -> None:
        """Receive a GitEvent and dispatch to appropriate workflow."""
        repo_key = self._resolve_repo_key_from_event(event)
        if repo_key is None:
            logger.warning(
                "workflow_event_unroutable_repo",
                project_id=event.project_id,
                repo=getattr(event, "git_repo_path", "") or "",
            )
            return

        logger.info(
            "workflow_event_received",
            event_id=event.event_id,
            project_id=event.project_id,
            repo_key=repo_key,
            event_type=event.event_type.value,
            branch=event.branch_name,
            commit=event.commit_hash[:8],
            prev_branch=event.previous_branch_name or "",
            prev_commit=event.previous_commit_hash[:8] if event.previous_commit_hash else "",
            source=event.source.value if hasattr(event, "source") else "",
        )

        if event.event_type == EventType.BRANCH_SWITCH:
            task = WorkflowTask(
                task_type="branch_switch",
                project_id=event.project_id,
                repo_key=repo_key,
                kwargs={
                    "branch_name": event.branch_name,
                    "commit_hash": event.commit_hash,
                    "previous_branch": event.previous_branch_name,
                    "previous_commit": event.previous_commit_hash,
                },
            )
        elif event.event_type == EventType.CONTENT_COMMIT:
            task = WorkflowTask(
                task_type="content_commit",
                project_id=event.project_id,
                repo_key=repo_key,
                kwargs={
                    "branch_name": event.branch_name,
                    "commit_hash": event.commit_hash,
                    "previous_commit": event.previous_commit_hash,
                },
            )
        else:
            logger.warning("workflow_unknown_event_type", event_type=event.event_type)
            return

        self._task_queue.enqueue(task)

    def _resolve_repo_key_from_event(self, event: GitEvent) -> Optional[str]:
        """Map an event to the registered repository copy it originated from."""
        if event.git_repo_path:
            repo_key = normalize_repo_key(event.git_repo_path)
            if repo_key in self._project_repos:
                return repo_key
            return None
        # Legacy events without a repo path: route only when the project
        # has exactly one registered copy.
        matches = [
            repo_key for repo_key, pid in self._project_repos.items()
            if pid == event.project_id
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def enqueue_initialization(
        self,
        project_id: str,
        repo_path: str,
        branch_name: str,
        commit_hash: str,
    ) -> None:
        """Enqueue a full initialization task for one repository copy."""
        task = WorkflowTask(
            task_type="initialization",
            project_id=project_id,
            repo_key=normalize_repo_key(repo_path),
            kwargs={"branch_name": branch_name, "commit_hash": commit_hash},
        )
        self._task_queue.enqueue(task)

    def enqueue_content_commit(
        self,
        project_id: str,
        repo_path: str,
        branch_name: str,
        commit_hash: str,
        previous_commit: str,
    ) -> None:
        """Enqueue a content commit task for one repository copy."""
        task = WorkflowTask(
            task_type="content_commit",
            project_id=project_id,
            repo_key=normalize_repo_key(repo_path),
            kwargs={
                "branch_name": branch_name,
                "commit_hash": commit_hash,
                "previous_commit": previous_commit,
            },
        )
        self._task_queue.enqueue(task)

    # =========================================================================
    # 2.2 Branch switch workflow
    # =========================================================================

    async def workflow_branch_switch(
        self,
        project_id: str,
        repo_key: str,
        branch_name: str,
        commit_hash: str,
        previous_branch: Optional[str],
        previous_commit: Optional[str],
    ) -> BranchSwitchResult:
        """Execute branch switch — update the copy's in-memory instance."""
        start_ms = time.monotonic() * 1000
        repo_path = repo_key

        logger.info(
            "workflow_branch_switch_start",
            project_id=project_id,
            repo_key=repo_key,
            branch=branch_name,
            commit=commit_hash[:8],
        )

        # Step 1: Check cache
        cached = await self._memory.get_cached_instance(
            repo_key, branch_name, commit_hash,
        )
        if cached is not None:
            await self._memory.set_active_instance(
                repo_key, branch_name, commit_hash,
            )
            duration = int(time.monotonic() * 1000 - start_ms)
            logger.info(
                "workflow_branch_switch_cache_hit",
                project_id=project_id,
                duration_ms=duration,
            )
            return BranchSwitchResult(
                cache_hit=True, diff_added=0, diff_removed=0,
                diff_unchanged=0, duration_ms=duration,
            )

        # Step 2: Get new branch file list
        file_tree = await self._git_ops.get_file_tree(repo_path, branch_name)
        new_versions: Set[str] = {e.file_version_id for e in file_tree}

        # Step 3: Get current in-memory file_version_ids (if active instance exists)
        mem_versions: Set[str] = set()
        active_info = await self._memory.get_active_instance_info(repo_key)
        if active_info is not None:
            active_handle = await self._memory.get_cached_instance(
                repo_key, active_info.branch_name, active_info.commit_hash,
            )
            if active_handle is not None:
                mem_versions = await self._memory.get_all_file_version_ids(active_handle)

        # Step 4: Compute diff
        to_add = new_versions - mem_versions
        to_remove = mem_versions - new_versions
        unchanged = new_versions & mem_versions

        logger.info(
            "workflow_branch_switch_diff",
            project_id=project_id,
            add=len(to_add),
            remove=len(to_remove),
            unchanged=len(unchanged),
        )

        # Step 5: Create a staging in-memory instance and keep the old active
        # searchable until the new instance has fully loaded.
        handle = await self._memory.create_instance(
            repo_key, branch_name, commit_hash,
            project_id=project_id, staging=True,
        )

        try:
            # Step 6: Bulk copy unchanged + fetch to_add from Qdrant Server
            all_needed = list(unchanged | to_add)
            logger.info(
                "workflow_branch_switch_loading",
                project_id=project_id,
                files_to_load=len(all_needed),
            )
            files_loaded, points_loaded = await self._batch_load_qdrant_to_memory(
                project_id=project_id,
                file_version_ids=all_needed,
                handle=handle,
                log_event="branch_switch_batch_load",
            )

            # Step 7: Promote only after the staged load succeeds.
            await self._memory.set_active_handle(handle)
            await self._memory.destroy_inactive_instances(repo_key)
        except Exception:
            await self._memory.destroy_instance(handle)
            logger.exception(
                "workflow_branch_switch_staging_failed",
                project_id=project_id,
                branch=branch_name,
                commit=commit_hash[:8],
            )
            raise

        duration = int(time.monotonic() * 1000 - start_ms)
        logger.info(
            "workflow_branch_switch_complete",
            project_id=project_id,
            files_loaded=files_loaded,
            points_loaded=points_loaded,
            added=len(to_add),
            removed=len(to_remove),
            unchanged=len(unchanged),
            duration_ms=duration,
        )

        return BranchSwitchResult(
            cache_hit=False,
            diff_added=len(to_add),
            diff_removed=len(to_remove),
            diff_unchanged=len(unchanged),
            duration_ms=duration,
        )

    # =========================================================================
    # 2.2b Lazy rebuild — reconstruct in-memory instance from Qdrant Server
    # =========================================================================

    async def rebuild_memory_instance(self, repo_key: str) -> None:
        """Rebuild an in-memory instance from Qdrant Server for one repo copy.

        Called by MemoryQdrantService's rebuild handler when a search request
        arrives for a copy that has no active instance (e.g. after idle
        eviction).  The repo_key identifies the local copy; its project_id
        (Qdrant partition) is resolved from the registry.

        Flow:
          1. Resolve project_id from the registered repository copy
          2. Read current HEAD (branch + commit) from the copy
          3. Get file tree → compute file_version_ids
          4. Create in-memory instance
          5. Batch load Points from Qdrant Server
          6. Set as active instance
        """
        project_id = self._project_repos.get(repo_key)
        if not project_id:
            raise WorkflowError(
                f"Cannot rebuild: repository copy '{repo_key}' is not registered "
                "in workflow."
            )
        repo_path = repo_key

        start_ms = time.monotonic() * 1000

        # Step 1: Get current HEAD
        head = await self._git_ops.get_head_info(repo_path)
        branch_name = head.branch_name or "HEAD"
        commit_hash = head.commit_hash

        logger.info(
            "rebuild_memory_start",
            project_id=project_id,
            repo_key=repo_key,
            branch=branch_name,
            commit=commit_hash[:8],
        )

        # Step 2: Check if already cached (race condition guard)
        cached = await self._memory.get_cached_instance(
            repo_key, branch_name, commit_hash,
        )
        if cached is not None:
            await self._memory.set_active_instance(
                repo_key, branch_name, commit_hash,
            )
            logger.info(
                "rebuild_memory_cache_hit",
                project_id=project_id,
                repo_key=repo_key,
            )
            return

        # Step 3: Get file tree for current branch
        file_tree = await self._git_ops.get_file_tree(repo_path, branch_name)
        all_fvids = [e.file_version_id for e in file_tree]

        # Step 4: Create a staging instance so an existing active instance is
        # preserved until the replacement is fully loaded.
        handle = await self._memory.create_instance(
            repo_key, branch_name, commit_hash,
            project_id=project_id, staging=True,
        )

        try:
            # Step 5: Batch load from Qdrant Server
            files_loaded, points_loaded = await self._batch_load_qdrant_to_memory(
                project_id=project_id,
                file_version_ids=all_fvids,
                handle=handle,
                log_event="rebuild_memory_batch_load",
            )

            # Step 6: Promote the staged instance after successful load.
            await self._memory.set_active_handle(handle)
            await self._memory.destroy_inactive_instances(repo_key)
        except Exception:
            await self._memory.destroy_instance(handle)
            logger.exception(
                "rebuild_memory_staging_failed",
                project_id=project_id,
                repo_key=repo_key,
                branch=branch_name,
                commit=commit_hash[:8],
            )
            raise

        duration = int(time.monotonic() * 1000 - start_ms)
        logger.info(
            "rebuild_memory_complete",
            project_id=project_id,
            repo_key=repo_key,
            branch=branch_name,
            files_loaded=files_loaded,
            points_loaded=points_loaded,
            duration_ms=duration,
        )

    # =========================================================================
    # 2.3 Content commit workflow (knowledge production)
    # =========================================================================

    async def workflow_content_commit(
        self,
        project_id: str,
        repo_key: str,
        branch_name: str,
        commit_hash: str,
        previous_commit: str,
    ) -> ContentCommitResult:
        """Execute knowledge production for a content commit on one copy."""
        start = time.monotonic()
        repo_path = repo_key

        logger.info(
            "workflow_content_commit_start",
            project_id=project_id,
            repo_key=repo_key,
            branch=branch_name,
            commit=commit_hash[:8],
            prev_commit=previous_commit[:8],
        )

        # Step 1: Get changed files (filtered by global exclude_patterns + .git/codebase-rag/ignore)
        result = await self._git_ops.get_changed_files(
            repo_path, previous_commit, commit_hash,
        )

        all_added = list(result.changes.added)
        all_modified = list(result.changes.modified)
        all_deleted = list(result.changes.deleted)
        all_renamed = list(result.changes.renamed)

        logger.info(
            "workflow_content_commit_diff",
            project_id=project_id,
            added_files=len(all_added),
            modified_files=len(all_modified),
            deleted_files=len(all_deleted),
            renamed_files=len(all_renamed),
            excluded=result.excluded_count,
        )

        added_count = 0
        modified_count = 0
        deleted_count = 0
        renamed_count = 0
        skipped_count = 0
        sync_cache_hit_count = 0
        total_points = 0
        total_agent_calls = 0
        failed_files: List[FailedFile] = []

        file_sem = asyncio.Semaphore(self._config.content_commit.max_file_concurrency)

        # Step 2A: Process added files
        async def _process_added(entry) -> None:
            nonlocal added_count, skipped_count, sync_cache_hit_count, total_points, total_agent_calls
            async with file_sem:
                try:
                    pts, skipped, agents, cache_hit = await self._process_file_with_agent(
                        project_id, repo_path, entry.file_path,
                        entry.blob_hash, entry.file_version_id,
                        is_new=True, repo_key=repo_key,
                    )
                    total_points += pts
                    total_agent_calls += agents
                    if skipped:
                        skipped_count += 1
                    elif cache_hit:
                        sync_cache_hit_count += 1
                    else:
                        added_count += 1
                except Exception as e:
                    failed_files.append(FailedFile(entry.file_path, str(e)))
                    self._enqueue_retry(FailedFileRecord(
                        project_id=project_id,
                        file_path=entry.file_path,
                        blob_hash=entry.blob_hash,
                        file_version_id=entry.file_version_id,
                        is_new=True,
                        last_error=str(e)[:500],
                        created_at=time.time(),
                    ))
                    logger.warning(
                        "workflow_file_failed",
                        file_path=entry.file_path,
                        error=str(e),
                    )

        # Step 2M: Process modified files
        async def _process_modified(entry) -> None:
            nonlocal modified_count, skipped_count, sync_cache_hit_count, total_points, total_agent_calls
            async with file_sem:
                try:
                    # Get old descriptions for context only when auto_generate is enabled
                    old_descriptions: List[str] = []
                    if self._config.content_commit.auto_generate:
                        old_records = await self._qdrant.get_file_points(
                            project_id, entry.old_file_version_id,
                        )
                        old_descriptions = self._extract_descriptions_list(old_records)

                    pts, skipped, agents, cache_hit = await self._process_file_with_agent(
                        project_id, repo_path, entry.file_path,
                        entry.new_blob_hash, entry.new_file_version_id,
                        is_new=False,
                        old_descriptions=old_descriptions,
                        old_file_version_id=entry.old_file_version_id,
                        repo_key=repo_key,
                    )
                    total_points += pts
                    total_agent_calls += agents
                    if skipped:
                        skipped_count += 1
                    elif cache_hit:
                        sync_cache_hit_count += 1
                    else:
                        modified_count += 1
                except Exception as e:
                    failed_files.append(FailedFile(entry.file_path, str(e)))
                    self._enqueue_retry(FailedFileRecord(
                        project_id=project_id,
                        file_path=entry.file_path,
                        blob_hash=entry.new_blob_hash,
                        file_version_id=entry.new_file_version_id,
                        is_new=False,
                        old_file_version_id=entry.old_file_version_id,
                        last_error=str(e)[:500],
                        created_at=time.time(),
                    ))
                    logger.warning(
                        "workflow_file_failed",
                        file_path=entry.file_path,
                        error=str(e),
                    )

        # Step 2D: Process deleted files
        async def _process_deleted(entry) -> None:
            nonlocal deleted_count
            try:
                await self._delete_from_memory(project_id, repo_key, entry.file_version_id)
                deleted_count += 1
            except Exception as e:
                failed_files.append(FailedFile(entry.file_path, str(e)))

        # Step 2R: Process renamed files (clone points, no LLM / embedding)
        async def _process_renamed(entry: RenamedFileEntry) -> None:
            nonlocal renamed_count, total_points
            try:
                if self._knowledge_sync is None:
                    raise WorkflowError("KnowledgeSyncService is required for rename publishing.")

                source_records = await self._qdrant.get_file_points(
                    project_id,
                    entry.old_file_version_id,
                )
                if (
                    not source_records
                    and self._knowledge_sync.has_ready_job(
                        project_id, entry.old_file_version_id,
                    )
                ):
                    await self._knowledge_sync.ensure_published(
                        project_id,
                        entry.old_file_version_id,
                        update_memory=False,
                    )
                    source_records = await self._qdrant.get_file_points(
                        project_id,
                        entry.old_file_version_id,
                    )

                cloned_points = self._records_to_renamed_point_data(
                    source_records,
                    entry.new_file_path,
                    entry.blob_hash,
                )
                if cloned_points:
                    sync_result = await self._knowledge_sync.submit_points(
                        project_id=project_id,
                        file_path=entry.new_file_path,
                        blob_hash=entry.blob_hash,
                        file_version_id=entry.new_file_version_id,
                        points=cloned_points,
                        source="rename_clone",
                        old_file_version_id=entry.old_file_version_id,
                        update_latest=True,
                        update_memory=True,
                        repo_key=repo_key,
                    )
                    if sync_result.cancelled_stale or sync_result.skipped_existing:
                        return
                    total_points += sync_result.points_count
                    renamed_count += 1
                    logger.info(
                        "workflow_file_renamed",
                        old_path=entry.old_file_path,
                        new_path=entry.new_file_path,
                        cloned_points=sync_result.points_count,
                    )
                else:
                    logger.warning(
                        "workflow_rename_no_source_fallback",
                        old_path=entry.old_file_path,
                        new_path=entry.new_file_path,
                        blob_hash=entry.blob_hash[:16],
                    )
                    failed_files.append(FailedFile(
                        entry.new_file_path,
                        "rename clone failed: no source points found",
                    ))
            except Exception as e:
                failed_files.append(FailedFile(entry.new_file_path, str(e)))
                logger.warning(
                    "workflow_rename_failed",
                    old_path=entry.old_file_path,
                    new_path=entry.new_file_path,
                    error=str(e),
                )

        # Run all paths concurrently
        tasks = []
        for entry in all_added:
            tasks.append(_process_added(entry))
        for entry in all_modified:
            tasks.append(_process_modified(entry))
        for entry in all_deleted:
            tasks.append(_process_deleted(entry))
        for entry in all_renamed:
            tasks.append(_process_renamed(entry))

        if tasks:
            await asyncio.gather(*tasks)

        # Step 3: Update cache key
        await self._memory.update_cache_commit(
            repo_key, branch_name, previous_commit, commit_hash,
        )

        duration = time.monotonic() - start

        logger.info(
            "workflow_content_commit_complete",
            project_id=project_id,
            agent_generated_added_files=added_count,
            agent_generated_modified_files=modified_count,
            deleted=deleted_count,
            renamed=renamed_count,
            content_dedup_skipped_files=skipped_count,
            sync_cache_hit_files=sync_cache_hit_count,
            qdrant_points_written=total_points,
            agent_calls=total_agent_calls,
            failed=len(failed_files),
            duration_s=round(duration, 2),
        )

        return ContentCommitResult(
            added_files=added_count,
            modified_files=modified_count,
            deleted_files=deleted_count,
            renamed_files=renamed_count,
            skipped_files=skipped_count,
            cache_hit_files=sync_cache_hit_count,
            failed_files=failed_files,
            total_points_written=total_points,
            total_agent_calls=total_agent_calls,
            duration_seconds=round(duration, 2),
        )

    # =========================================================================
    # 2.4 Full initialization workflow
    # =========================================================================

    async def workflow_full_initialization(
        self,
        project_id: str,
        repo_key: str,
        branch_name: str,
        commit_hash: str,
    ) -> InitializationResult:
        """Full knowledge production for all files in a branch of one copy.

        When ``initialization.mode`` is ``"incremental"``, skip the full scan
        and create an empty in-memory instance directly so that subsequent
        commits will build up the knowledge base incrementally.
        """
        # --- incremental mode: empty start ---
        if self._config.initialization.mode == "incremental":
            return await self._incremental_initialization(
                project_id, repo_key, branch_name, commit_hash,
            )

        start = time.monotonic()
        repo_path = repo_key

        logger.info(
            "workflow_initialization_start",
            project_id=project_id,
            repo_key=repo_key,
            branch=branch_name,
            commit=commit_hash[:8],
        )

        # Step 1: Get all files
        file_tree = await self._git_ops.get_file_tree(repo_path, branch_name)
        all_fvids = [e.file_version_id for e in file_tree]

        # Step 2: Check which already exist in Qdrant
        existence = await self._qdrant.check_existence(project_id, all_fvids)

        processed = 0
        skipped = 0
        total_points = 0
        failed_files: List[FailedFile] = []
        progress = Progress(total_files=len(file_tree))

        self._current_tasks[repo_key] = TaskInfo(
            task_type="initialization",
            project_id=project_id,
            progress=progress,
        )

        file_sem = asyncio.Semaphore(
            self._config.initialization.max_file_concurrency,
        )
        processed_fvids: List[str] = []
        progress_lock = asyncio.Lock()

        # Pre-filter: skip files already in Qdrant (content-addressing dedup)
        pending_entries = []
        for entry in file_tree:
            fvid = entry.file_version_id
            if existence.get(fvid, False):
                skipped += 1
                processed_fvids.append(fvid)
                continue
            pending_entries.append(entry)

        async def _process_init_file(entry) -> None:
            nonlocal processed, skipped, total_points
            fvid = entry.file_version_id
            async with file_sem:
                progress.current_file = entry.file_path
                try:
                    pts, was_skipped, _, _ = await self._process_file_with_agent(
                        project_id, repo_path, entry.file_path,
                        entry.blob_hash, fvid, is_new=True,
                        sync_source="initialization_backfill",
                        repo_key=repo_key,
                    )
                    async with progress_lock:
                        total_points += pts
                        if was_skipped:
                            skipped += 1
                        else:
                            processed += 1
                        processed_fvids.append(fvid)
                except Exception as e:
                    async with progress_lock:
                        failed_files.append(FailedFile(entry.file_path, str(e)))
                    self._enqueue_retry(FailedFileRecord(
                        project_id=project_id,
                        file_path=entry.file_path,
                        blob_hash=entry.blob_hash,
                        file_version_id=fvid,
                        is_new=True,
                        last_error=str(e)[:500],
                        created_at=time.time(),
                    ))
                    logger.warning(
                        "workflow_init_file_failed",
                        file_path=entry.file_path,
                        error=str(e),
                    )

                async with progress_lock:
                    progress.processed_files = processed + skipped
                    progress.failed_files = len(failed_files)

        if pending_entries:
            await asyncio.gather(*[_process_init_file(e) for e in pending_entries])

        # Step 5: Build a staging in-memory instance, then promote only after
        # all readable persistent points have been loaded successfully.
        handle = await self._memory.create_instance(
            repo_key, branch_name, commit_hash,
            project_id=project_id, staging=True,
        )
        try:
            files_loaded, memory_points_loaded = await self._batch_load_qdrant_to_memory(
                project_id=project_id,
                file_version_ids=processed_fvids,
                handle=handle,
                log_event="workflow_init_memory_batch_load",
            )

            # Step 6: Set as active
            await self._memory.set_active_handle(handle)
            await self._memory.destroy_inactive_instances(repo_key)
        except Exception:
            await self._memory.destroy_instance(handle)
            logger.exception(
                "workflow_init_memory_staging_failed",
                project_id=project_id,
                branch=branch_name,
                commit=commit_hash[:8],
            )
            raise

        self._current_tasks.pop(repo_key, None)

        duration = time.monotonic() - start

        logger.info(
            "workflow_initialization_complete",
            project_id=project_id,
            total=len(file_tree),
            processed=processed,
            skipped=skipped,
            failed=len(failed_files),
            memory_files_loaded=files_loaded,
            memory_points_loaded=memory_points_loaded,
            duration_s=round(duration, 2),
        )

        return InitializationResult(
            total_files=len(file_tree),
            processed_files=processed,
            skipped_files=skipped,
            failed_files=failed_files,
            total_points=total_points,
            duration_seconds=round(duration, 2),
        )

    async def _incremental_initialization(
        self,
        project_id: str,
        repo_key: str,
        branch_name: str,
        commit_hash: str,
    ) -> InitializationResult:
        """Incremental mode: load existing knowledge from persistent storage
        into an in-memory instance, but do NOT generate missing descriptions.

        The knowledge base grows as the user makes commits.
        """
        start = time.monotonic()
        repo_path = repo_key

        logger.info(
            "workflow_incremental_initialization_start",
            project_id=project_id,
            repo_key=repo_key,
            branch=branch_name,
            commit=commit_hash[:8],
        )

        # Step 1: Get current file tree
        file_tree = await self._git_ops.get_file_tree(repo_path, branch_name)
        all_fvids = [e.file_version_id for e in file_tree]

        # Step 2: Check which already exist in persistent storage
        existence = await self._qdrant.check_existence(project_id, all_fvids)
        existing_fvids = [fvid for fvid, exists in existence.items() if exists]

        logger.info(
            "workflow_incremental_existing_check",
            project_id=project_id,
            total_files=len(file_tree),
            existing_in_storage=len(existing_fvids),
        )

        # Step 3: Create a staging in-memory instance. Do not replace any
        # existing active instance until all persisted points are loaded.
        handle = await self._memory.create_instance(
            repo_key, branch_name, commit_hash,
            project_id=project_id, staging=True,
        )

        try:
            # Step 4: Load existing points from persistent storage into memory
            files_loaded, loaded_count = await self._batch_load_qdrant_to_memory(
                project_id=project_id,
                file_version_ids=existing_fvids,
                handle=handle,
                log_event="workflow_incremental_memory_batch_load",
            )

            # Step 5: Set as active only after the staged load succeeds.
            await self._memory.set_active_handle(handle)
            await self._memory.destroy_inactive_instances(repo_key)
        except Exception:
            await self._memory.destroy_instance(handle)
            logger.exception(
                "workflow_incremental_memory_staging_failed",
                project_id=project_id,
                repo_key=repo_key,
                branch=branch_name,
                commit=commit_hash[:8],
            )
            raise

        duration = time.monotonic() - start

        logger.info(
            "workflow_incremental_initialization_complete",
            project_id=project_id,
            existing_files_available=len(existing_fvids),
            existing_files_loaded=files_loaded,
            points_loaded=loaded_count,
            duration_s=round(duration, 2),
        )

        return InitializationResult(
            total_files=0,
            processed_files=0,
            skipped_files=0,
            failed_files=[],
            total_points=0,
            duration_seconds=round(duration, 2),
        )

    # =========================================================================
    # 2.5 Workflow status query
    # =========================================================================

    def get_workflow_status(self, repo_key: str) -> WorkflowStatus:
        """Get current workflow status for a repository copy."""
        return WorkflowStatus(
            current_task=self._current_tasks.get(repo_key),
            queued_tasks=self._task_queue.get_queue_depth(repo_key),
            last_completed=self._last_completed.get(repo_key),
        )

    # =========================================================================
    # Internal: single-agent knowledge production
    # =========================================================================

    async def _process_file_with_agent(
        self,
        project_id: str,
        repo_path: str,
        file_path: str,
        blob_hash: str,
        file_version_id: str,
        is_new: bool,
        old_descriptions: Optional[List[str]] = None,
        old_file_version_id: Optional[str] = None,
        skip_memory_update: bool = False,
        sync_source: KnowledgeSyncSource = "commit_event",
        update_latest: bool = True,
        repo_key: Optional[str] = None,
    ) -> tuple:
        """Process a single file via knowledge_producer agent with tool calling.

        Args:
            skip_memory_update: If True, skip in-memory Qdrant update (used by
                compensation task which only writes to persistent storage).
            repo_key: Repository copy whose in-memory instance receives the
                update; when None the memory refresh is skipped.

        Returns (points_written, skipped, agent_calls, cache_hit).
        """
        if self._knowledge_sync is None:
            raise WorkflowError("KnowledgeSyncService is required for knowledge publishing.")

        if update_latest:
            self._knowledge_sync.mark_latest_file_version(
                project_id, file_path, file_version_id,
            )
        elif self._knowledge_sync.is_stale_file_version(
            project_id, file_path, file_version_id,
        ):
            logger.info(
                "workflow_file_stale_skip",
                file_path=file_path,
                file_version_id=file_version_id[:16],
            )
            return (0, True, 0, False)

        # Content-addressing dedup
        existence = await self._qdrant.check_existence(project_id, [file_version_id])
        if existence.get(file_version_id, False):
            logger.debug(
                "workflow_file_dedup_skip",
                file_path=file_path,
                file_version_id=file_version_id[:16],
            )
            if not skip_memory_update:
                await self._upsert_to_memory_from_qdrant(
                    project_id, repo_key, file_version_id,
                )
                if old_file_version_id:
                    await self._delete_from_memory(
                        project_id, repo_key, old_file_version_id,
                    )
            return (0, True, 0, False)

        if self._knowledge_sync.has_ready_job(project_id, file_version_id):
            sync_result = await self._knowledge_sync.ensure_published(
                project_id,
                file_version_id,
                update_memory=not skip_memory_update,
                repo_key=repo_key,
            )
            if sync_result.cancelled_stale or sync_result.skipped_existing:
                return (0, True, 0, True)
            return (sync_result.points_count, False, 0, True)

        # Check ignore rules (.git/codebase-rag/ignore + global exclude_patterns)
        if hasattr(self._git_ops, "is_file_ignored") and self._git_ops.is_file_ignored(repo_path, file_path):
            logger.info(
                "workflow_file_ignored_skip",
                project_id=project_id,
                file_path=file_path,
                file_version_id=file_version_id[:16] if file_version_id else "",
            )
            return (0, True, 0, False)

        # Check if auto_generate is enabled for workflow commit
        if sync_source in ("commit_event", "compensation_retry") and not self._config.content_commit.auto_generate:
            logger.info(
                "workflow_file_auto_generate_disabled_skip",
                project_id=project_id,
                file_path=file_path,
                file_version_id=file_version_id[:16],
                sync_source=sync_source,
            )
            if not skip_memory_update and old_file_version_id:
                await self._delete_from_memory(
                    project_id, repo_key, old_file_version_id,
                )
            return (0, True, 0, False)

        # Detect file modality (text vs supported image .png/.jpg/.jpeg)
        raw_bytes = await self._git_ops.get_file_bytes(
            repo_path, blob_hash, file_path=file_path,
        )
        modality, mime_type = self._git_ops.detect_file_modality(file_path, raw_bytes)
        if modality == "unsupported_binary":
            logger.info(
                "workflow_file_unsupported_binary_skip",
                file_path=file_path,
                file_version_id=file_version_id[:16],
            )
            return (0, True, 0, False)

        file_type: FileType = "image" if modality == "image" else "text"
        user_message: Union[str, List[Dict[str, Any]]]

        if file_type == "image":
            assert mime_type is not None
            b64_img = base64.b64encode(raw_bytes).decode("ascii")
            if is_new:
                prompt_text = (
                    "请根据下面新建的图片文件信息和视觉内容，调用创建知识点tool为该图片文件创建业务描述知识点。\n\n"
                    f"文件路径：{file_path}\n"
                    f"文件类型：图片（{mime_type}，大小 {len(raw_bytes)} 字节）\n\n"
                    "请仔细观察图片中的视觉信息（如架构图、流程图、UI界面、原型图、图标、图表、文字标注等），"
                    "按照业务描述模板详细描述该图片在项目中的技术定位、业务域、核心视觉内容与业务用途。"
                )
            else:
                old_desc_text = "\n---\n".join(old_descriptions or [])
                prompt_text = (
                    "请根据下面修改后的图片文件信息和最新视觉内容，调用创建知识点tool为该图片文件创建业务描述知识点。\n\n"
                    f"文件路径：{file_path}\n"
                    f"文件类型：图片（{mime_type}，大小 {len(raw_bytes)} 字节）\n\n"
                    f"图片修改前的业务描述知识点的描述内容：\n{old_desc_text}\n\n"
                    "注意是给出最新完整的业务描述，不是只描述修改内容，修改前的业务描述只是辅助你理解业务。"
                )
            user_message = [
                {"type": "text", "text": prompt_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64_img}"},
                },
            ]
        else:
            source_code = raw_bytes.decode("utf-8", errors="replace")
            if is_new:
                user_message = (
                    "请根据下面新建的源码文件信息和内容，调用创建知识点tool为该文件创建业务描述知识点。\n\n"
                    f"文件路径：{file_path}\n\n"
                    f"文件内容：\n```\n{source_code}\n```"
                )
            else:
                old_desc_text = "\n---\n".join(old_descriptions or [])
                user_message = (
                    "请根据下面修改后的源码文件信息和内容，调用创建知识点tool为该文件创建业务描述知识点。\n\n"
                    f"文件路径：{file_path}\n\n"
                    f"修改后的完整内容：\n```\n{source_code}\n```\n\n"
                    f"文件修改前的业务描述知识点的描述内容：\n{old_desc_text}\n\n"
                    "注意是给出最新完整的业务描述，不是只描述修改内容，修改前的业务描述只是辅助你理解业务。"
                )

        # Mutable state captured by tool handler closures
        points_written: List[PointData] = []
        token_limit = self._config.content_commit.point_token_limit
        image_token_overhead = IMAGE_TOKEN_COST if file_type == "image" else 0

        # ----- Tool handler: create_knowledge_points -----
        async def _handle_create_knowledge_points(points: list, **_kwargs) -> str:
            # Validate token limits for ALL points, collect all violations
            violations = []
            for idx, p in enumerate(points):
                desc = p.get("business_description", "")
                text_tokens = await self._token_counter.count(desc)
                total_tokens = text_tokens + image_token_overhead
                if total_tokens > token_limit:
                    if image_token_overhead > 0:
                        violations.append(
                            f"第{idx + 1}个知识点超过{token_limit}tokens上限"
                            f"（文本{text_tokens}tokens + 图片{image_token_overhead}tokens = {total_tokens}tokens）"
                        )
                    else:
                        violations.append(
                            f"第{idx + 1}个知识点超过{token_limit}tokens上限"
                            f"（实际{total_tokens}tokens）"
                        )
            if violations:
                detail = "；".join(violations)
                return (
                    f"执行失败，{detail}，"
                    f"请按业务垂直划分分片，重新生成。"
                )

            # All passed — embed descriptions (with image if file_type == "image")
            descriptions = [p["business_description"] for p in points]
            if file_type == "image" and mime_type:
                embed_inputs: List[EmbedInputItem] = [
                    MultimodalEmbedInput(
                        text=desc,
                        image_bytes=raw_bytes,
                        mime_type=mime_type,
                    )
                    for desc in descriptions
                ]
            else:
                embed_inputs = list(descriptions)

            logger.debug(
                "workflow_embedding_call",
                file_path=file_path,
                file_type=file_type,
                descriptions_count=len(descriptions),
            )
            embeddings = await self._embedding.embed(embed_inputs)

            # Build PointData with non-LLM fields filled in
            point_data_list: List[PointData] = []
            entity_name = os.path.basename(file_path)
            for i, (p, emb) in enumerate(zip(points, embeddings)):
                point_data_list.append(PointData(
                    file_path=file_path,
                    entity_type="file",
                    entity_name=entity_name,
                    business_description=p["business_description"],
                    blob_hash=blob_hash,
                    dense_vector=emb.dense_vector,
                    file_type=file_type,
                ))

            # Accumulate in memory — actual Qdrant write happens after agent completes
            points_written.extend(point_data_list)

            return f"成功创建{len(point_data_list)}个知识点。"

        # ----- Tool handler: end_task -----
        async def _handle_end_task(signal: str = "exit", **_kwargs) -> str:
            raise AgentTaskComplete()

        tool_handlers = {
            "create_knowledge_points": _handle_create_knowledge_points,
            "end_task": _handle_end_task,
        }

        # Run agent with provider fallback
        logger.info(
            "workflow_agent_call_start",
            project_id=project_id,
            file_path=file_path,
            is_new=is_new,
            file_version_id=file_version_id[:16],
        )

        # 容量感知的服务商选择 + 故障降级 fallback
        # 1. select_provider 按优先级选择有并发容量的服务商（全满时阻塞等待）
        # 2. 尝试该服务商下所有 agent（同一服务商可能有多个模型）
        # 3. 该服务商所有 agent 都失败 → 从候选列表移除，重新 select_provider
        last_error: Optional[Exception] = None
        remaining_providers = list(self._kp_provider_order)
        tried_agents: set = set()

        while remaining_providers:
            selected = await self._agent.select_provider(remaining_providers)
            agent_candidates = self._kp_provider_agents.get(selected, [])

            success = False
            for agent_name in agent_candidates:
                if agent_name in tried_agents:
                    continue
                tried_agents.add(agent_name)

                try:
                    await self._agent.run_agent(
                        agent_name,
                        {"point_token_limit": str(token_limit)},
                        tool_handlers=tool_handlers,
                        user_message=user_message,
                    )
                    success = True
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "kp_provider_failed",
                        agent_name=agent_name,
                        file_path=file_path,
                        error=str(e)[:200],
                    )
                    points_written.clear()

            if success:
                break

            # 该服务商所有 agent 都失败，从候选列表移除后重试其他服务商
            remaining_providers.remove(selected)
        else:
            raise WorkflowError(
                f"All knowledge_producer providers exhausted for {file_path}. "
                f"Last error: {last_error}"
            )

        logger.info(
            "workflow_agent_call_done",
            project_id=project_id,
            file_path=file_path,
            points_written=len(points_written),
        )

        # Persist all accumulated points through the unified sync coordinator.
        if points_written:
            sync_result = await self._knowledge_sync.submit_points(
                project_id=project_id,
                file_path=file_path,
                blob_hash=blob_hash,
                file_version_id=file_version_id,
                points=points_written,
                source=sync_source,
                old_file_version_id=old_file_version_id,
                update_latest=update_latest,
                update_memory=not skip_memory_update,
                repo_key=repo_key,
            )
            if sync_result.cancelled_stale or sync_result.skipped_existing:
                return (0, True, 1, False)

        return (len(points_written), False, 1, False)

    # =========================================================================
    # Internal: memory helpers
    # =========================================================================

    async def _batch_load_qdrant_to_memory(
        self,
        *,
        project_id: str,
        file_version_ids: List[str],
        handle: str,
        log_event: str,
    ) -> tuple[int, int]:
        """Batch-load readable persisted Qdrant records into a memory instance.

        ``QdrantStorageService.get_files_points`` preserves the per-file complete
        knowledge-set/readability contract, so this helper never treats arbitrary
        matching payloads as sufficient for memory reload.
        """
        unique_fvids = list(dict.fromkeys(file_version_ids))
        if not unique_fvids:
            return 0, 0

        grouped = await self._qdrant.get_files_points(project_id, unique_fvids)
        loaded_files = 0
        loaded_points = 0
        for fvid in unique_fvids:
            records = grouped.get(fvid, [])
            if not records:
                continue
            points = self._records_to_point_structs(records)
            if not points:
                continue
            await self._memory.upsert_points(handle, points)
            loaded_files += 1
            loaded_points += len(points)

        logger.info(
            log_event,
            project_id=project_id,
            files_requested=len(unique_fvids),
            files_loaded=loaded_files,
            points_loaded=loaded_points,
        )
        return loaded_files, loaded_points

    async def _upsert_to_memory_from_qdrant(
        self, project_id: str, repo_key: Optional[str], file_version_id: str,
    ) -> None:
        """Load points from Qdrant and upsert into a copy's active instance."""
        if repo_key is None:
            return
        active_info = await self._memory.get_active_instance_info(repo_key)
        if active_info is None:
            return

        handle = await self._memory.get_cached_instance(
            repo_key, active_info.branch_name, active_info.commit_hash,
        )
        if handle is None:
            return

        _, loaded_points = await self._batch_load_qdrant_to_memory(
            project_id=project_id,
            file_version_ids=[file_version_id],
            handle=handle,
            log_event="memory_upsert_from_qdrant",
        )
        if loaded_points:
            logger.debug(
                "memory_upsert_from_qdrant",
                project_id=project_id,
                repo_key=repo_key,
                file_version_id=file_version_id[:16],
                points=loaded_points,
            )

    async def _delete_from_memory(
        self, project_id: str, repo_key: Optional[str], file_version_id: str,
    ) -> None:
        """Delete points from a copy's active in-memory instance."""
        if repo_key is None:
            return
        active_info = await self._memory.get_active_instance_info(repo_key)
        if active_info is None:
            return

        handle = await self._memory.get_cached_instance(
            repo_key, active_info.branch_name, active_info.commit_hash,
        )
        if handle is None:
            return

        await self._memory.delete_points_by_file_version(handle, [file_version_id])
        logger.debug(
            "memory_delete_file_version",
            project_id=project_id,
            repo_key=repo_key,
            file_version_id=file_version_id[:16],
        )

    @staticmethod
    def _records_to_point_structs(records: list) -> List[models.PointStruct]:
        """Convert Qdrant Record objects to PointStruct for in-memory upsert."""
        points = []
        for rec in records:
            payload = rec.payload or {}
            vectors = {}

            if rec.vector:
                if isinstance(rec.vector, dict):
                    if "dense" in rec.vector:
                        vectors["dense"] = rec.vector["dense"]
                    else:
                        vectors = {
                            name: vec
                            for name, vec in rec.vector.items()
                            if isinstance(vec, list)
                        }
                elif isinstance(rec.vector, list):
                    vectors["dense"] = rec.vector

            points.append(models.PointStruct(
                id=rec.id,
                vector=vectors if vectors else rec.vector,
                payload=payload,
            ))

        if points:
            logger.debug(
                "records_to_point_structs",
                total=len(points),
            )
        return points

    @staticmethod
    def _extract_descriptions_list(records: list) -> List[str]:
        """Extract business descriptions from records."""
        if not records:
            return []
        return [
            (r.payload or {}).get("business_description", "")
            for r in records
            if (r.payload or {}).get("business_description")
        ]

    def _records_to_renamed_point_data(
        self,
        records: list,
        new_file_path: str,
        blob_hash: str,
    ) -> List[PointData]:
        dense_name = self._config_manager.config.qdrant_storage.collection.dense_vector.name
        entity_name = os.path.basename(new_file_path)
        points: List[PointData] = []

        for rec in records:
            payload = rec.payload or {}
            if isinstance(rec.vector, dict):
                dense_vector = rec.vector.get(dense_name)
            elif isinstance(rec.vector, list):
                dense_vector = rec.vector
            else:
                dense_vector = None
            description = payload.get("business_description")
            if not isinstance(description, str) or not description:
                continue
            if not isinstance(dense_vector, list):
                continue
            raw_ft = payload.get("file_type", "text")
            file_type: FileType = "image" if raw_ft == "image" else "text"
            points.append(PointData(
                file_path=new_file_path,
                entity_type=str(payload.get("entity_type") or "file"),
                entity_name=str(payload.get("entity_name") or entity_name),
                business_description=description,
                blob_hash=blob_hash,
                dense_vector=dense_vector,
                file_type=file_type,
            ))

        return points

    # =========================================================================
    # Internal: task execution dispatcher
    # =========================================================================

    async def _execute_task(self, task: WorkflowTask) -> None:
        """Execute a workflow task (called by ProjectTaskQueue)."""
        repo_key = task.repo_key or self._resolve_repo_key_for_project(
            task.project_id,
        )
        if not repo_key or repo_key not in self._project_repos:
            logger.warning(
                "workflow_task_repo_not_registered",
                project_id=task.project_id,
                repo_key=task.repo_key,
            )
            return

        project_id = self._project_repos.get(repo_key, task.project_id)

        task_info = TaskInfo(
            task_type=task.task_type,
            project_id=project_id,
        )
        self._current_tasks[repo_key] = task_info

        try:
            if task.task_type == "branch_switch":
                await self.workflow_branch_switch(
                    project_id=project_id,
                    repo_key=repo_key,
                    **task.kwargs,
                )
            elif task.task_type == "content_commit":
                await self.workflow_content_commit(
                    project_id=project_id,
                    repo_key=repo_key,
                    **task.kwargs,
                )
            elif task.task_type == "initialization":
                await self.workflow_full_initialization(
                    project_id=project_id,
                    repo_key=repo_key,
                    **task.kwargs,
                )
            else:
                logger.warning(
                    "workflow_unknown_task_type",
                    task_type=task.task_type,
                )
        finally:
            self._last_completed[repo_key] = task_info
            self._current_tasks.pop(repo_key, None)

    def _resolve_repo_key_for_project(self, project_id: str) -> str:
        """Resolve a legacy task's repo_key: valid only for single-copy projects."""
        matches = [
            repo_key for repo_key, pid in self._project_repos.items()
            if pid == project_id
        ]
        return matches[0] if len(matches) == 1 else ""

    # =========================================================================
    # Internal: agent registration
    # =========================================================================

    async def _register_agents(self) -> None:
        """Build agent definitions and register providers for knowledge_producer."""
        self._build_kp_defs()
        await self._init_kp_providers()

    # ----- knowledge_producer agent defs -----

    def _build_kp_defs(self) -> None:
        """Build and store knowledge_producer system prompt + tool schemas."""
        token_limit = self._config.content_commit.point_token_limit
        max_points = self._config.knowledge_producer.max_points_per_call

        self._kp_system_prompt = (
            "你是一个高级软件工程师，负责分析源码文件并生成高质量的业务描述知识点。\n\n"
            "## 角色职责\n"
            "根据最新的源码文件内容，给出详细且完整的业务描述知识点。\n\n"
            "**核心原则：尽最大能力描述出更多的业务逻辑细节！** 我们不是要一个简洁的概括描述！因为包含越多业务细节的描述构建的知识库越精准，"
            "一个高质量的业务描述知识库对软件项目的高质量迭代开发起到了决定性的影响。描述中一定要包含充分的业务关键词、详细的功能目的说明、业务实现说明，这是后续检索成功率的关键！\n\n"
            "## 业务描述 `business_description` 的格式规范\n\n"
            "每个知识点的业务描述必须遵循以下 **5 层结构化模板**，从宏观定位到微观细节，确保检索时能从多个维度命中：\n\n"
            "```\n"
            "## 项目结构域\n"
            "[说明该文件在项目中的技术定位]\n"
            "取值范围：前端源码 | 后端源码 | 配置文件 | 测试代码 | 构建脚本 | 数据库脚本 | 文档 | 其他\n"
            "\n"
            "## 业务域\n"
            "[说明该文件所属的业务模块]\n"
            "示例：用户管理模块、订单模块、支付模块、权限认证模块、消息通知模块等\n"
            "\n"
            "## 功能描述\n"
            "[一句话说明该文件/类实现的核心功能是什么]\n"
            "\n"
            "## 目的描述\n"
            "[说明为什么需要这个文件/类，它解决了什么问题，在系统中扮演什么角色]\n"
            "\n"
            "## 业务逻辑细节\n"
            "[详细描述具体的业务实现逻辑，这是最重要的部分，必须包含充分的业务关键词]\n"
            "\n"
            "### 类/文件定义\n"
            "[类的声明、继承关系、实现的接口等]\n"
            "\n"
            "### 核心方法/函数\n"
            "[逐个描述核心方法的业务逻辑、入参、出参、处理流程]\n"
            "\n"
            "### 辅助方法/函数\n"
            "[描述辅助方法的作用和调用场景]\n"
            "\n"
            "### 关键业务规则\n"
            "[描述代码中体现的业务规则、校验逻辑、边界条件等]\n"
            "```\n\n"
            "**示例：一个完整的业务描述**\n\n"
            "```\n"
            "## 项目结构域\n"
            "后端源码\n"
            "\n"
            "## 业务域\n"
            "用户管理模块 - 用户认证子模块\n"
            "\n"
            "## 功能描述\n"
            "实现用户登录认证功能，包括账号密码验证、JWT令牌生成和登录日志记录。\n"
            "\n"
            "## 目的描述\n"
            "作为系统的安全入口，确保只有合法用户能够访问系统资源。该类是认证流程的核心控制器，协调密码验证服务、令牌服务和日志服务完成完整的登录流程。\n"
            "\n"
            "## 业务逻辑细节\n"
            "\n"
            "### 类/文件定义\n"
            "LoginController 是一个 Spring REST 控制器，使用 @RestController 注解，映射路径为 /api/auth。依赖注入了 UserService、JwtTokenService、LoginLogService 三个服务。\n"
            "\n"
            "### 核心方法/函数\n"
            "1. **login(LoginRequest request)** - 用户登录方法\n"
            "   - 入参：LoginRequest 包含 username(用户名) 和 password(密码)\n"
            "   - 处理流程：\n"
            "     a. 调用 UserService.findByUsername() 查询用户\n"
            "     b. 使用 BCrypt 验证密码是否匹配\n"
            "     c. 检查用户状态是否为启用状态\n"
            "     d. 调用 JwtTokenService.generateToken() 生成访问令牌\n"
            "     e. 记录登录日志（IP、时间、设备信息）\n"
            "   - 出参：LoginResponse 包含 token、refreshToken、过期时间\n"
            "   - 异常处理：用户不存在抛出 UserNotFoundException，密码错误抛出 BadCredentialsException\n"
            "\n"
            "2. **logout(String token)** - 用户登出方法\n"
            "   - 将当前令牌加入黑名单，使其立即失效\n"
            "\n"
            "### 辅助方法/函数\n"
            "- **recordLoginLog()** - 私有方法，记录登录日志到数据库\n"
            "- **validateLoginAttempts()** - 检查登录失败次数，超过5次锁定账户30分钟\n"
            "\n"
            "### 关键业务规则\n"
            "- 密码连续错误5次，账户锁定30分钟\n"
            "- JWT令牌有效期为2小时，刷新令牌有效期为7天\n"
            "- 登录成功后清除之前的失败计数\n"
            "```\n\n"
            "## Token 限制与大型源码文件分块策略\n\n"
            "**重要限制：** 单个知识点的 `business_description` 文本不能超过 **{point_token_limit} tokens**！超过此限制会导致工具调用失败。\n\n"
            "当源码文件的业务描述超过 {point_token_limit} tokens 时，必须采用语义分块策略进行拆分。\n\n"
            "**核心原则：垂直分块（必须「竖着拆」，禁止「横着拆」）**\n\n"
            "向量检索是**「单点命中」机制**——用户的搜索查询只会命中某一个知识点，而不是多个知识点的组合。因此每个分块必须是「自包含」的，能够独立被检索和理解。\n\n"
            "**❌ 错误做法（横着拆）**：按 5 层结构水平切分\n"
            "```\n"
            "分块1: 项目结构域 + 业务域\n"
            "分块2: 功能描述 + 目的描述\n"
            "分块3: 业务逻辑细节\n"
            "```\n"
            "问题：搜索\"后端用户登录JWT令牌\"时，可能命中\"分块3\"，但该分块没有\"后端\"\"用户登录\"等定位词，导致语义相似度不够高，检索失败或排名靠后。\n\n"
            "**✅ 正确做法（竖着拆）**：每个分块都保留完整的定位信息\n"
            "```\n"
            "分块1: 项目结构域 + 业务域 + 功能 + 目的 + 类定义\n"
            "分块2: 项目结构域 + 业务域 + 核心方法详情\n"
            "分块3: 项目结构域 + 业务域 + 辅助方法与内部类\n"
            "```\n"
            "优势：无论用户从「技术栈」「业务模块」「功能」「实现细节」哪个角度搜索，每个分块都能命中，且命中后立即获得完整上下文。\n\n"
            "**冗余是值得的**：用少量存储空间（每个分块多几十个 token）换取检索命中率的大幅提升！\n\n"
            "**分块结构要求：**\n"
            "| 分块类型 | 必须包含 | 重点展开 |\n"
            "|----------|----------|----------|\n"
            "| 第 1 个分块（概述） | 完整 5 层结构 | 类/文件定义 |\n"
            "| 第 2~N 个分块（细节） | 项目结构域 + 业务域 | 业务逻辑细节的不同部分 |\n\n"
            "**分块粒度控制：**\n"
            "- 每个核心方法单独一个分块\n"
            "- 多个小型辅助方法可合并为一个分块\n"
            "- 内部类/枚举可单独一个分块\n"
            "- 建议每个分块控制在 **1500-1800 tokens**，留出安全余量\n\n"
            "**分块示例：**\n\n"
            "假设 `GeminiEmbeddingService.java` 的完整业务描述超过 {point_token_limit} tokens，应拆分为：\n\n"
            "**知识点 1：概述与定义**（包含前 4 层 + 类定义）\n"
            "- entity_name: `GeminiEmbeddingService`\n"
            "- entity_type: `class`\n"
            "- business_description:\n"
            "```\n"
            "## 项目结构域\n"
            "后端源码\n"
            "\n"
            "## 业务域\n"
            "AI能力模块 - 向量嵌入子模块\n"
            "\n"
            "## 功能描述\n"
            "实现文本向量嵌入服务，调用 Google Gemini API 将文本转换为向量表示，支持批量处理和Token计数。\n"
            "\n"
            "## 目的描述\n"
            "作为RAG系统的核心组件，负责将业务文本转换为可检索的向量形式，是知识库语义搜索的基础设施。\n"
            "\n"
            "## 业务逻辑细节\n"
            "\n"
            "### 类/文件定义\n"
            "GeminiEmbeddingService 实现 EmbeddingService 接口，使用 @Service 注解标记为Spring组件。\n"
            "依赖注入 WebClient 用于HTTP调用，配置项包括 API_KEY、MODEL_NAME、BATCH_SIZE 等。\n"
            "```\n\n"
            "**知识点 2：核心方法详情**（保留前 2 层定位 + 核心方法）\n"
            "- entity_name: `GeminiEmbeddingService`\n"
            "- entity_type: `class`\n"
            "- business_description:\n"
            "```\n"
            "## 项目结构域\n"
            "后端源码\n"
            "\n"
            "## 业务域\n"
            "AI能力模块 - 向量嵌入子模块\n"
            "\n"
            "## 业务逻辑细节\n"
            "\n"
            "### 核心方法/函数\n"
            "1. **createEmbeddings(List<String> texts)** - 批量文本向量化\n"
            "   - 入参：待向量化的文本列表\n"
            "   - 处理流程：按 BATCH_SIZE 分批 → 构建API请求 → 调用Gemini接口 → 解析响应提取向量\n"
            "   - 出参：List<float[]> 向量列表，与输入文本一一对应\n"
            "   - 重试机制：失败自动重试3次，指数退避\n"
            "\n"
            "2. **countTokens(String text)** - Token计数\n"
            "   - 调用Gemini的countTokens接口，返回文本的token数量\n"
            "   - 用于分块前的长度预估\n"
            "```\n\n"
            "**知识点 3：辅助方法与内部类**（保留前 2 层定位 + 辅助内容）\n"
            "- entity_name: `GeminiEmbeddingService`\n"
            "- entity_type: `class`\n"
            "- business_description:\n"
            "```\n"
            "## 项目结构域\n"
            "后端源码\n"
            "\n"
            "## 业务域\n"
            "AI能力模块 - 向量嵌入子模块\n"
            "\n"
            "## 业务逻辑细节\n"
            "\n"
            "### 辅助方法/函数\n"
            "- **buildRequest()** - 构建HTTP请求体\n"
            "- **parseResponse()** - 解析API响应，提取embedding数组\n"
            "- **handleRateLimit()** - 处理API限流，实现退避重试\n"
            "\n"
            "### 内部类\n"
            "1. **EmbeddingRequest** - 请求DTO，包含texts数组和model字段\n"
            "2. **EmbeddingResponse** - 响应DTO，包含embeddings数组\n"
            "3. **CountTokensRequest/Response** - Token计数的请求响应对\n"
            "```\n\n"
            "**分块要点：**\n"
            "- 每个分块都保留「项目结构域」和「业务域」，确保单独检索时能定位上下文\n"
            "- 第一个分块包含完整的前 4 层（定位信息）+ 类定义\n"
            "- 后续分块简化前置信息，重点展开业务逻辑细节的不同部分\n\n"
            "## 工具使用规则\n"
            "1. 使用 create_knowledge_points 工具来创建业务描述知识点\n"
            "2. 该工具会校验单个知识点的描述文本是否超过 {point_token_limit} tokens 上限，如果工具返回错误信息请按业务垂直划分分片重新生成\n"
            "3. 每次回复只调用一个工具，不要多工具同时调用\n"
            "4. 知识点创建完成后，调用 end_task 工具结束任务\n"
        )

        self._kp_tools = [
            ToolSchema(
                name="create_knowledge_points",
                description=f"为源码文件创建业务描述知识点。一次调用最多创建{max_points}个知识点。每个知识点的business_description不能超过{token_limit} tokens上限。",
                parameters={
                    "type": "object",
                    "properties": {
                        "points": {
                            "type": "array",
                            "maxItems": max_points,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "business_description": {
                                        "type": "string",
                                        "description": "该知识点的详细业务描述",
                                    },
                                },
                                "required": ["business_description"],
                            },
                        },
                    },
                    "required": ["points"],
                },
            ),
            ToolSchema(
                name="end_task",
                description="源码文件的知识点创建完成后调用此工具结束任务。",
                parameters={
                    "type": "object",
                    "properties": {
                        "signal": {
                            "type": "string",
                            "enum": ["exit"],
                            "description": "固定值 exit",
                        },
                    },
                    "required": ["signal"],
                },
            ),
        ]

    async def _init_kp_providers(self) -> None:
        """注册所有 knowledge_producer agent 并构建服务商→agent 映射。

        按优先级排序后全量注册，构建两个索引结构：
        - _kp_provider_order: 去重的服务商优先级列表（用于 select_provider）
        - _kp_provider_agents: 服务商 → agent 名称列表（同一服务商多模型按优先级排序）
        """
        kp_cfg = self._config.knowledge_producer
        if not kp_cfg.providers:
            logger.warning("knowledge_producer_no_providers_configured")
            return

        sorted_providers = sorted(kp_cfg.providers, key=lambda e: e.priority)
        self._kp_agent_names = []
        self._kp_provider_order = []
        self._kp_provider_agents = {}

        for idx, entry in enumerate(sorted_providers):
            agent_name = f"{_KP_AGENT_PREFIX}_{idx}"
            agent_def = AgentDefinition(
                name=agent_name,
                description="分析源码文件并生成业务描述知识点",
                system_prompt=self._kp_system_prompt,
                provider=entry.provider,
                model=entry.model,
                tools=self._kp_tools,
                timeout=self._config.knowledge_producer.timeout,
                max_rate_limit_retries=self._config.knowledge_producer.max_rate_limit_retries,
            )
            await self._agent.register_agent(agent_def)
            self._kp_agent_names.append(agent_name)

            # 构建服务商 → agent 映射（保持优先级顺序）
            if entry.provider not in self._kp_provider_agents:
                self._kp_provider_agents[entry.provider] = []
                self._kp_provider_order.append(entry.provider)
            self._kp_provider_agents[entry.provider].append(agent_name)

            logger.info(
                "kp_agent_registered",
                agent_name=agent_name,
                provider=entry.provider,
                model=entry.model,
                queue_index=idx,
            )



    # =========================================================================
    # Internal: retry queue / dead letter queue persistence
    # =========================================================================

    def _enqueue_retry(self, record: FailedFileRecord) -> None:
        """Add a failed file to the retry queue and persist."""
        if self._knowledge_sync is not None:
            self._knowledge_sync.mark_latest_file_version(
                record.project_id,
                record.file_path,
                record.file_version_id,
            )
        queue = self._retry_queues.setdefault(record.project_id, [])
        queue[:] = [
            r for r in queue
            if not (
                r.file_path == record.file_path
                and r.file_version_id != record.file_version_id
            )
        ]
        # Deduplicate by file_version_id
        for existing in queue:
            if existing.file_version_id == record.file_version_id:
                return
        queue.append(record)
        self._save_retry_queue(record.project_id)
        logger.info(
            "retry_queue_enqueued",
            project_id=record.project_id,
            file_path=record.file_path,
            file_version_id=record.file_version_id[:16],
            queue_size=len(queue),
        )

    def _archive_to_dead_letter(
        self, record: FailedFileRecord, reason: str,
    ) -> None:
        """Move a FailedFileRecord to the dead letter queue."""
        dl = self._dead_letters.setdefault(record.project_id, [])
        dl.append(DeadLetterRecord(
            project_id=record.project_id,
            file_path=record.file_path,
            blob_hash=record.blob_hash,
            file_version_id=record.file_version_id,
            reason=reason,
            last_error=record.last_error,
            retry_count=record.retry_count,
            created_at=record.created_at,
            archived_at=time.time(),
        ))
        self._save_dead_letter(record.project_id)
        logger.warning(
            "dead_letter_archived",
            project_id=record.project_id,
            file_path=record.file_path,
            reason=reason,
            retry_count=record.retry_count,
        )

    def _retry_queue_path(self, project_id: str) -> str:
        return os.path.join(self._config.data_dir, f"retry_queue_{project_id}.json")

    def _dead_letter_path(self, project_id: str) -> str:
        return os.path.join(self._config.data_dir, f"dead_letter_{project_id}.json")

    def _save_retry_queue(self, project_id: str) -> None:
        """Persist retry queue to disk."""
        path = self._retry_queue_path(project_id)
        queue = self._retry_queues.get(project_id, [])
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{
                    "project_id": r.project_id,
                    "file_path": r.file_path,
                    "blob_hash": r.blob_hash,
                    "file_version_id": r.file_version_id,
                    "is_new": r.is_new,
                    "old_file_version_id": r.old_file_version_id,
                    "retry_count": r.retry_count,
                    "last_error": r.last_error,
                    "created_at": r.created_at,
                } for r in queue], f, ensure_ascii=False)
        except Exception:
            logger.exception("retry_queue_save_failed", project_id=project_id)

    def _load_retry_queue(self, project_id: str) -> List[FailedFileRecord]:
        """Load retry queue from disk."""
        path = self._retry_queue_path(project_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
            return [FailedFileRecord(**item) for item in items]
        except Exception:
            logger.exception("retry_queue_load_failed", project_id=project_id)
            return []

    def _save_dead_letter(self, project_id: str) -> None:
        """Persist dead letter queue to disk."""
        path = self._dead_letter_path(project_id)
        dl = self._dead_letters.get(project_id, [])
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{
                    "project_id": r.project_id,
                    "file_path": r.file_path,
                    "blob_hash": r.blob_hash,
                    "file_version_id": r.file_version_id,
                    "reason": r.reason,
                    "last_error": r.last_error,
                    "retry_count": r.retry_count,
                    "created_at": r.created_at,
                    "archived_at": r.archived_at,
                } for r in dl], f, ensure_ascii=False)
        except Exception:
            logger.exception("dead_letter_save_failed", project_id=project_id)

    def _load_dead_letter(self, project_id: str) -> List[DeadLetterRecord]:
        """Load dead letter queue from disk."""
        path = self._dead_letter_path(project_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
            return [DeadLetterRecord(**item) for item in items]
        except Exception:
            logger.exception("dead_letter_load_failed", project_id=project_id)
            return []

    # =========================================================================
    # Internal: background compensation task
    # =========================================================================

    async def _compensation_loop(self) -> None:
        """Background loop that retries failed files with global backoff.

        Startup timing: runs after ``_register_agents`` completes, so
        knowledge_producer providers are available for fallback.
        """
        comp_cfg = self._config.compensation

        # Load persisted retry queues for all registered projects
        await asyncio.sleep(5)  # brief delay to let projects register
        logger.info("compensation_loop_running")

        while True:
            try:
                # Compute backoff sleep duration
                backoff = min(
                    comp_cfg.base_backoff * (2 ** self._comp_consecutive_failures),
                    comp_cfg.max_backoff,
                )
                sleep_time = max(
                    backoff - (time.time() - self._comp_last_round_at),
                    comp_cfg.scan_interval,
                )
                await asyncio.sleep(sleep_time)

                # Collect all pending records across projects
                all_records: List[FailedFileRecord] = []
                registered_project_ids = {
                    pid for pid in self._project_repos.values()
                }
                for project_id in registered_project_ids:
                    if project_id not in self._retry_queues:
                        self._retry_queues[project_id] = self._load_retry_queue(
                            project_id,
                        )
                    all_records.extend(self._retry_queues.get(project_id, []))

                self._comp_last_round_at = time.time()

                # Take a batch
                batch = all_records[: comp_cfg.batch_size]

                logger.info(
                    "compensation_round_start",
                    total_pending=len(all_records),
                    batch_size=len(batch),
                    consecutive_failures=self._comp_consecutive_failures,
                    backoff_s=round(backoff, 1),
                )

                round_success = 0
                round_failed = 0

                if self._knowledge_sync is not None:
                    outbox_success, outbox_failed = await self._knowledge_sync.retry_ready_jobs(
                        batch_size=comp_cfg.batch_size,
                    )
                    round_success += outbox_success
                    round_failed += outbox_failed

                if not all_records:
                    if round_failed == 0:
                        self._comp_consecutive_failures = 0
                    continue

                for record in batch:
                    result = await self._compensation_process_file(record)
                    if result == "success":
                        round_success += 1
                    elif result == "llm_failed":
                        round_failed += 1
                        # First LLM failure in this round — stop processing
                        # remaining files to avoid wasting requests
                        if round_success == 0:
                            self._comp_consecutive_failures += 1
                            logger.info(
                                "compensation_round_probe_failed",
                                consecutive_failures=(
                                    self._comp_consecutive_failures
                                ),
                            )
                            break
                    # "dead_lettered" doesn't count as LLM failure

                if round_success > 0:
                    self._comp_consecutive_failures = 0

                logger.info(
                    "compensation_round_done",
                    success=round_success,
                    failed=round_failed,
                )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("compensation_loop_error")
                await asyncio.sleep(comp_cfg.scan_interval)

    async def _compensation_process_file(
        self, record: FailedFileRecord,
    ) -> str:
        """Try to re-process a single failed file.

        Returns:
            ``"success"`` — file processed, removed from retry queue.
            ``"dead_lettered"`` — moved to dead letter (blob gone / max retries).
            ``"llm_failed"`` — LLM error, stays in retry queue.
        """
        comp_cfg = self._config.compensation
        project_id = record.project_id
        # Any registered copy holding the blob works: try them in order.
        repo_candidates = [
            repo_key for repo_key, pid in self._project_repos.items()
            if pid == project_id
        ]
        repo_path = repo_candidates[0] if repo_candidates else None

        if not repo_path:
            logger.warning(
                "compensation_skip_unregistered",
                project_id=project_id,
                file_path=record.file_path,
            )
            return "dead_lettered"

        if hasattr(self._git_ops, "is_file_ignored") and self._git_ops.is_file_ignored(repo_path, record.file_path):
            self._remove_from_retry_queue(record)
            logger.info(
                "compensation_skip_ignored_file",
                project_id=project_id,
                file_path=record.file_path,
            )
            return "success"

        if self._knowledge_sync is not None and self._knowledge_sync.is_stale_file_version(
            project_id,
            record.file_path,
            record.file_version_id,
        ):
            self._remove_from_retry_queue(record)
            logger.info(
                "compensation_skip_stale_file_version",
                project_id=project_id,
                file_path=record.file_path,
                file_version_id=record.file_version_id[:16],
            )
            return "success"

        # Check blob availability (content is re-read inside _process_file_with_agent)
        try:
            await self._git_ops.get_file_bytes(
                repo_path, record.blob_hash, file_path=record.file_path,
            )
        except GitFileNotFoundError:
            self._remove_from_retry_queue(record)
            self._archive_to_dead_letter(record, "blob_not_found")
            return "dead_lettered"
        except Exception as e:
            record.last_error = f"git_error: {str(e)[:200]}"
            record.retry_count += 1
            self._save_retry_queue(project_id)
            logger.warning(
                "compensation_git_error",
                project_id=project_id,
                file_path=record.file_path,
                error=str(e)[:200],
            )
            return "llm_failed"

        # Try running the knowledge_producer agent
        try:
            await self._process_file_with_agent(
                project_id, repo_path, record.file_path,
                record.blob_hash, record.file_version_id,
                is_new=record.is_new,
                old_file_version_id=record.old_file_version_id,
                skip_memory_update=True,
                sync_source="compensation_retry",
                update_latest=False,
            )
        except Exception as e:
            record.last_error = str(e)[:500]
            record.retry_count += 1
            logger.warning(
                "compensation_file_failed",
                project_id=project_id,
                file_path=record.file_path,
                retry_count=record.retry_count,
                error=str(e)[:200],
            )
            if record.retry_count >= comp_cfg.max_file_retries:
                self._remove_from_retry_queue(record)
                self._archive_to_dead_letter(record, "max_retries_exceeded")
                return "dead_lettered"
            self._save_retry_queue(project_id)
            return "llm_failed"

        # Success — remove from retry queue
        self._remove_from_retry_queue(record)
        logger.info(
            "compensation_file_success",
            project_id=project_id,
            file_path=record.file_path,
            retry_count=record.retry_count,
        )
        return "success"

    def _remove_from_retry_queue(self, record: FailedFileRecord) -> None:
        """Remove a record from the retry queue and persist."""
        queue = self._retry_queues.get(record.project_id, [])
        self._retry_queues[record.project_id] = [
            r for r in queue if r.file_version_id != record.file_version_id
        ]
        self._save_retry_queue(record.project_id)
