from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codebase_rag.services.workflow.service import WorkflowOrchestratorService
from codebase_rag.services.workflow.task_queue import ProjectTaskQueue, WorkflowTask


class WorkflowTaskStatusTest(unittest.IsolatedAsyncioTestCase):
    def make_service(self):
        service = object.__new__(WorkflowOrchestratorService)
        # repo_key → project_id; the test's copy lives at key 'project'
        service._project_repos = {'project': 'project'}
        service._current_tasks = {}
        service._last_completed = {}
        service._task_queue = ProjectTaskQueue()
        service._task_queue.set_executor(service._execute_task)
        service._initialized = True
        service._comp_task = None
        service._config_manager = SimpleNamespace(config=SimpleNamespace(workflow=SimpleNamespace(
            compensation=SimpleNamespace(enabled=True),
            initialization=SimpleNamespace(mode='incremental'),
        )))
        return service

    async def test_dispatcher_reports_incremental_initialization_while_running(self):
        service = self.make_service()
        entered, release = asyncio.Event(), asyncio.Event()
        async def incremental(*args, **kwargs):
            entered.set()
            await release.wait()
        service._incremental_initialization = incremental
        task = asyncio.create_task(service._execute_task(WorkflowTask(
            'initialization','project', {'branch_name':'main','commit_hash':'commit'},
        )))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(service.get_workflow_status('project').current_task.task_type, 'initialization')
        finally:
            release.set()
            await task
        self.assertIsNone(service.get_workflow_status('project').current_task)

    async def test_other_tasks_and_failed_initialization_are_not_success_evidence(self):
        service = self.make_service()
        entered, release = asyncio.Event(), asyncio.Event()
        async def commit(**kwargs):
            entered.set()
            await release.wait()
        service.workflow_content_commit = commit
        task = asyncio.create_task(service._execute_task(WorkflowTask(
            'content_commit','project', {'branch_name':'main','old_commit':'a','new_commit':'b'},
        )))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(service.get_workflow_status('project').current_task.task_type, 'content_commit')
        finally:
            release.set()
            await task
        service._incremental_initialization = AsyncMock(side_effect=RuntimeError('failed init'))
        with self.assertRaisesRegex(RuntimeError, 'failed init'):
            await service._execute_task(WorkflowTask('initialization','project',{'branch_name':'main','commit_hash':'c'}))
        self.assertIsNone(service.get_workflow_status('project').current_task)

    async def test_compensation_probe_observes_task_lifecycle(self):
        service = self.make_service()
        self.assertFalse(service.is_compensation_running())
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        service._comp_task = task
        self.assertTrue(service.is_compensation_running())
        service._initialized = False
        self.assertFalse(service.is_compensation_running())
        service._initialized = True
        service._config.compensation.enabled = False
        self.assertFalse(service.is_compensation_running())
        service._config.compensation.enabled = True
        task.cancel()
        self.assertFalse(service.is_compensation_running())
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(service.is_compensation_running())

    async def test_composition_root_wires_existing_instances_without_initializing_services(self):
        import codebase_rag.__main__ as main
        names = [
            'EmbeddingService','GitOperationsService','QdrantStorageService','MemoryQdrantService',
            'McpClientService','TokenCounter','AgentService','EventDetectionService','KnowledgeSyncStore',
            'KnowledgeSyncService','WorkflowOrchestratorService','ManagementApiService','McpService','ContextEngineService',
        ]
        with ExitStack() as stack:
            mocks = {name:stack.enter_context(patch.object(main,name)) for name in names}
            config = SimpleNamespace(config=SimpleNamespace(workflow=SimpleNamespace(data_dir='unused')))
            container = main.ServiceContainer(config)
            container.knowledge_sync.set_retry_availability_provider.assert_called_once_with(
                container.workflow.is_compensation_running,
            )
            mocks['KnowledgeSyncService'].assert_called_once()
            mocks['WorkflowOrchestratorService'].assert_called_once()
