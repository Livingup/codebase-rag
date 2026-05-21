from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codebase_rag.services.mcp_service.models import ProjectNotIndexedError
from codebase_rag.services.mcp_service.service import McpService
from codebase_rag.services.memory_qdrant.errors import NoActiveInstanceError
from codebase_rag.services.memory_qdrant.models import SearchResult


class _ConfigManager:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            mcp=SimpleNamespace(
                reranker=SimpleNamespace(recall_limit=20),
                knowledge_submission=SimpleNamespace(point_token_limit=8000),
            )
        )


class _Embedding:
    async def embed(self, texts, is_query: bool):
        return [
            SimpleNamespace(
                dense_vector=[0.1, 0.2],
                sparse_vector=SimpleNamespace(indices=[1], values=[1.0]),
            )
            for _ in texts
        ]


class _Memory:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.active_info_calls = 0
        self.hybrid_search_calls = 0

    async def get_active_instance_info(self, project_id: str):
        self.active_info_calls += 1
        return None

    async def hybrid_search(self, project_id: str, query_dense, query_sparse, top_k: int):
        self.hybrid_search_calls += 1
        if self.error is not None:
            raise self.error
        return [
            SearchResult(
                score=0.9,
                file_version_id="file-version-1",
                file_path="src/example.py",
                entity_type="file",
                entity_name="example.py",
                business_description="example knowledge",
            )
        ]


def _create_project_path(tmp: str, project_id: str = "project-1") -> str:
    meta_dir = Path(tmp) / ".git" / "codebase-rag"
    meta_dir.mkdir(parents=True)
    (meta_dir / "project_id").write_text(project_id, encoding="utf-8")
    return tmp


class McpSearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_delegates_to_hybrid_search_without_active_precheck(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_path = _create_project_path(tmp)
            memory = _Memory()
            service = McpService(
                config_manager=_ConfigManager(),
                embedding=_Embedding(),
                memory_qdrant=memory,
                agent=SimpleNamespace(),
            )

            results = await service.search(project_path, "find example", top_k=10)

            self.assertEqual(memory.active_info_calls, 0)
            self.assertEqual(memory.hybrid_search_calls, 1)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].file_path, os.path.join(project_path, "src/example.py"))

    async def test_search_wraps_lazy_rebuild_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_path = _create_project_path(tmp)
            memory = _Memory(error=NoActiveInstanceError("no active instance"))
            service = McpService(
                config_manager=_ConfigManager(),
                embedding=_Embedding(),
                memory_qdrant=memory,
                agent=SimpleNamespace(),
            )

            with self.assertRaises(ProjectNotIndexedError) as ctx:
                await service.search(project_path, "find example", top_k=10)

            self.assertEqual(memory.active_info_calls, 0)
            self.assertEqual(memory.hybrid_search_calls, 1)
            self.assertIn("lazy rebuild failed", str(ctx.exception))
