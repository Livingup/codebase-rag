from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codebase_rag.services.git_operations.service import (
    compute_file_version_id,
    normalize_repo_relative_path,
)
from codebase_rag.services.knowledge_sync.models import KnowledgeSyncResult
from codebase_rag.services.knowledge_sync.service import KnowledgeSyncService
from codebase_rag.services.mcp_service.service import McpService


class _ConfigManager:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            mcp=SimpleNamespace(
                reranker=SimpleNamespace(recall_limit=20),
                knowledge_submission=SimpleNamespace(point_token_limit=8000),
            )
        )


class _Embedding:
    def __init__(self) -> None:
        self.count_tokens_calls = 0
        self.embed_calls = 0

    async def count_tokens(self, text: str) -> int:
        self.count_tokens_calls += 1
        return len(text.split())

    async def embed(self, texts: list[str], is_query: bool):
        self.embed_calls += 1
        return [
            SimpleNamespace(
                dense_vector=[0.1, 0.2],
                sparse_vector=SimpleNamespace(indices=[1], values=[1.0]),
            )
            for _ in texts
        ]


class _GitOps:
    def __init__(self, worktree_blob_hash: str) -> None:
        self.worktree_blob_hash = worktree_blob_hash
        self.calls: list[tuple[str, str]] = []

    async def get_workdir_blob_hash(self, project_path: str, file_path: str) -> str:
        self.calls.append((project_path, file_path))
        return self.worktree_blob_hash


class _KnowledgeSync:
    def __init__(
        self,
        fast_result: KnowledgeSyncResult | None = None,
        submit_result: KnowledgeSyncResult | None = None,
    ) -> None:
        self.fast_result = fast_result
        self.submit_result = submit_result
        self.try_skip_calls: list[dict] = []
        self.submit_calls: list[dict] = []
        self.retry_available = True

    def can_retry_pending(self) -> bool:
        return self.retry_available

    async def try_skip_existing_file_version(
        self,
        project_id: str,
        file_path: str,
        file_version_id: str,
        old_file_version_id: str | None = None,
        update_latest: bool = True,
        update_memory: bool = True,
    ) -> KnowledgeSyncResult | None:
        self.try_skip_calls.append(
            {
                "project_id": project_id,
                "file_path": file_path,
                "file_version_id": file_version_id,
                "old_file_version_id": old_file_version_id,
                "update_latest": update_latest,
                "update_memory": update_memory,
            }
        )
        return self.fast_result

    async def submit_points(
        self,
        project_id: str,
        file_path: str,
        blob_hash: str,
        file_version_id: str,
        points: list,
        source: str,
        old_file_version_id: str | None = None,
        update_latest: bool = True,
        update_memory: bool = True,
        replace_existing_job: bool = False,
    ) -> KnowledgeSyncResult:
        self.submit_calls.append(
            {
                "project_id": project_id,
                "file_path": file_path,
                "blob_hash": blob_hash,
                "file_version_id": file_version_id,
                "points": points,
                "source": source,
                "old_file_version_id": old_file_version_id,
                "update_latest": update_latest,
                "update_memory": update_memory,
                "replace_existing_job": replace_existing_job,
            }
        )
        if self.submit_result is not None:
            return self.submit_result
        return KnowledgeSyncResult(
            file_version_id=file_version_id,
            points_count=len(points),
            published=True,
        )


def _create_project_path(tmp: str, project_id: str = "project-1") -> str:
    meta_dir = Path(tmp) / ".git" / "codebase-rag"
    meta_dir.mkdir(parents=True)
    (meta_dir / "project_id").write_text(project_id, encoding="utf-8")
    return tmp


class McpAddFileKnowledgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_fast_skips_existing_file_version_without_token_count_or_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_path = _create_project_path(tmp)
            file_path = "src/example.py"
            blob_hash = "abc123"
            file_version_id = compute_file_version_id(file_path, blob_hash)
            fast_result = KnowledgeSyncResult(
                file_version_id=file_version_id,
                points_count=0,
                published=False,
                skipped_existing=True,
            )
            embedding = _Embedding()
            knowledge_sync = _KnowledgeSync(fast_result=fast_result)
            service = McpService(
                config_manager=_ConfigManager(),
                embedding=embedding,
                memory_qdrant=SimpleNamespace(),
                agent=SimpleNamespace(),
                git_ops=_GitOps(blob_hash),
                knowledge_sync=knowledge_sync,
            )

            result = await service.add_file_knowledge(
                project_path, file_path, blob_hash, ["existing knowledge"],
            )

            self.assertEqual(len(result.succeeded), 1)
            self.assertEqual(len(result.failed), 0)
            self.assertEqual(result.succeeded[0].points_count, 1)
            self.assertEqual(result.succeeded[0].outcome, "unchanged")
            self.assertEqual(embedding.count_tokens_calls, 0)
            self.assertEqual(embedding.embed_calls, 0)
            self.assertEqual(len(knowledge_sync.submit_calls), 0)
            self.assertEqual(len(knowledge_sync.try_skip_calls), 1)
            self.assertEqual(knowledge_sync.try_skip_calls[0]["file_version_id"], file_version_id)
            self.assertTrue(knowledge_sync.try_skip_calls[0]["update_latest"])
            self.assertTrue(knowledge_sync.try_skip_calls[0]["update_memory"])

    async def test_blob_hash_mismatch_fails_before_fast_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_path = _create_project_path(tmp)
            file_path = "src/example.py"
            client_blob_hash = "client-hash"
            worktree_blob_hash = "worktree-hash"
            embedding = _Embedding()
            knowledge_sync = _KnowledgeSync(
                fast_result=KnowledgeSyncResult(
                    file_version_id=compute_file_version_id(file_path, client_blob_hash),
                    points_count=0,
                    published=False,
                    skipped_existing=True,
                )
            )
            service = McpService(
                config_manager=_ConfigManager(),
                embedding=embedding,
                memory_qdrant=SimpleNamespace(),
                agent=SimpleNamespace(),
                git_ops=_GitOps(worktree_blob_hash),
                knowledge_sync=knowledge_sync,
            )

            result = await service.add_file_knowledge(
                project_path, file_path, client_blob_hash, ["existing knowledge"],
            )

            self.assertEqual(len(result.succeeded), 0)
            self.assertEqual(len(result.failed), 1)
            self.assertIn("blob_hash mismatch", result.failed[0].error)
            self.assertEqual(embedding.count_tokens_calls, 0)
            self.assertEqual(embedding.embed_calls, 0)
            self.assertEqual(len(knowledge_sync.try_skip_calls), 0)
            self.assertEqual(len(knowledge_sync.submit_calls), 0)

    async def test_non_existing_file_version_uses_normal_submission_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_path = _create_project_path(tmp)
            file_path = "src/example.py"
            blob_hash = "abc123"
            file_version_id = compute_file_version_id(file_path, blob_hash)
            embedding = _Embedding()
            knowledge_sync = _KnowledgeSync()
            service = McpService(
                config_manager=_ConfigManager(),
                embedding=embedding,
                memory_qdrant=SimpleNamespace(),
                agent=SimpleNamespace(),
                git_ops=_GitOps(blob_hash),
                knowledge_sync=knowledge_sync,
            )

            result = await service.add_file_knowledge(
                project_path, file_path, blob_hash, ["new knowledge"],
            )

            self.assertEqual(len(result.succeeded), 1)
            self.assertEqual(len(result.failed), 0)
            self.assertEqual(result.succeeded[0].points_count, 1)
            self.assertEqual(result.succeeded[0].outcome, "accepted")
            self.assertEqual(embedding.count_tokens_calls, 1)
            self.assertEqual(embedding.embed_calls, 1)
            self.assertEqual(len(knowledge_sync.try_skip_calls), 1)
            self.assertEqual(len(knowledge_sync.submit_calls), 1)
            submit_call = knowledge_sync.submit_calls[0]
            self.assertEqual(submit_call["file_version_id"], file_version_id)
            self.assertEqual(submit_call["source"], "client_submit")
            self.assertTrue(submit_call["update_latest"])
            self.assertTrue(submit_call["update_memory"])
            self.assertTrue(submit_call["replace_existing_job"])

    async def test_acceptance_requires_consistent_evidence_and_running_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_path = _create_project_path(tmp)
            fvid = compute_file_version_id("src/example.py", "blob")
            cases = [
                ({"published": True}, True, "accepted"),
                ({"published": False, "skipped_existing": True}, False, "unchanged"),
                ({"published": False, "error": "offline", "retry_job_saved": True}, True, "accepted"),
                ({"published": False, "error": "offline", "retry_job_saved": True}, False, None),
                ({"published": False, "error": "offline"}, True, None),
                ({"published": False}, True, None),
                ({"published": False, "cancelled_stale": True}, True, None),
                ({"published": True, "skipped_existing": True}, True, None),
                ({"published": True, "error": "contradictory"}, True, None),
                ({"published": True, "retry_job_saved": True}, True, None),
            ]
            for fields, retry_available, expected in cases:
                with self.subTest(fields=fields, retry_available=retry_available):
                    sync = _KnowledgeSync(submit_result=KnowledgeSyncResult(
                        file_version_id=fvid, points_count=123, **fields,
                    ))
                    sync.retry_available = retry_available
                    service = McpService(_ConfigManager(), _Embedding(), SimpleNamespace(), SimpleNamespace(),
                                         git_ops=_GitOps("blob"), knowledge_sync=sync)
                    result = await service.add_file_knowledge(project_path,"src/example.py","blob",["description"])
                    if expected:
                        self.assertEqual(result.succeeded[0].outcome, expected)
                        self.assertEqual(result.failed, [])
                    else:
                        self.assertEqual(result.succeeded, [])
                        self.assertEqual(len(result.failed), 1)
            sync = _KnowledgeSync(submit_result=KnowledgeSyncResult('wrong-version',1,True))
            service = McpService(_ConfigManager(), _Embedding(), SimpleNamespace(), SimpleNamespace(),
                                 git_ops=_GitOps('blob'), knowledge_sync=sync)
            result = await service.add_file_knowledge(project_path,'src/example.py','blob',['description'])
            self.assertIn('文件版本不一致', result.failed[0].error)

    async def test_incomplete_embeddings_and_token_limit_never_submit_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_path = _create_project_path(tmp)
            for mode in ['short_embeddings', 'token_limit']:
                with self.subTest(mode=mode):
                    sync, embedding, config = _KnowledgeSync(), _Embedding(), _ConfigManager()
                    if mode == 'short_embeddings':
                        async def incomplete(texts, is_query):
                            return []
                        embedding.embed = incomplete
                    else:
                        config.config.mcp.knowledge_submission.point_token_limit = 1
                    service = McpService(config, embedding, SimpleNamespace(), SimpleNamespace(),
                                         git_ops=_GitOps('blob'), knowledge_sync=sync)
                    result = await service.add_file_knowledge(project_path,'src/example.py','blob',['two words'])
                    self.assertEqual(len(result.failed), 1)
                    self.assertEqual(sync.submit_calls, [])
                    if mode == 'token_limit':
                        self.assertIn('2 tokens > 1 limit', result.failed[0].error)
                        self.assertIn('拆分', result.failed[0].error)
                    else:
                        self.assertIn('处理结果不完整', result.failed[0].error)

    def test_normalize_repo_relative_path(self) -> None:
        repo = "G:/project/agent/agentruntime"
        cases = [
            (r"G:/project/agent/agentruntime/src\agent_runtime\focus_context\inner_brain_prompt.py", "src/agent_runtime/focus_context/inner_brain_prompt.py"),
            (r"G:\project\agent\agentruntime\src\agent_runtime\focus_context\inner_brain_prompt.py", "src/agent_runtime/focus_context/inner_brain_prompt.py"),
            (r"G:/project/agent/agentruntime/src/agent_runtime/focus_context/inner_brain_prompt.py", "src/agent_runtime/focus_context/inner_brain_prompt.py"),
            (r"g:/project/agent/agentruntime/src/agent_runtime/focus_context/inner_brain_prompt.py", "src/agent_runtime/focus_context/inner_brain_prompt.py"),
            (r"src\agent_runtime\focus_context\inner_brain_prompt.py", "src/agent_runtime/focus_context/inner_brain_prompt.py"),
            (r"src/agent_runtime/focus_context/inner_brain_prompt.py", "src/agent_runtime/focus_context/inner_brain_prompt.py"),
            (r"/src/agent_runtime/focus_context/inner_brain_prompt.py", "src/agent_runtime/focus_context/inner_brain_prompt.py"),
        ]
        for inp, expected in cases:
            self.assertEqual(normalize_repo_relative_path(repo, inp), expected)

    async def test_add_file_knowledge_with_absolute_and_mixed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_path = _create_project_path(tmp)
            abs_file_path = os.path.join(project_path, "src\\example.py")
            rel_file_path = "src/example.py"
            blob_hash = "abc123"
            file_version_id = compute_file_version_id(rel_file_path, blob_hash)
            fast_result = KnowledgeSyncResult(
                file_version_id=file_version_id,
                points_count=1,
                published=False,
                skipped_existing=True,
            )
            embedding = _Embedding()
            git_ops = _GitOps(blob_hash)
            knowledge_sync = _KnowledgeSync(fast_result=fast_result)
            service = McpService(
                config_manager=_ConfigManager(),
                embedding=embedding,
                memory_qdrant=SimpleNamespace(),
                agent=SimpleNamespace(),
                git_ops=git_ops,
                knowledge_sync=knowledge_sync,
            )

            result = await service.add_file_knowledge(
                project_path, abs_file_path, blob_hash, ["some knowledge"],
            )

            self.assertEqual(len(result.succeeded), 1)
            self.assertEqual(result.succeeded[0].file_path, os.path.normpath(abs_file_path))
            self.assertEqual(git_ops.calls, [(project_path, "src/example.py")])
            self.assertEqual(knowledge_sync.try_skip_calls[0]["file_path"], "src/example.py")
            self.assertEqual(knowledge_sync.try_skip_calls[0]["file_version_id"], file_version_id)


class _Store:
    def __init__(self) -> None:
        self.latest_calls: list[tuple[str, str, str]] = []
        self.cancel_calls: list[tuple[str, str, str]] = []
        self.remove_calls: list[tuple[str, str]] = []

    def ensure_dir(self) -> None:
        return None

    def set_latest(self, project_id: str, file_path: str, file_version_id: str) -> None:
        self.latest_calls.append((project_id, file_path, file_version_id))

    def cancel_stale_file_jobs(self, project_id: str, file_path: str, latest_file_version_id: str) -> int:
        self.cancel_calls.append((project_id, file_path, latest_file_version_id))
        return 0

    def remove_job(self, project_id: str, file_version_id: str) -> None:
        self.remove_calls.append((project_id, file_version_id))


class _Qdrant:
    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.calls: list[tuple[str, list[str]]] = []

    async def check_existence(self, project_id: str, file_version_ids: list[str]) -> dict[str, bool]:
        self.calls.append((project_id, file_version_ids))
        return {file_version_ids[0]: self.exists}


class _KnowledgeSyncServiceForTest(KnowledgeSyncService):
    def __init__(self, store: _Store, qdrant: _Qdrant) -> None:
        super().__init__(
            config_manager=SimpleNamespace(),
            store=store,
            qdrant_storage=qdrant,
            memory_qdrant=SimpleNamespace(),
        )
        self.upsert_calls: list[tuple[str, str, str | None]] = []

    async def upsert_memory_from_qdrant(
        self,
        project_id: str,
        file_version_id: str,
        old_file_version_id: str | None = None,
    ) -> None:
        self.upsert_calls.append((project_id, file_version_id, old_file_version_id))


class KnowledgeSyncFastSkipTest(unittest.IsolatedAsyncioTestCase):
    async def test_try_skip_existing_updates_latest_and_memory_only_when_existing(self) -> None:
        store = _Store()
        qdrant = _Qdrant(exists=True)
        service = _KnowledgeSyncServiceForTest(store, qdrant)
        await service.initialize()

        result = await service.try_skip_existing_file_version(
            project_id="project-1",
            file_path="src/example.py",
            file_version_id="fvid-1",
            old_file_version_id="old-fvid",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.skipped_existing)
        self.assertEqual(qdrant.calls, [("project-1", ["fvid-1"])])
        self.assertEqual(store.latest_calls, [("project-1", "src/example.py", "fvid-1")])
        self.assertEqual(store.cancel_calls, [("project-1", "src/example.py", "fvid-1")])
        self.assertEqual(store.remove_calls, [("project-1", "fvid-1")])
        self.assertEqual(service.upsert_calls, [("project-1", "fvid-1", "old-fvid")])

    async def test_try_skip_existing_does_not_mutate_latest_on_miss(self) -> None:
        store = _Store()
        qdrant = _Qdrant(exists=False)
        service = _KnowledgeSyncServiceForTest(store, qdrant)
        await service.initialize()

        result = await service.try_skip_existing_file_version(
            project_id="project-1",
            file_path="src/example.py",
            file_version_id="fvid-1",
        )

        self.assertIsNone(result)
        self.assertEqual(qdrant.calls, [("project-1", ["fvid-1"])])
        self.assertEqual(store.latest_calls, [])
        self.assertEqual(store.cancel_calls, [])
        self.assertEqual(store.remove_calls, [])
        self.assertEqual(service.upsert_calls, [])
