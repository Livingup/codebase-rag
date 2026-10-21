from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from codebase_rag.services.management_api.models import InvalidRequestError, ProjectRecord
from codebase_rag.services.management_api.service import ManagementApiService


class RegistrationViewTest(unittest.IsolatedAsyncioTestCase):
    def make_service(self, records=(), task=None):
        service = object.__new__(ManagementApiService)
        service._projects = {r.project_id: r for r in records}
        service._workflow = SimpleNamespace(get_workflow_status=Mock(return_value=SimpleNamespace(
            current_task=task, queued_tasks=9, last_completed=SimpleNamespace(task_type='initialization'),
        )))
        service._memory = Mock(side_effect=AssertionError('must not probe memory'))
        service._get_project_summary = Mock(side_effect=AssertionError('must not read summary'))
        return service

    async def test_registration_snapshot_is_ordered_and_detached(self):
        records = [ProjectRecord('a', 'G:/one', 'first'), ProjectRecord('b', 'G:/two', 'second')]
        service = self.make_service(records)
        result = await service.list_registered_projects()
        self.assertEqual([r.project_id for r in result], ['a', 'b'])
        result[0].display_name = 'changed'
        self.assertEqual(service._projects['a'].display_name, 'first')
        service._get_project_summary.assert_not_called()
        service._workflow.get_workflow_status.assert_not_called()

    async def test_registration_does_not_depend_on_cache_or_directory_existence(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / 'nonexistent-repo')
            service = self.make_service([ProjectRecord('a', missing, 'demo')])
            info = await service.get_project_registration_status(missing)
            self.assertTrue(info.registered)
            self.assertFalse(info.initialization_running)

    async def test_cache_without_registration_does_not_register_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / '.git' / 'codebase-rag' / 'project_id'
            cache.parent.mkdir(parents=True)
            cache.write_text('cached-id', encoding='utf-8')
            service = self.make_service()
            info = await service.get_project_registration_status(tmp)
            self.assertFalse(info.registered)
            service._workflow.get_workflow_status.assert_not_called()

    async def test_only_current_initialization_task_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            for task_type in [None, 'content_commit', 'branch_switch', 'initialization']:
                with self.subTest(task_type=task_type):
                    task = None if task_type is None else SimpleNamespace(task_type=task_type)
                    service = self.make_service([ProjectRecord('a', tmp, 'demo')], task)
                    info = await service.get_project_registration_status(tmp)
                    self.assertEqual(info.initialization_running, task_type == 'initialization')

    async def test_equivalent_paths_and_nested_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = str(Path(tmp) / 'child')
            service = self.make_service([ProjectRecord('a', tmp, 'outer'), ProjectRecord('b', child, 'inner')])
            variants = [tmp + os.sep, os.path.join(tmp, '.'), tmp.replace('\\', '/')]
            if os.name == 'nt':
                variants.append(tmp.swapcase())
            for path in variants:
                with self.subTest(path=path):
                    info = await service.get_project_registration_status(path)
                    self.assertEqual(info.git_repo_path, tmp)
            self.assertEqual((await service.get_project_registration_status(child)).git_repo_path, child)
            self.assertFalse((await service.get_project_registration_status(str(Path(child) / 'other'))).registered)

    async def test_duplicate_registration_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service([ProjectRecord('a', tmp, 'a'), ProjectRecord('b', tmp + os.sep, 'b')])
            with self.assertRaisesRegex(InvalidRequestError, '多个登记记录'):
                await service.get_project_registration_status(tmp)

    async def test_invalid_paths_and_status_failures_are_not_unregistered(self):
        service = self.make_service()
        for path in ['', ' ', None, 23, 'relative/path']:
            with self.subTest(path=path), self.assertRaises(InvalidRequestError):
                await service.get_project_registration_status(path)
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service([ProjectRecord('a', tmp, 'a')])
            service._workflow.get_workflow_status.side_effect = RuntimeError('status unavailable')
            with self.assertRaisesRegex(RuntimeError, 'status unavailable'):
                await service.get_project_registration_status(tmp)
