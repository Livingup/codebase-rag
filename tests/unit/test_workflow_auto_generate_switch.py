from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from codebase_rag.config.schema import ContentCommitConfig
from codebase_rag.services.git_operations.models import ChangedFiles, ChangedFilesResult, FileEntry, ModifiedFileEntry
from codebase_rag.services.knowledge_sync.models import KnowledgeSyncResult
from codebase_rag.services.workflow.service import WorkflowOrchestratorService


class WorkflowAutoGenerateSwitchTest(unittest.IsolatedAsyncioTestCase):
    def test_schema_defaults(self):
        cfg_default = ContentCommitConfig()
        self.assertTrue(cfg_default.auto_generate)

        cfg_false = ContentCommitConfig(auto_generate=False)
        self.assertFalse(cfg_false.auto_generate)

    def _make_service(self, auto_generate: bool = True):
        service = object.__new__(WorkflowOrchestratorService)
        service._project_repos = {"/repo": "project_1"}
        service._initialized = True
        service._comp_task = None
        service._comp_consecutive_failures = 0
        service._comp_last_round_at = 0.0
        service._retry_queues = {}

        # Config mock
        content_commit_cfg = SimpleNamespace(
            auto_generate=auto_generate,
            max_file_concurrency=5,
            point_token_limit=8192,
        )
        service._config_manager = SimpleNamespace(
            config=SimpleNamespace(
                workflow=SimpleNamespace(
                    data_dir="/tmp/test",
                    content_commit=content_commit_cfg,
                    compensation=SimpleNamespace(enabled=False),
                    initialization=SimpleNamespace(mode="incremental"),
                )
            )
        )

        # Collaborator mocks
        service._knowledge_sync = SimpleNamespace(
            mark_latest_file_version=MagicMock(),
            is_stale_file_version=MagicMock(return_value=False),
            has_ready_job=MagicMock(return_value=False),
            ensure_published=AsyncMock(),
        )
        service._qdrant = SimpleNamespace(
            check_existence=AsyncMock(return_value={}),
            get_file_points=AsyncMock(return_value=[]),
        )
        service._git_ops = SimpleNamespace(
            get_file_bytes=AsyncMock(),
            detect_file_modality=MagicMock(return_value=("text", None)),
            get_changed_files=AsyncMock(),
        )
        service._memory = SimpleNamespace(
            get_active_instance_info=AsyncMock(return_value=None),
            get_cached_instance=AsyncMock(return_value=None),
            delete_points_by_file_version=AsyncMock(),
            update_cache_commit=AsyncMock(),
        )
        service._embedding = SimpleNamespace(
            embed=AsyncMock(),
        )
        service._delete_from_memory = AsyncMock()
        service._upsert_to_memory_from_qdrant = AsyncMock()

        return service

    async def test_auto_generate_disabled_skips_agent_and_deletes_old_memory(self):
        service = self._make_service(auto_generate=False)

        pts, skipped, agents, cache_hit = await service._process_file_with_agent(
            project_id="project_1",
            repo_path="/repo",
            file_path="src/new_module.py",
            blob_hash="blob_new_123",
            file_version_id="fvid_new_123",
            is_new=True,
            old_file_version_id=None,
            repo_key="/repo",
            sync_source="commit_event",
        )

        self.assertEqual(pts, 0)
        self.assertTrue(skipped)
        self.assertEqual(agents, 0)
        self.assertFalse(cache_hit)
        service._git_ops.get_file_bytes.assert_not_called()
        service._delete_from_memory.assert_not_called()

        # Test modified file: old_file_version_id should be removed from memory
        pts_mod, skipped_mod, agents_mod, _ = await service._process_file_with_agent(
            project_id="project_1",
            repo_path="/repo",
            file_path="src/existing_module.py",
            blob_hash="blob_mod_456",
            file_version_id="fvid_mod_456",
            is_new=False,
            old_file_version_id="old_fvid_000",
            repo_key="/repo",
            sync_source="commit_event",
        )

        self.assertEqual(pts_mod, 0)
        self.assertTrue(skipped_mod)
        self.assertEqual(agents_mod, 0)
        service._git_ops.get_file_bytes.assert_not_called()
        service._delete_from_memory.assert_called_once_with(
            "project_1", "/repo", "old_fvid_000"
        )

    async def test_auto_generate_disabled_still_publishes_ready_job(self):
        service = self._make_service(auto_generate=False)

        service._knowledge_sync.has_ready_job.return_value = True
        service._knowledge_sync.ensure_published.return_value = KnowledgeSyncResult(
            file_version_id="fvid_ready_789",
            points_count=3,
            published=True,
            skipped_existing=False,
            cancelled_stale=False,
        )

        pts, skipped, agents, cache_hit = await service._process_file_with_agent(
            project_id="project_1",
            repo_path="/repo",
            file_path="src/ready_module.py",
            blob_hash="blob_ready_789",
            file_version_id="fvid_ready_789",
            is_new=True,
            repo_key="/repo",
            sync_source="commit_event",
        )

        self.assertEqual(pts, 3)
        self.assertFalse(skipped)
        self.assertEqual(agents, 0)
        self.assertTrue(cache_hit)
        service._knowledge_sync.ensure_published.assert_called_once()
        service._git_ops.get_file_bytes.assert_not_called()

    async def test_auto_generate_disabled_still_handles_existing_in_qdrant(self):
        service = self._make_service(auto_generate=False)

        service._qdrant.check_existence.return_value = {"fvid_exist_111": True}

        pts, skipped, agents, cache_hit = await service._process_file_with_agent(
            project_id="project_1",
            repo_path="/repo",
            file_path="src/exist_module.py",
            blob_hash="blob_exist_111",
            file_version_id="fvid_exist_111",
            is_new=False,
            old_file_version_id="old_fvid_prev",
            repo_key="/repo",
            sync_source="commit_event",
        )

        self.assertEqual(pts, 0)
        self.assertTrue(skipped)
        self.assertEqual(agents, 0)
        self.assertFalse(cache_hit)
        service._upsert_to_memory_from_qdrant.assert_called_once_with(
            "project_1", "/repo", "fvid_exist_111"
        )
        service._delete_from_memory.assert_called_once_with(
            "project_1", "/repo", "old_fvid_prev"
        )
        service._git_ops.get_file_bytes.assert_not_called()

    async def test_workflow_content_commit_full_flow_with_auto_generate_disabled(self):
        service = self._make_service(auto_generate=False)

        # Mock diff: 1 added, 1 modified, 1 deleted
        service._git_ops.get_changed_files.return_value = ChangedFilesResult(
            changes=ChangedFiles(
                added=[FileEntry("src/new.py", "b_new", "fv_new")],
                modified=[ModifiedFileEntry("src/mod.py", "b_old", "b_new2", "fv_old", "fv_new2")],
                deleted=[FileEntry("src/del.py", "b_del", "fv_del")],
                renamed=[],
            ),
            excluded_count=0,
        )

        result = await service.workflow_content_commit(
            project_id="project_1",
            repo_key="/repo",
            branch_name="main",
            commit_hash="commit_hash_2222",
            previous_commit="commit_hash_1111",
        )

        # Added and modified files are skipped cleanly because auto_generate is False
        self.assertEqual(result.added_files, 0)
        self.assertEqual(result.modified_files, 0)
        self.assertEqual(result.deleted_files, 1)
        self.assertEqual(result.skipped_files, 2)
        self.assertEqual(result.total_agent_calls, 0)
        self.assertEqual(result.total_points_written, 0)
        self.assertEqual(len(result.failed_files), 0)

        # Deleted file deleted from memory
        service._delete_from_memory.assert_any_call("project_1", "/repo", "fv_del")
        # Modified file old version deleted from memory
        service._delete_from_memory.assert_any_call("project_1", "/repo", "fv_old")

        # Memory cache key updated
        service._memory.update_cache_commit.assert_called_once_with(
            "/repo", "main", "commit_hash_1111", "commit_hash_2222"
        )
