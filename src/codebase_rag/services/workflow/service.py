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
import json
import os
import time
from typing import Dict, List, Optional, Set

from qdrant_client import models

from codebase_rag.common.logging import get_logger
from codebase_rag.common.token_counter import TokenCounter
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import WorkflowConfig
from codebase_rag.services.agent.errors import AgentTaskComplete
from codebase_rag.services.agent.models import AgentDefinition, ToolSchema
from codebase_rag.services.agent.service import AgentService
from codebase_rag.services.embedding.service import EmbeddingService
from codebase_rag.services.event_detection.models import EventType, GitEvent
from codebase_rag.services.git_operations.errors import GitFileNotFoundError
from codebase_rag.services.git_operations.gray_area_store import GrayAreaStore
from codebase_rag.services.git_operations.models import ChangedFiles, RenamedFileEntry
from codebase_rag.services.git_operations.service import GitOperationsService
from codebase_rag.services.memory_qdrant.service import MemoryQdrantService
from codebase_rag.services.mcp_service.pending_knowledge_store import PendingKnowledgeStore
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
_GRAY_AGENT_PREFIX = "gray_area_classifier"


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
        pending_knowledge_store: Optional[PendingKnowledgeStore] = None,
    ) -> None:
        self._config_manager = config_manager
        self._git_ops = git_ops
        self._qdrant = qdrant_storage
        self._memory = memory_qdrant
        self._agent = agent
        self._embedding = embedding
        self._token_counter = token_counter or TokenCounter()
        self._pending_store = pending_knowledge_store

        # Task queue: per-project serial, cross-project parallel
        self._task_queue = ProjectTaskQueue()
        self._task_queue.set_executor(self._execute_task)

        # Project repo path registry (set by caller via register_project)
        self._project_repos: Dict[str, str] = {}

        # Current task info per project (for status queries)
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

        # gray_area_classifier agent 状态
        self._gray_system_prompt: str = ""
        self._gray_tools: List[ToolSchema] = []
        self._gray_agent_names: List[str] = []
        self._gray_provider_order: List[str] = []
        self._gray_provider_agents: Dict[str, List[str]] = {}

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
        logger.info("workflow_service_initialized")

    async def shutdown(self) -> None:
        """Gracefully stop task queue and wait for current tasks."""
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
        """Register a project's repo path (called by management API).

        Also loads persisted retry/dead-letter queues so the background
        compensation task can pick them up after service restart.
        """
        self._project_repos[project_id] = repo_path
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
        """Unregister a project."""
        self._project_repos.pop(project_id, None)

    # =========================================================================
    # 2.1 Event reception (callback for EventDetectionService.subscribe)
    # =========================================================================

    async def on_git_event(self, event: GitEvent) -> None:
        """Receive a GitEvent and dispatch to appropriate workflow."""
        repo_path = self._project_repos.get(event.project_id)
        if repo_path is None:
            logger.warning(
                "workflow_event_unknown_project",
                project_id=event.project_id,
            )
            return

        logger.info(
            "workflow_event_received",
            event_id=event.event_id,
            project_id=event.project_id,
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

    def enqueue_initialization(
        self, project_id: str, branch_name: str, commit_hash: str,
    ) -> None:
        """Enqueue a full initialization task via the project task queue."""
        task = WorkflowTask(
            task_type="initialization",
            project_id=project_id,
            kwargs={"branch_name": branch_name, "commit_hash": commit_hash},
        )
        self._task_queue.enqueue(task)

    def enqueue_content_commit(
        self,
        project_id: str,
        branch_name: str,
        commit_hash: str,
        previous_commit: str,
    ) -> None:
        """Enqueue a content commit task via the project task queue."""
        task = WorkflowTask(
            task_type="content_commit",
            project_id=project_id,
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
        branch_name: str,
        commit_hash: str,
        previous_branch: Optional[str],
        previous_commit: Optional[str],
    ) -> BranchSwitchResult:
        """Execute branch switch — update in-memory instance."""
        start_ms = time.monotonic() * 1000
        repo_path = self._project_repos.get(project_id, "")

        logger.info(
            "workflow_branch_switch_start",
            project_id=project_id,
            branch=branch_name,
            commit=commit_hash[:8],
        )

        # Step 1: Check cache
        cached = await self._memory.get_cached_instance(
            project_id, branch_name, commit_hash,
        )
        if cached is not None:
            await self._memory.set_active_instance(
                project_id, branch_name, commit_hash,
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

        # Step 2: Get new branch file list (include uncertain for loading existing data)
        file_tree = await self._git_ops.get_file_tree(
            repo_path, branch_name, include_uncertain=True,
        )
        new_versions: Set[str] = {e.file_version_id for e in file_tree}

        # Step 3: Get current in-memory file_version_ids (if active instance exists)
        mem_versions: Set[str] = set()
        active_info = await self._memory.get_active_instance_info(project_id)
        if active_info is not None:
            active_handle = await self._memory.get_cached_instance(
                project_id, active_info.branch_name, active_info.commit_hash,
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

        # Step 5: Create new in-memory instance
        handle = await self._memory.create_instance(
            project_id, branch_name, commit_hash,
        )

        # Step 6: Bulk copy unchanged + fetch to_add from Qdrant Server
        file_sem = asyncio.Semaphore(self._config.branch_switch.max_diff_concurrency)

        async def _fetch_and_upsert(fvid: str) -> None:
            async with file_sem:
                try:
                    records = await self._qdrant.get_file_points(project_id, fvid)
                    if records:
                        points = self._records_to_point_structs(records)
                        await self._memory.upsert_points(handle, points)
                except Exception:
                    logger.warning(
                        "branch_switch_fetch_failed",
                        project_id=project_id,
                        file_version_id=fvid[:16],
                    )

        # Fetch unchanged + to_add
        all_needed = list(unchanged | to_add)
        logger.info(
            "workflow_branch_switch_loading",
            project_id=project_id,
            files_to_load=len(all_needed),
        )
        tasks = [_fetch_and_upsert(fvid) for fvid in all_needed]
        if tasks:
            await asyncio.gather(*tasks)

        # Step 8: Set as active
        await self._memory.set_active_instance(
            project_id, branch_name, commit_hash,
        )

        duration = int(time.monotonic() * 1000 - start_ms)
        logger.info(
            "workflow_branch_switch_complete",
            project_id=project_id,
            files_loaded=len(all_needed),
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

    async def rebuild_memory_instance(self, project_id: str) -> None:
        """Rebuild an in-memory instance from Qdrant Server for a project.

        Called by MemoryQdrantService's rebuild handler when a search request
        arrives but no active instance exists (e.g. after idle eviction).

        Flow:
          1. Look up repo_path from registered projects
          2. Read current HEAD (branch + commit)
          3. Get file tree → compute file_version_ids
          4. Create in-memory instance
          5. Batch load Points from Qdrant Server
          6. Set as active instance
        """
        repo_path = self._project_repos.get(project_id)
        if not repo_path:
            raise WorkflowError(
                f"Cannot rebuild: project '{project_id}' not registered in workflow."
            )

        start_ms = time.monotonic() * 1000

        # Step 1: Get current HEAD
        head = await self._git_ops.get_head_info(repo_path)
        branch_name = head.branch_name or "HEAD"
        commit_hash = head.commit_hash

        logger.info(
            "rebuild_memory_start",
            project_id=project_id,
            branch=branch_name,
            commit=commit_hash[:8],
        )

        # Step 2: Check if already cached (race condition guard)
        cached = await self._memory.get_cached_instance(
            project_id, branch_name, commit_hash,
        )
        if cached is not None:
            await self._memory.set_active_instance(
                project_id, branch_name, commit_hash,
            )
            logger.info(
                "rebuild_memory_cache_hit",
                project_id=project_id,
            )
            return

        # Step 3: Get file tree for current branch
        file_tree = await self._git_ops.get_file_tree(
            repo_path, branch_name, include_uncertain=True,
        )
        all_fvids = [e.file_version_id for e in file_tree]

        # Step 4: Create in-memory instance
        handle = await self._memory.create_instance(
            project_id, branch_name, commit_hash,
        )

        # Step 5: Batch load from Qdrant Server
        file_sem = asyncio.Semaphore(
            self._config.branch_switch.max_diff_concurrency,
        )

        async def _fetch_and_upsert(fvid: str) -> None:
            async with file_sem:
                try:
                    records = await self._qdrant.get_file_points(project_id, fvid)
                    if records:
                        points = self._records_to_point_structs(records)
                        await self._memory.upsert_points(handle, points)
                except Exception:
                    logger.warning(
                        "rebuild_fetch_failed",
                        project_id=project_id,
                        file_version_id=fvid[:16],
                    )

        if all_fvids:
            await asyncio.gather(*[_fetch_and_upsert(fvid) for fvid in all_fvids])

        # Step 6: Set as active
        await self._memory.set_active_instance(
            project_id, branch_name, commit_hash,
        )

        duration = int(time.monotonic() * 1000 - start_ms)
        logger.info(
            "rebuild_memory_complete",
            project_id=project_id,
            branch=branch_name,
            files_loaded=len(all_fvids),
            duration_ms=duration,
        )

    # =========================================================================
    # 2.3 Content commit workflow (knowledge production)
    # =========================================================================

    async def workflow_content_commit(
        self,
        project_id: str,
        branch_name: str,
        commit_hash: str,
        previous_commit: str,
    ) -> ContentCommitResult:
        """Execute knowledge production for a content commit."""
        start = time.monotonic()
        repo_path = self._project_repos.get(project_id, "")

        logger.info(
            "workflow_content_commit_start",
            project_id=project_id,
            branch=branch_name,
            commit=commit_hash[:8],
            prev_commit=previous_commit[:8],
        )

        # Step 1: Get changed files (L1 blacklist + L2 whitelist)
        result = await self._git_ops.get_changed_files(
            repo_path, previous_commit, commit_hash,
        )

        # Step 1b: Apply L3 gray-area classification to uncertain files
        l3_added, l3_modified, l3_deleted, l3_renamed = await self._classify_uncertain_files(
            result.uncertain,
        )

        # Merge L2-included + L3-included
        all_added = list(result.changes.added) + l3_added
        all_modified = list(result.changes.modified) + l3_modified
        all_deleted = list(result.changes.deleted) + l3_deleted
        all_renamed = list(result.changes.renamed) + l3_renamed

        logger.info(
            "workflow_content_commit_diff",
            project_id=project_id,
            added_files=len(all_added),
            modified_files=len(all_modified),
            deleted_files=len(all_deleted),
            renamed_files=len(all_renamed),
            l3_added=len(l3_added),
            l3_modified=len(l3_modified),
            l3_deleted=len(l3_deleted),
            l3_renamed=len(l3_renamed),
            excluded=result.excluded_count,
        )

        added_count = 0
        modified_count = 0
        deleted_count = 0
        renamed_count = 0
        skipped_count = 0
        cache_hit_count = 0
        total_points = 0
        total_agent_calls = 0
        failed_files: List[FailedFile] = []

        file_sem = asyncio.Semaphore(self._config.content_commit.max_file_concurrency)

        # Step 2A: Process added files
        async def _process_added(entry) -> None:
            nonlocal added_count, skipped_count, cache_hit_count, total_points, total_agent_calls
            async with file_sem:
                try:
                    pts, skipped, agents, cache_hit = await self._process_file_with_agent(
                        project_id, repo_path, entry.file_path,
                        entry.blob_hash, entry.file_version_id,
                        is_new=True,
                    )
                    total_points += pts
                    total_agent_calls += agents
                    if skipped:
                        skipped_count += 1
                    elif cache_hit:
                        cache_hit_count += 1
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
            nonlocal modified_count, skipped_count, cache_hit_count, total_points, total_agent_calls
            async with file_sem:
                try:
                    # Get old descriptions for context
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
                    )
                    total_points += pts
                    total_agent_calls += agents
                    if skipped:
                        skipped_count += 1
                    elif cache_hit:
                        cache_hit_count += 1
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
                await self._delete_from_memory(project_id, entry.file_version_id)
                deleted_count += 1
            except Exception as e:
                failed_files.append(FailedFile(entry.file_path, str(e)))

        # Step 2R: Process renamed files (clone points, no LLM / embedding)
        async def _process_renamed(entry: RenamedFileEntry) -> None:
            nonlocal renamed_count, total_points
            try:
                # Clone existing points with updated file_path / file_version_id
                cloned = await self._qdrant.clone_points_for_rename(
                    project_id=project_id,
                    blob_hash=entry.blob_hash,
                    new_file_version_id=entry.new_file_version_id,
                    new_file_path=entry.new_file_path,
                )
                if cloned > 0:
                    # Delete old file_version_id from Qdrant
                    await self._qdrant.delete_file_points(
                        project_id, entry.old_file_version_id,
                    )
                    # Update in-memory instance
                    await self._delete_from_memory(
                        project_id, entry.old_file_version_id,
                    )
                    await self._upsert_to_memory_from_qdrant(
                        project_id, entry.new_file_version_id,
                    )
                    total_points += cloned
                    renamed_count += 1
                    logger.info(
                        "workflow_file_renamed",
                        old_path=entry.old_file_path,
                        new_path=entry.new_file_path,
                        cloned_points=cloned,
                    )
                else:
                    # No source points found — fall back to treating as new add
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
            project_id, branch_name, previous_commit, commit_hash,
        )

        duration = time.monotonic() - start

        logger.info(
            "workflow_content_commit_complete",
            project_id=project_id,
            added=added_count,
            modified=modified_count,
            deleted=deleted_count,
            renamed=renamed_count,
            skipped=skipped_count,
            cache_hit=cache_hit_count,
            failed=len(failed_files),
            duration_s=round(duration, 2),
        )

        return ContentCommitResult(
            added_files=added_count,
            modified_files=modified_count,
            deleted_files=deleted_count,
            renamed_files=renamed_count,
            skipped_files=skipped_count,
            cache_hit_files=cache_hit_count,
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
        branch_name: str,
        commit_hash: str,
    ) -> InitializationResult:
        """Full knowledge production for all files in a branch.

        When ``initialization.mode`` is ``"incremental"``, skip the full scan
        and create an empty in-memory instance directly so that subsequent
        commits will build up the knowledge base incrementally.
        """
        # --- incremental mode: empty start ---
        if self._config.initialization.mode == "incremental":
            return await self._incremental_initialization(
                project_id, branch_name, commit_hash,
            )

        start = time.monotonic()
        repo_path = self._project_repos.get(project_id, "")

        logger.info(
            "workflow_initialization_start",
            project_id=project_id,
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

        self._current_tasks[project_id] = TaskInfo(
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

        # Step 5: Build in-memory instance
        handle = await self._memory.create_instance(
            project_id, branch_name, commit_hash,
        )

        # Load all points from Qdrant into memory
        fetch_sem = asyncio.Semaphore(
            self._config.branch_switch.max_diff_concurrency,
        )

        async def _load_to_memory(fvid: str) -> None:
            async with fetch_sem:
                try:
                    records = await self._qdrant.get_file_points(project_id, fvid)
                    if records:
                        points = self._records_to_point_structs(records)
                        await self._memory.upsert_points(handle, points)
                except Exception:
                    logger.warning(
                        "workflow_init_memory_load_failed",
                        file_version_id=fvid[:16],
                    )

        load_tasks = [_load_to_memory(fvid) for fvid in processed_fvids]
        if load_tasks:
            await asyncio.gather(*load_tasks)

        # Step 6: Set as active
        await self._memory.set_active_instance(
            project_id, branch_name, commit_hash,
        )

        self._current_tasks.pop(project_id, None)

        duration = time.monotonic() - start

        logger.info(
            "workflow_initialization_complete",
            project_id=project_id,
            total=len(file_tree),
            processed=processed,
            skipped=skipped,
            failed=len(failed_files),
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
        branch_name: str,
        commit_hash: str,
    ) -> InitializationResult:
        """Incremental mode: load existing knowledge from persistent storage
        into an in-memory instance, but do NOT generate missing descriptions.

        The knowledge base grows as the user makes commits.
        """
        start = time.monotonic()
        repo_path = self._project_repos.get(project_id, "")

        logger.info(
            "workflow_incremental_initialization_start",
            project_id=project_id,
            branch=branch_name,
            commit=commit_hash[:8],
        )

        # Step 1: Get current file tree (include uncertain for loading existing data)
        file_tree = await self._git_ops.get_file_tree(
            repo_path, branch_name, include_uncertain=True,
        )
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

        # Step 3: Create in-memory instance
        handle = await self._memory.create_instance(
            project_id, branch_name, commit_hash,
        )

        # Step 4: Load existing points from persistent storage into memory
        fetch_sem = asyncio.Semaphore(
            self._config.branch_switch.max_diff_concurrency,
        )
        loaded_count = 0

        async def _load_to_memory(fvid: str) -> None:
            nonlocal loaded_count
            async with fetch_sem:
                try:
                    records = await self._qdrant.get_file_points(project_id, fvid)
                    if records:
                        points = self._records_to_point_structs(records)
                        await self._memory.upsert_points(handle, points)
                        loaded_count += len(records)
                except Exception:
                    logger.warning(
                        "workflow_incremental_memory_load_failed",
                        file_version_id=fvid[:16],
                    )

        if existing_fvids:
            load_tasks = [_load_to_memory(fvid) for fvid in existing_fvids]
            await asyncio.gather(*load_tasks)

        # Step 5: Set as active
        await self._memory.set_active_instance(
            project_id, branch_name, commit_hash,
        )

        duration = time.monotonic() - start

        logger.info(
            "workflow_incremental_initialization_complete",
            project_id=project_id,
            existing_files_loaded=len(existing_fvids),
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

    def get_workflow_status(self, project_id: str) -> WorkflowStatus:
        """Get current workflow status for a project."""
        return WorkflowStatus(
            current_task=self._current_tasks.get(project_id),
            queued_tasks=self._task_queue.get_queue_depth(project_id),
            last_completed=self._last_completed.get(project_id),
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
    ) -> tuple:
        """Process a single file via knowledge_producer agent with tool calling.

        Args:
            skip_memory_update: If True, skip in-memory Qdrant update (used by
                compensation task which only writes to persistent storage).

        Returns (points_written, skipped, agent_calls, cache_hit).
        """
        # Content-addressing dedup
        existence = await self._qdrant.check_existence(project_id, [file_version_id])
        if existence.get(file_version_id, False):
            logger.debug(
                "workflow_file_dedup_skip",
                file_path=file_path,
                file_version_id=file_version_id[:16],
            )
            if not skip_memory_update:
                await self._upsert_to_memory_from_qdrant(project_id, file_version_id)
                if old_file_version_id:
                    await self._delete_from_memory(project_id, old_file_version_id)
            return (0, True, 0, False)

        # Check pending knowledge cache (pre-commit knowledge from MCP tools)
        if self._pending_store is not None:
            pending = await self._pending_store.consume(project_id, file_version_id)
            if pending is not None:
                logger.info(
                    "workflow_pending_cache_hit",
                    file_path=file_path,
                    file_version_id=file_version_id[:16],
                    points=len(pending.points),
                )
                # Write pre-built PointData (with pre-generated vectors) to Qdrant
                await self._qdrant.upsert_file_points(
                    project_id=project_id,
                    file_version_id=file_version_id,
                    points_data=pending.points,
                )
                if not skip_memory_update:
                    await self._upsert_to_memory_from_qdrant(
                        project_id, file_version_id,
                    )
                    if old_file_version_id:
                        await self._delete_from_memory(
                            project_id, old_file_version_id,
                        )
                return (len(pending.points), False, 0, True)

        # Get file content
        source_code = await self._git_ops.get_file_content(repo_path, blob_hash)

        # Build user message
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

        # ----- Tool handler: create_knowledge_points -----
        async def _handle_create_knowledge_points(points: list, **_kwargs) -> str:
            # Validate token limits for ALL points, collect all violations
            violations = []
            for idx, p in enumerate(points):
                desc = p.get("business_description", "")
                tokens = await self._token_counter.count(desc)
                if tokens > token_limit:
                    violations.append(
                        f"第{idx + 1}个知识点超过{token_limit}tokens上限"
                        f"（实际{tokens}tokens）"
                    )
            if violations:
                detail = "；".join(violations)
                return (
                    f"执行失败，{detail}，"
                    f"请按业务垂直划分分片，重新生成。"
                )

            # All passed — embed descriptions
            descriptions = [p["business_description"] for p in points]
            logger.debug(
                "workflow_embedding_call",
                file_path=file_path,
                descriptions_count=len(descriptions),
            )
            embeddings = await self._embedding.embed(descriptions)

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
                    sparse_vector=emb.sparse_vector,
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

        # Atomic write: all accumulated points in one delete-then-write call
        if points_written:
            await self._qdrant.upsert_file_points(
                project_id, file_version_id, points_written,
            )

        # Update in-memory instance (skipped by compensation task)
        if not skip_memory_update:
            if old_file_version_id:
                await self._delete_from_memory(project_id, old_file_version_id)
            await self._upsert_to_memory_from_qdrant(project_id, file_version_id)

        return (len(points_written), False, 1, False)

    # =========================================================================
    # Internal: memory helpers
    # =========================================================================

    async def _upsert_to_memory_from_qdrant(
        self, project_id: str, file_version_id: str,
    ) -> None:
        """Load points from Qdrant and upsert into active in-memory instance."""
        active_info = await self._memory.get_active_instance_info(project_id)
        if active_info is None:
            return

        handle = await self._memory.get_cached_instance(
            project_id, active_info.branch_name, active_info.commit_hash,
        )
        if handle is None:
            return

        records = await self._qdrant.get_file_points(project_id, file_version_id)
        if records:
            points = self._records_to_point_structs(records)
            await self._memory.upsert_points(handle, points)
            logger.debug(
                "memory_upsert_from_qdrant",
                project_id=project_id,
                file_version_id=file_version_id[:16],
                points=len(points),
            )

    async def _delete_from_memory(
        self, project_id: str, file_version_id: str,
    ) -> None:
        """Delete points from active in-memory instance."""
        active_info = await self._memory.get_active_instance_info(project_id)
        if active_info is None:
            return

        handle = await self._memory.get_cached_instance(
            project_id, active_info.branch_name, active_info.commit_hash,
        )
        if handle is None:
            return

        await self._memory.delete_points_by_file_version(handle, [file_version_id])
        logger.debug(
            "memory_delete_file_version",
            project_id=project_id,
            file_version_id=file_version_id[:16],
        )

    @staticmethod
    def _records_to_point_structs(records: list) -> List[models.PointStruct]:
        """Convert Qdrant Record objects to PointStruct for in-memory upsert."""
        points = []
        sparse_count = 0
        for rec in records:
            payload = rec.payload or {}
            vectors = {}

            if rec.vector:
                if isinstance(rec.vector, dict):
                    for name, vec in rec.vector.items():
                        if isinstance(vec, models.SparseVector):
                            vectors[name] = vec
                            sparse_count += 1
                        else:
                            vectors[name] = vec
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
                with_sparse=sparse_count,
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

    # =========================================================================
    # Internal: task execution dispatcher
    # =========================================================================

    async def _execute_task(self, task: WorkflowTask) -> None:
        """Execute a workflow task (called by ProjectTaskQueue)."""
        project_id = task.project_id
        repo_path = self._project_repos.get(project_id, "")

        task_info = TaskInfo(
            task_type=task.task_type,
            project_id=project_id,
        )
        self._current_tasks[project_id] = task_info

        try:
            if task.task_type == "branch_switch":
                await self.workflow_branch_switch(
                    project_id=project_id,
                    **task.kwargs,
                )
            elif task.task_type == "content_commit":
                await self.workflow_content_commit(
                    project_id=project_id,
                    **task.kwargs,
                )
            elif task.task_type == "initialization":
                await self.workflow_full_initialization(
                    project_id=project_id,
                    **task.kwargs,
                )
            else:
                logger.warning(
                    "workflow_unknown_task_type",
                    task_type=task.task_type,
                )
        finally:
            self._last_completed[project_id] = task_info
            self._current_tasks.pop(project_id, None)

    # =========================================================================
    # Internal: agent registration
    # =========================================================================

    async def _register_agents(self) -> None:
        """Build agent definitions and register first provider for each agent type."""
        self._build_kp_defs()
        self._build_gray_defs()
        await self._init_kp_providers()
        await self._init_gray_providers()

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

    # ----- gray_area_classifier agent defs -----

    def _build_gray_defs(self) -> None:
        """Build and store gray_area_classifier system prompt + tool schemas."""
        self._gray_system_prompt = (
            "你是文件类型分类专家。你的任务是判断给定的文件类型（按扩展名或文件名）是否应纳入代码知识库。\n\n"
            "**纳入标准（include）：**\n"
            "- 源代码文件（任何编程语言）\n"
            "- 项目配置文件（构建、部署、CI/CD、环境变量等）\n"
            "- 构建脚本\n"
            "- 技术文档（README、API文档、设计文档等）\n"
            "- API/协议定义文件（proto、graphql、openapi等）\n"
            "- 数据库脚本（SQL、migration等）\n\n"
            "**排除标准（exclude）：**\n"
            "- 二进制文件（图片、字体、编译产物、压缩包等）\n"
            "- 媒体文件（音频、视频、动画等）\n"
            "- 生成的文件（minified JS/CSS、source map、compiled 等）\n"
            "- 大型数据文件（CSV数据集、数据库文件等）\n"
            "- 其他与代码理解无关的文件\n\n"
            "请逐个分析，使用 submit_classifications 工具一次性提交所有分类结果，然后调用 end_task 结束。"
        )

        self._gray_tools = [
            ToolSchema(
                name="submit_classifications",
                description="提交文件类型分类结果。",
                parameters={
                    "type": "object",
                    "properties": {
                        "classifications": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type_key": {
                                        "type": "string",
                                        "description": "文件类型键，格式为 ext:<扩展名> 或 name:<文件名>",
                                    },
                                    "decision": {
                                        "type": "string",
                                        "enum": ["include", "exclude"],
                                        "description": "分类决定：include=纳入知识库，exclude=排除",
                                    },
                                },
                                "required": ["type_key", "decision"],
                            },
                        },
                    },
                    "required": ["classifications"],
                },
            ),
            ToolSchema(
                name="end_task",
                description="分类完成后调用此工具结束任务。",
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

    async def _init_gray_providers(self) -> None:
        """注册所有 gray_area_classifier agent 并构建服务商→agent 映射。"""
        gray_cfg = self._config.gray_area_classifier
        if not gray_cfg.providers:
            logger.warning("gray_area_classifier_no_providers_configured")
            return

        sorted_providers = sorted(gray_cfg.providers, key=lambda e: e.priority)
        self._gray_agent_names = []
        self._gray_provider_order = []
        self._gray_provider_agents = {}

        for idx, entry in enumerate(sorted_providers):
            agent_name = f"{_GRAY_AGENT_PREFIX}_{idx}"
            agent_def = AgentDefinition(
                name=agent_name,
                description="判断灰区文件类型是否应纳入代码知识库",
                system_prompt=self._gray_system_prompt,
                provider=entry.provider,
                model=entry.model,
                tools=self._gray_tools,
                timeout=self._config.gray_area_classifier.timeout,
                max_rate_limit_retries=self._config.gray_area_classifier.max_rate_limit_retries,
            )
            await self._agent.register_agent(agent_def)
            self._gray_agent_names.append(agent_name)

            if entry.provider not in self._gray_provider_agents:
                self._gray_provider_agents[entry.provider] = []
                self._gray_provider_order.append(entry.provider)
            self._gray_provider_agents[entry.provider].append(agent_name)

            logger.info(
                "gray_agent_registered",
                agent_name=agent_name,
                provider=entry.provider,
                model=entry.model,
                queue_index=idx,
            )

    # =========================================================================
    # Internal: L3 gray-area classification
    # =========================================================================

    async def _classify_uncertain_files(
        self, uncertain: ChangedFiles,
    ) -> tuple:
        """Apply L3 gray-area classification to uncertain changed files.

        Flow: GrayAreaStore lookup → LLM fallback → persist → filter.

        Returns:
            ``(added, modified, deleted, renamed)`` — only L3-included file entries.
        """
        all_paths = (
            [e.file_path for e in uncertain.added]
            + [e.file_path for e in uncertain.modified]
            + [e.file_path for e in uncertain.deleted]
            + [e.new_file_path for e in uncertain.renamed]
        )

        if not all_paths:
            return [], [], [], []

        store = self._git_ops.gray_area_store

        # Step 1: Resolve via persistent store
        included_paths, excluded_paths, unknown_paths = store.resolve(all_paths)

        logger.info(
            "l3_store_resolve",
            total_uncertain=len(all_paths),
            store_included=len(included_paths),
            store_excluded=len(excluded_paths),
            still_unknown=len(unknown_paths),
        )

        # Step 2: LLM classify unknown types (if enabled and there are unknowns)
        if unknown_paths and self._git_ops._config.file_filter.uncertain.enabled:
            unknown_keys = store.get_unknown_keys(unknown_paths)
            if unknown_keys:
                classifications = await self._llm_classify_gray_area(
                    unknown_keys, unknown_paths,
                )
                if classifications:
                    store.update(classifications)

                    # Re-resolve the unknown paths with new classifications
                    newly_inc, newly_exc, still_unk = store.resolve(unknown_paths)
                    included_paths.extend(newly_inc)

                    logger.info(
                        "l3_llm_classified",
                        keys_classified=len(classifications),
                        newly_included=len(newly_inc),
                        newly_excluded=len(newly_exc),
                        still_unknown=len(still_unk),
                    )

        included_set = set(included_paths)

        added = [e for e in uncertain.added if e.file_path in included_set]
        modified = [e for e in uncertain.modified if e.file_path in included_set]
        deleted = [e for e in uncertain.deleted if e.file_path in included_set]
        renamed = [e for e in uncertain.renamed if e.new_file_path in included_set]

        return added, modified, deleted, renamed

    async def _llm_classify_gray_area(
        self, unknown_keys: Set[str], sample_paths: List[str],
    ) -> Dict[str, str]:
        """Call gray_area_classifier agent to classify unknown file types.

        Args:
            unknown_keys: Type keys like ``ext:pyx``, ``name:Makefile``.
            sample_paths: Paths for building context examples.

        Returns:
            Mapping of type_key → ``"include"`` / ``"exclude"``.
        """
        # Build per-key sample paths for LLM context
        key_samples: Dict[str, List[str]] = {}
        for path in sample_paths:
            key = GrayAreaStore.extract_type_key(path)
            if key in unknown_keys:
                key_samples.setdefault(key, []).append(path)

        lines = ["以下是需要分类的文件类型，请判断每个类型是否应纳入代码知识库：\n"]
        for key in sorted(unknown_keys):
            samples = key_samples.get(key, [])[:3]
            sample_str = ", ".join(samples)
            lines.append(f"- **{key}** (示例路径: {sample_str})")

        user_message = "\n".join(lines)

        # Mutable state captured by tool handler closure
        classifications: Dict[str, str] = {}

        async def _handle_submit(classifications_list: list = None, **_kwargs) -> str:
            items = classifications_list or _kwargs.get("classifications", [])
            for item in items:
                type_key = item.get("type_key", "")
                decision = item.get("decision", "")
                if type_key and decision in ("include", "exclude"):
                    classifications[type_key] = decision
            return f"已记录 {len(items)} 个分类结果。"

        async def _handle_end_task(signal: str = "exit", **_kwargs) -> str:
            raise AgentTaskComplete()

        tool_handlers = {
            "submit_classifications": _handle_submit,
            "end_task": _handle_end_task,
        }

        # 容量感知的服务商选择 + 故障降级 fallback
        last_error: Optional[Exception] = None
        remaining_providers = list(self._gray_provider_order)
        tried_agents: set = set()

        while remaining_providers:
            selected = await self._agent.select_provider(remaining_providers)
            agent_candidates = self._gray_provider_agents.get(selected, [])

            success = False
            for agent_name in agent_candidates:
                if agent_name in tried_agents:
                    continue
                tried_agents.add(agent_name)

                try:
                    await self._agent.run_agent(
                        agent_name,
                        {},
                        tool_handlers=tool_handlers,
                        user_message=user_message,
                    )
                    success = True
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "gray_provider_failed",
                        agent_name=agent_name,
                        error=str(e)[:200],
                    )
                    classifications.clear()

            if success:
                break

            remaining_providers.remove(selected)
        else:
            logger.warning(
                "l3_llm_classify_all_providers_exhausted",
                unknown_keys=len(unknown_keys),
                last_error=str(last_error)[:200] if last_error else None,
            )

        logger.info(
            "l3_llm_classify_result",
            unknown_keys=len(unknown_keys),
            classified=len(classifications),
            include=sum(1 for v in classifications.values() if v == "include"),
            exclude=sum(1 for v in classifications.values() if v == "exclude"),
        )

        return classifications

    # =========================================================================
    # Internal: retry queue / dead letter queue persistence
    # =========================================================================

    def _enqueue_retry(self, record: FailedFileRecord) -> None:
        """Add a failed file to the retry queue and persist."""
        queue = self._retry_queues.setdefault(record.project_id, [])
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
                for project_id in list(self._project_repos):
                    if project_id not in self._retry_queues:
                        self._retry_queues[project_id] = self._load_retry_queue(
                            project_id,
                        )
                    all_records.extend(self._retry_queues.get(project_id, []))

                if not all_records:
                    self._comp_consecutive_failures = 0
                    continue

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
        repo_path = self._project_repos.get(project_id)

        if not repo_path:
            logger.warning(
                "compensation_skip_unregistered",
                project_id=project_id,
                file_path=record.file_path,
            )
            return "dead_lettered"

        # Check blob availability (content is re-read inside _process_file_with_agent)
        try:
            await self._git_ops.get_file_content(
                repo_path, record.blob_hash,
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
