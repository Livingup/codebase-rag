from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codebase_rag.common.types import SparseVector
from codebase_rag.services.qdrant_storage.errors import QdrantOperationError
from codebase_rag.services.qdrant_storage.models import PointData
from codebase_rag.services.qdrant_storage.service import QdrantStorageService


class QdrantWriteAckTest(unittest.IsolatedAsyncioTestCase):
    def make_service(self):
        service = object.__new__(QdrantStorageService)
        service._initialized = True
        service._semaphore = asyncio.Semaphore(1)
        service._client = SimpleNamespace(upsert=AsyncMock(return_value=SimpleNamespace(status='completed')))
        service._config_manager = SimpleNamespace(config=SimpleNamespace(qdrant_storage=SimpleNamespace(
            collection=SimpleNamespace(name='test', dense_vector=SimpleNamespace(name='dense'), sparse_vector=SimpleNamespace(name='sparse')),
            upsert_batch_size=2,
        )))
        return service

    def points(self):
        return [PointData('demo.py','file','demo.py',f'description {i}','blob',[0.1,0.2],SparseVector(indices=[1],values=[1.0])) for i in range(3)]

    async def test_all_batches_wait_for_ack_and_preserve_payload(self):
        service = self.make_service()
        self.assertTrue(await service.upsert_file_points('project', 'version', self.points()))
        calls = service._client.upsert.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual([len(c.kwargs['points']) for c in calls], [2,1])
        self.assertTrue(all(c.kwargs['wait'] is True for c in calls))
        self.assertEqual(calls[0].kwargs['points'][0].payload['project_id'], 'project')

    async def test_any_batch_error_is_not_success(self):
        service = self.make_service()
        service._client.upsert.side_effect = [SimpleNamespace(status='completed'), RuntimeError('second batch failed')]
        with self.assertRaisesRegex(QdrantOperationError, 'second batch failed'):
            await service.upsert_file_points('project', 'version', self.points())
