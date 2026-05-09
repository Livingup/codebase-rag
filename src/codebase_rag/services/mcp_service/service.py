"""MCP Service — two-stage semantic search with LLM reranking + knowledge sync.

Provides the core search logic used by the MCP Server tools:
  Stage 1: Hybrid vector search (dense + sparse) → recall up to recall_limit candidates
  Stage 2: LLM reranker agent scores candidates → return top_k results

Also provides knowledge point management:
  - get_file_knowledge: batch-query committed knowledge descriptions
  - add_file_knowledge: submit pre-commit knowledge through KnowledgeSyncService

Internal dependencies:
  - 01-Qdrant Storage Service (committed knowledge query)
  - 02-Memory Qdrant Service (hybrid search)
  - 03-Agent Service (reranker agent execution)
  - 04-Embedding Service (query vectorization + knowledge embedding)
  - 07-Git Operations Service (workdir blob hash)
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Dict, List, Optional

from codebase_rag.common.logging import get_logger
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import KnowledgeSubmissionConfig, MCPRerankerConfig
from codebase_rag.services.agent.errors import AgentTaskComplete
from codebase_rag.services.agent.models import AgentDefinition, ToolSchema
from codebase_rag.services.agent.service import AgentService
from codebase_rag.services.embedding.service import EmbeddingService
from codebase_rag.services.git_operations.service import (
    GitOperationsService,
    compute_file_version_id,
)
from codebase_rag.services.memory_qdrant.models import ActiveInstanceInfo
from codebase_rag.services.memory_qdrant.service import MemoryQdrantService
from codebase_rag.services.knowledge_sync.service import KnowledgeSyncService
from codebase_rag.services.mcp_service.models import (
    AddFileFailure,
    AddFileSuccess,
    AddKnowledgeResult,
    CandidatePoint,
    FileKnowledgeResult,
    McpServiceError,
    ProjectNotIndexedError,
    ProjectNotRegisteredError,
    RerankerError,
    RerankerScoreEntry,
    SearchResultPoint,
)
from codebase_rag.services.qdrant_storage.models import PointData
from codebase_rag.services.qdrant_storage.service import QdrantStorageService

logger = get_logger(__name__)

# Directory inside .git/ for project metadata
_PROJECT_META_DIR = "codebase-rag"
_PROJECT_ID_FILE = "project_id"

# Reranker agent name prefix (suffixed with index: reranker_0, reranker_1, ...)
_RERANKER_AGENT_PREFIX = "reranker"


class McpService:
    """MCP search service with two-stage retrieval + LLM reranking.

    Also provides knowledge point submission for pre-commit files.

    Args:
        config_manager: Configuration manager.
        embedding: Embedding service for query vectorization and knowledge embedding.
        memory_qdrant: Memory Qdrant service for hybrid search.
        agent: Agent service for reranker execution.
        git_ops: Git operations service for workdir blob hash computation.
        qdrant_storage: Qdrant storage service for committed knowledge query.
        knowledge_sync: Shared knowledge synchronization service.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        embedding: EmbeddingService,
        memory_qdrant: MemoryQdrantService,
        agent: AgentService,
        git_ops: Optional[GitOperationsService] = None,
        qdrant_storage: Optional[QdrantStorageService] = None,
        knowledge_sync: Optional[KnowledgeSyncService] = None,
    ) -> None:
        self._config_manager = config_manager
        self._embedding = embedding
        self._memory = memory_qdrant
        self._agent = agent
        self._git_ops = git_ops
        self._qdrant_storage = qdrant_storage
        self._knowledge_sync = knowledge_sync

        self._sorted_providers: list = []
        self._reranker_agent_names: List[str] = []
        self._reranker_next_idx: int = 0
        self._initialized = False

    @property
    def _reranker_config(self) -> MCPRerankerConfig:
        return self._config_manager.config.mcp.reranker

    @property
    def _knowledge_submission_config(self) -> KnowledgeSubmissionConfig:
        return self._config_manager.config.mcp.knowledge_submission

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Initialize service and register the reranker agent."""
        logger.info("mcp_service_initializing")

        await self._register_reranker_agent()

        self._initialized = True
        logger.info("mcp_service_initialized")

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        self._initialized = False
        logger.info("mcp_service_shutdown")

    # =========================================================================
    # 2.1 Two-stage search
    # =========================================================================

    async def search(
        self,
        project_path: str,
        query: str,
        top_k: int = 10,
    ) -> List[SearchResultPoint]:
        """Execute two-stage search: vector recall + LLM reranking.

        Args:
            project_path: Absolute path to project root (contains .git/).
            query: Natural language search query.
            top_k: Max results to return after reranking.

        Returns:
            List of SearchResultPoint sorted by reranker score descending.
        """
        # Step 0: Resolve project_id from .git/codebase-rag/project_id
        project_id = self._read_project_id(project_path)

        # Verify active instance exists
        active = await self._memory.get_active_instance_info(project_id)
        if not active:
            raise ProjectNotIndexedError(
                f"Project at '{project_path}' has no active index. "
                f"Register it via the management API first."
            )

        # ---- Stage 1: Vector recall ----
        recall_limit = self._reranker_config.recall_limit

        # Embed query (dense + sparse)
        embeddings = await self._embedding.embed([query], is_query=True)
        emb = embeddings[0]

        has_sparse = emb.sparse_vector is not None and len(emb.sparse_vector.indices) > 0
        logger.info(
            "mcp_search_start",
            project_id=project_id,
            query_preview=query[:80],
            top_k=top_k,
            recall_limit=recall_limit,
            dense_dim=len(emb.dense_vector),
            has_sparse=has_sparse,
        )

        # Hybrid search
        candidates_raw = await self._memory.hybrid_search(
            project_id=project_id,
            query_dense=emb.dense_vector,
            query_sparse=emb.sparse_vector,
            top_k=recall_limit,
        )

        if not candidates_raw:
            logger.info("mcp_search_no_candidates", project_id=project_id)
            return []

        # Build candidate list
        candidates = [
            CandidatePoint(
                id=r.file_version_id,
                file_path=r.file_path,
                business_description=r.business_description,
                vector_score=r.score,
            )
            for r in candidates_raw
        ]

        logger.info(
            "stage1_recall_complete",
            project_id=project_id,
            query_len=len(query),
            candidates=len(candidates),
        )

        # If recall count <= top_k, skip reranking (not enough to rerank)
        if len(candidates) <= top_k:
            logger.info(
                "mcp_rerank_skipped",
                project_id=project_id,
                reason="candidates_leq_top_k",
                candidates=len(candidates),
                top_k=top_k,
            )
            return [
                SearchResultPoint(
                    score=c.vector_score,
                    file_path=os.path.join(project_path, c.file_path),
                    business_description=c.business_description,
                )
                for c in candidates
            ]

        # ---- Stage 2: LLM reranking (pick top_k+5, capped by candidate count) ----
        pick_n = min(top_k + 5, len(candidates))
        logger.info(
            "stage2_reranker_start",
            project_id=project_id,
            candidates=len(candidates),
            pick_n=pick_n,
            provider_count=len(self._reranker_agent_names),
        )
        try:
            scores = await self._run_reranker(query, candidates, pick_n)
        except RerankerError as e:
            logger.warning(
                "stage2_reranker_fallback",
                project_id=project_id,
                reason="reranker_failed",
                error=str(e),
                fallback="un-reranked top_k",
            )
            return [
                SearchResultPoint(
                    score=c.vector_score,
                    file_path=os.path.join(project_path, c.file_path),
                    business_description=c.business_description,
                )
                for c in candidates[:top_k]
            ]

        # Build id→candidate lookup
        candidate_map = {c.id: c for c in candidates}

        # Sort returned scores descending, take up to top_k
        scores.sort(key=lambda s: s.score, reverse=True)
        results = []
        for s in scores[:top_k]:
            c = candidate_map.get(s.id)
            if c is None:
                continue
            results.append(SearchResultPoint(
                score=round(s.score, 4),
                file_path=os.path.join(project_path, c.file_path),
                business_description=c.business_description,
            ))

        logger.info(
            "stage2_reranker_success",
            project_id=project_id,
            candidates=len(candidates),
            pick_n=pick_n,
            scored_count=len(scores),
            returned=len(results),
            top_score=round(results[0].score, 4) if results else 0,
        )

        return results

    # =========================================================================
    # 2.2 Reranker agent
    # =========================================================================

    async def _run_reranker(
        self,
        query: str,
        candidates: List[CandidatePoint],
        top_k: int,
    ) -> List[RerankerScoreEntry]:
        """Run the reranker agent to pick top_k most relevant candidates.

        Lazy fallback: tries the current registered agent first. On failure,
        registers the next provider in the queue and retries.

        Returns:
            List of RerankerScoreEntry with id + score (up to pick_n).
        """
        user_message = self._build_reranker_user_message(query, candidates, top_k)
        valid_ids = {c.id for c in candidates}
        last_error: Optional[Exception] = None
        tried_idx = 0

        while tried_idx < len(self._reranker_agent_names) or await self._register_next_reranker():
            agent_name = self._reranker_agent_names[tried_idx]
            tried_idx += 1

            all_scores: List[RerankerScoreEntry] = []

            async def handle_submit_scores(scores: list) -> str:
                for entry in scores:
                    point_id = entry.get("id")
                    score = entry.get("score", 0.0)
                    if point_id is not None and point_id in valid_ids:
                        score = max(0.0, min(1.0, float(score)))
                        all_scores.append(RerankerScoreEntry(id=point_id, score=score))
                return f"已接收 {len(scores)} 条评分。"

            async def handle_end_task(**kwargs) -> str:
                raise AgentTaskComplete()

            tool_handlers = {
                "submit_scores": handle_submit_scores,
                "end_task": handle_end_task,
            }

            try:
                await self._agent.run_agent(
                    agent_name=agent_name,
                    input_variables={},
                    tool_handlers=tool_handlers,
                    user_message=user_message,
                )
            except AgentTaskComplete:
                pass  # Normal exit via end_task
            except Exception as e:
                last_error = e
                logger.warning(
                    "reranker_provider_failed",
                    agent_name=agent_name,
                    error=str(e)[:200],
                )
                continue  # Register & try next provider

            return all_scores

        raise RerankerError(
            f"All reranker providers exhausted. Last error: {last_error}"
        )

    @staticmethod
    def _build_reranker_user_message(
        query: str,
        candidates: List[CandidatePoint],
        pick_n: int,
    ) -> str:
        """Build the user message for the reranker agent.

        Candidates are provided as a JSON array. Each object only contains
        id and business_description — the minimum needed for relevance judgment.
        """
        points = [
            {
                "id": c.id,
                "business_description": c.business_description,
            }
            for c in candidates
        ]

        return (
            f"用户查询：{query}\n\n"
            f"以下是从知识库中召回的 {len(candidates)} 条候选知识点（JSON）：\n"
            f"```json\n{json.dumps(points, ensure_ascii=False, indent=2)}\n```\n\n"
            f"请从中挑选与查询最相关的 **{pick_n}** 条，"
            f"只对这 {pick_n} 条打分，其余忽略。"
        )

    # =========================================================================
    # 2.3 Register reranker agent definition
    # =========================================================================

    async def _register_reranker_agent(self) -> None:
        """Sort providers and register only the first one. Others are lazy."""
        reranker_cfg = self._reranker_config

        if not reranker_cfg.providers:
            logger.warning("reranker_no_providers_configured")
            return

        # Sort by priority and store for lazy registration
        self._sorted_providers = sorted(reranker_cfg.providers, key=lambda e: e.priority)
        self._reranker_agent_names = []
        self._reranker_next_idx = 0

        # Register only the first (highest priority) provider
        await self._register_next_reranker()

    async def _register_next_reranker(self) -> bool:
        """Register the next provider in the queue. Returns False if exhausted."""
        if self._reranker_next_idx >= len(self._sorted_providers):
            return False

        entry = self._sorted_providers[self._reranker_next_idx]
        idx = self._reranker_next_idx
        self._reranker_next_idx += 1

        agent_name = f"{_RERANKER_AGENT_PREFIX}_{idx}"

        system_prompt = (
            "你是一个语义相关性评估推理专家。\n\n"
            "## 任务\n"
            "用户会提供一个查询和一组候选知识点（JSON 数组，每个对象含 id 和 business_description）。\n"
            "根据 business_description 与查询的相关性进行 0.0~1.0 打分，"
            "分数越高相关性越强。给出相关性最高的 N 条（N 由用户指定）及其分数。\n\n"
            "## 工具使用规则\n"
            "1. 使用 submit_scores 工具提交挑选出的 N 条的 id 和分数，可多次调用分批提交\n"
            "2. 每次回复只调用一个工具\n"
            "3. 提交完毕后，调用 end_task 工具结束任务\n"
        )

        tools = [
            ToolSchema(
                name="submit_scores",
                description=(
                    "提交一批候选知识点的相关性分数。"
                    "分数范围 0.0~1.0，分数越高相关性越强。"
                    "可多次调用，每次提交一批。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "scores": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "description": "候选知识点的 id",
                                    },
                                    "score": {
                                        "type": "number",
                                        "minimum": 0.0,
                                        "maximum": 1.0,
                                        "description": "相关性分数",
                                    },
                                },
                                "required": ["id", "score"],
                            },
                        },
                    },
                    "required": ["scores"],
                },
            ),
            ToolSchema(
                name="end_task",
                description="所有候选评分完毕后调用此工具结束任务。",
                parameters={
                    "type": "object",
                    "properties": {
                        "signal": {
                            "type": "string",
                            "enum": ["exit"],
                            "description": "固定传 exit",
                        },
                    },
                    "required": ["signal"],
                },
            ),
        ]

        agent_def = AgentDefinition(
            name=agent_name,
            description="Semantic relevance reranker for codebase search",
            system_prompt=system_prompt,
            provider=entry.provider,
            model=entry.model,
            tools=tools,
            timeout=self._reranker_config.timeout,
            max_rate_limit_retries=self._reranker_config.max_rate_limit_retries,
        )
        await self._agent.register_agent(agent_def)
        self._reranker_agent_names.append(agent_name)
        logger.info(
            "reranker_agent_registered",
            agent_name=agent_name,
            provider=entry.provider,
            model=entry.model,
            queue_index=idx,
        )
        return True

    # =========================================================================
    # 2.4 Project info helpers
    # =========================================================================

    async def get_active_instance_info(
        self, project_path: str,
    ) -> Optional[ActiveInstanceInfo]:
        """Get active instance info for a project path."""
        project_id = self._read_project_id(project_path)
        return await self._memory.get_active_instance_info(project_id)

    # =========================================================================
    # 2.5 Knowledge query — get committed file knowledge
    # =========================================================================

    async def get_file_knowledge(
        self,
        project_path: str,
        file_paths: List[str],
    ) -> List[FileKnowledgeResult]:
        """Batch-query committed knowledge descriptions for given files.

        For each file, resolves its HEAD blob hash, computes file_version_id,
        and queries Qdrant for stored knowledge points.

        Args:
            project_path: Absolute path to the Git repository root.
            file_paths: List of file paths relative to the repository root.

        Returns:
            List of FileKnowledgeResult, one per input file_path.
        """
        assert self._git_ops is not None, "git_ops dependency required"
        assert self._qdrant_storage is not None, "qdrant_storage dependency required"

        project_id = self._read_project_id(project_path)

        # Get HEAD ref
        head_info = await self._git_ops.get_head_info(project_path)
        head_ref = head_info.commit_hash

        async def _query_one(fp: str) -> FileKnowledgeResult:
            try:
                blob_hash = await self._git_ops.get_blob_hash(
                    project_path, head_ref, fp,
                )
                if blob_hash is None:
                    return FileKnowledgeResult(file_path=fp)

                fvid = compute_file_version_id(fp, blob_hash)
                records = await self._qdrant_storage.get_file_points(
                    project_id, fvid,
                )
                descriptions = []
                for r in records:
                    payload = r.payload or {}
                    desc = payload.get("business_description", "")
                    if desc:
                        descriptions.append(desc)

                return FileKnowledgeResult(
                    file_path=fp, descriptions=descriptions,
                )
            except Exception as e:
                logger.warning(
                    "get_file_knowledge_error",
                    file_path=fp,
                    error=str(e),
                )
                return FileKnowledgeResult(file_path=fp, error=str(e))

        results = await asyncio.gather(*[_query_one(fp) for fp in file_paths])
        return list(results)

    # =========================================================================
    # 2.6 Knowledge synchronization — add pre-commit knowledge
    # =========================================================================

    async def add_file_knowledge(
        self,
        project_path: str,
        file_path: str,
        blob_hash: str,
        knowledge_descriptions: List[str],
    ) -> AddKnowledgeResult:
        """Submit pre-commit knowledge descriptions for a single file version.

        Validates token limits, verifies the current worktree blob hash,
        generates embedding vectors, builds PointData, and submits the file
        version to KnowledgeSyncService.

        Args:
            project_path: Absolute path to the Git repository root.
            file_path: Repository-relative file path.
            blob_hash: Git blob hash for the current worktree file content.
            knowledge_descriptions: Complete knowledge descriptions for the file.

        Returns:
            AddKnowledgeResult with succeeded and failed lists.
        """
        assert self._git_ops is not None, "git_ops dependency required"
        assert self._knowledge_sync is not None, "knowledge_sync dependency required"

        project_id = self._read_project_id(project_path)
        token_limit = self._knowledge_submission_config.point_token_limit
        result = AddKnowledgeResult()

        try:
            sync_result = await self._process_add_file(
                project_id,
                project_path,
                file_path,
                blob_hash,
                knowledge_descriptions,
                token_limit,
            )
            result.succeeded.append(
                AddFileSuccess(
                    file_path=file_path,
                    points_count=(
                        sync_result.points_count
                        if sync_result.points_count > 0
                        else len(knowledge_descriptions)
                    ),
                )
            )
        except Exception as e:
            logger.warning(
                "add_file_knowledge_failed",
                file_path=file_path,
                error=str(e),
            )
            result.failed.append(AddFileFailure(file_path=file_path, error=str(e)))

        logger.info(
            "add_file_knowledge_done",
            project_id=project_id,
            succeeded=len(result.succeeded),
            failed=len(result.failed),
        )
        return result

    async def _process_add_file(
        self,
        project_id: str,
        project_path: str,
        file_path: str,
        blob_hash: str,
        descriptions: List[str],
        token_limit: int,
    ):
        """Process a single file for add_file_knowledge.

        Validates tokens, verifies blob hash, generates embeddings, builds
        PointData list, and submits through KnowledgeSyncService.

        Raises on any failure (token limit, file not found, etc).
        """
        assert self._git_ops is not None
        assert self._knowledge_sync is not None

        # Token validation
        over_limit: List[str] = []
        for idx, desc in enumerate(descriptions):
            token_count = await self._embedding.count_tokens(desc)
            if token_count > token_limit:
                over_limit.append(
                    f"[{idx}] {token_count} tokens > {token_limit} limit"
                )
        if over_limit:
            raise ValueError(
                "Token limit exceeded: " + "; ".join(over_limit)
            )

        # Verify the client-provided blob hash against current worktree content
        current_blob_hash = await self._git_ops.get_workdir_blob_hash(
            project_path, file_path,
        )
        if current_blob_hash != blob_hash:
            raise ValueError(
                "blob_hash mismatch for "
                f"{file_path}: client={blob_hash}, current_worktree={current_blob_hash}. "
                "Please recompute the Git blob hash for the latest file content."
            )
        fvid = compute_file_version_id(file_path, blob_hash)

        # Generate embeddings (dense + sparse) for all descriptions
        embeddings = await self._embedding.embed(descriptions, is_query=False)

        # Build PointData list
        file_name = os.path.basename(file_path)
        points: List[PointData] = []
        for desc, emb in zip(descriptions, embeddings):
            points.append(PointData(
                file_path=file_path,
                entity_type="file",
                entity_name=file_name,
                business_description=desc,
                blob_hash=blob_hash,
                dense_vector=emb.dense_vector,
                sparse_vector=emb.sparse_vector,
            ))

        sync_result = await self._knowledge_sync.submit_points(
            project_id=project_id,
            file_path=file_path,
            blob_hash=blob_hash,
            file_version_id=fvid,
            points=points,
            source="client_submit",
            update_latest=True,
            update_memory=True,
            replace_existing_job=True,
        )

        logger.info(
            "add_file_knowledge_submitted",
            project_id=project_id,
            file_path=file_path,
            file_version_id=fvid[:16],
            points=len(points),
            published=sync_result.published,
            skipped_existing=sync_result.skipped_existing,
        )
        return sync_result

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _read_project_id(self, project_path: str) -> str:
        """Read project_id from the ``.git/codebase-rag/project_id`` cache.

        The authoritative project_id is derived from the repository's root
        commit by ``ManagementApiService._resolve_project_id_with_migration``
        at registration time and written into this cache file.  MCP read
        paths consult the cache directly (sync, no git subprocess) so the
        per-call hot path stays fast.  Stale cache values are healed
        automatically the next time the project is registered.

        Raises ProjectNotRegisteredError if the cache file is missing or
        empty (project has never been registered).
        """
        id_file = os.path.join(
            project_path, ".git", _PROJECT_META_DIR, _PROJECT_ID_FILE,
        )

        if not os.path.exists(id_file):
            raise ProjectNotRegisteredError(
                f"No project_id found at '{project_path}'. "
                f"Register the project via the management API first."
            )

        with open(id_file, "r", encoding="utf-8") as f:
            project_id = f.read().strip()

        if not project_id:
            raise ProjectNotRegisteredError(
                f"Empty project_id file at '{id_file}'."
            )

        return project_id
