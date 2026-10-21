from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI

from codebase_rag.services.management_api.models import ManagementApiError, ProjectRecord, ProjectResponse
from codebase_rag.services.management_api.routes import create_router
from codebase_rag.services.management_api.service import ManagementApiService


class RegistryPersistenceTest(unittest.IsolatedAsyncioTestCase):
    def make_service(self, directory):
        service = object.__new__(ManagementApiService)
        service._config_manager = SimpleNamespace(config=SimpleNamespace(management_api=SimpleNamespace(
            registry_path=str(Path(directory) / 'registry.json'),
        )))
        service._projects = {}
        service._git_ops = SimpleNamespace(
            validate_repository=AsyncMock(return_value=SimpleNamespace(is_valid=True)),
            get_head_info=AsyncMock(return_value=SimpleNamespace(branch_name='main', commit_hash='a' * 40)),
        )
        service._resolve_project_id_with_migration = AsyncMock(return_value='new-id')
        service._event_detection = SimpleNamespace(register_project=AsyncMock())
        service._workflow = SimpleNamespace(register_project=Mock(), enqueue_initialization=Mock())
        return service

    async def test_registration_persisted_before_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(tmp)
            service._projects['old'] = ProjectRecord('old', str(Path(tmp)/'old'), 'old')
            def assert_saved(*args):
                records = json.loads(Path(service._config.registry_path).read_text(encoding='utf-8'))
                self.assertEqual([r['project_id'] for r in records], ['old','new-id'])
            service._workflow.enqueue_initialization.side_effect = assert_saved
            response = await service.register_project(str(Path(tmp)/'new'), 'new')
            self.assertEqual(response.project_id, 'new-id')
            service._workflow.enqueue_initialization.assert_called_once()

    async def test_save_faults_fail_registration_without_new_enqueue_or_damaged_registry(self):
        targets = [
            'os.makedirs', 'tempfile.mkstemp', 'json.dump', 'os.replace',
        ]
        for target in targets:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                service = self.make_service(tmp)
                path = Path(service._config.registry_path)
                path.write_text('[]', encoding='utf-8')
                with patch('codebase_rag.services.management_api.service.' + target, side_effect=OSError('write denied')):
                    with self.assertRaisesRegex(ManagementApiError, '无法保存注册表') as ctx:
                        await service.register_project(str(Path(tmp)/'new'), 'new')
                self.assertIn('部分操作可能已执行', str(ctx.exception))
                self.assertIn('相同仓库路径重试', str(ctx.exception))
                service._workflow.enqueue_initialization.assert_not_called()
                self.assertEqual(path.read_text(encoding='utf-8'), '[]')

    async def test_save_defaults_keep_best_effort_and_mkdir_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(tmp)
            with patch('codebase_rag.services.management_api.service.os.replace', side_effect=OSError('replace failed')):
                service._save_registry()
                with self.assertRaises(ManagementApiError):
                    service._save_registry(raise_on_error=True)
            with patch('codebase_rag.services.management_api.service.os.makedirs', side_effect=OSError('mkdir failed')):
                with self.assertRaises(OSError):
                    service._save_registry()

    async def test_rest_registration_retains_error_mapping_and_response_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(tmp)
            service._save_registry = Mock(side_effect=OSError('full disk'))
            service.list_projects = AsyncMock(return_value=[ProjectResponse(
                project_id='old', git_repo_path=tmp, display_name='old', status='registered', knowledge_points=0,
            )])
            app = FastAPI()
            app.include_router(create_router(service))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post('/projects', json={'git_repo_path': tmp})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['detail']['error']['code'], 'INVALID_REQUEST')
                self.assertIn('注册表', response.json()['detail']['error']['message'])
                service._workflow.enqueue_initialization.assert_not_called()
                listing = await client.get('/projects')
                self.assertIn('knowledge_points', listing.json()[0])
                self.assertIn('status', listing.json()[0])

    async def test_batch_records_save_failure_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.make_service(tmp)
            service._save_registry = Mock(side_effect=OSError('full disk'))
            result = await service.batch_register_projects([{'git_repo_path':tmp}])
            self.assertEqual((result.succeeded, result.failed), (0,1))
            self.assertFalse(result.results[0].success)
            self.assertIn('无法保存注册表', result.results[0].error)
