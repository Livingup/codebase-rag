from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from codebase_rag.common.types import SparseVector
from codebase_rag.services.knowledge_sync.models import KnowledgeSyncJob
from codebase_rag.services.knowledge_sync.service import KnowledgeSyncService
from codebase_rag.services.knowledge_sync.store import KnowledgeSyncStore
from codebase_rag.services.qdrant_storage.models import PointData


def point(description='complete description'):
    return PointData('demo.py','file','demo.py',description,'blob',[0.1,0.2],SparseVector(indices=[1],values=[1.0]))


class KnowledgeSyncReceiptTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = KnowledgeSyncStore(self.tmp.name)
        self.qdrant = SimpleNamespace(
            check_existence=AsyncMock(return_value={'version':False}),
            upsert_file_points=AsyncMock(return_value=True),
        )
        self.service = KnowledgeSyncService(SimpleNamespace(), self.store, self.qdrant, SimpleNamespace())
        await self.service.initialize()
        self.service.set_retry_availability_provider(lambda: True)
        self.service.upsert_memory_from_qdrant = AsyncMock()
        self.service.upsert_memory_points = AsyncMock()

    async def submit(self, **kwargs):
        return await self.service.submit_points(
            'project','demo.py','blob','version',[point()], 'client_submit',
            replace_existing_job=True, **kwargs,
        )

    async def test_confirmed_write_does_not_become_failure_when_memory_refresh_fails(self):
        self.service.upsert_memory_from_qdrant.side_effect = RuntimeError('memory unavailable')
        result = await self.submit()
        self.assertTrue(result.published)
        self.assertFalse(result.retry_job_saved)
        self.assertEqual(result.error, '')
        self.assertIsNone(self.store.get_job('project','version'))

    async def test_existing_fast_path_does_not_claim_new_text_was_written(self):
        self.qdrant.check_existence.return_value = {'version':True}
        self.service.upsert_memory_from_qdrant.side_effect = RuntimeError('memory unavailable')
        result = await self.service.try_skip_existing_file_version('project','demo.py','version')
        self.assertTrue(result.skipped_existing)
        self.assertFalse(result.published)
        self.qdrant.upsert_file_points.assert_not_awaited()

    async def test_saved_retry_receipt_is_recoverable_from_fresh_store(self):
        self.qdrant.upsert_file_points.side_effect = RuntimeError('Qdrant unavailable')
        self.service.upsert_memory_points.side_effect = RuntimeError('no memory')
        result = await self.submit()
        self.assertFalse(result.published)
        self.assertTrue(result.retry_job_saved)
        recovered_store = KnowledgeSyncStore(self.tmp.name)
        job = recovered_store.get_job('project','version')
        self.assertEqual(job.status, 'ready')
        self.assertEqual(job.retry_count, 1)
        self.assertEqual(job.points[0], point())
        payload = json.loads((Path(self.tmp.name)/'jobs'/'project'/'version.json').read_text())
        self.assertNotIn('retry_job_saved', payload)
        # A new service object recovers the job without a registry or Git dependency.
        self.qdrant.upsert_file_points.side_effect = None
        recovered = KnowledgeSyncService(SimpleNamespace(), recovered_store, self.qdrant, SimpleNamespace())
        await recovered.initialize()
        self.assertEqual(await recovered.retry_ready_jobs(), (1,0))
        self.assertIsNone(recovered_store.get_job('project','version'))

    async def test_failed_retry_save_cannot_return_saved_evidence(self):
        self.qdrant.upsert_file_points.side_effect = RuntimeError('offline')
        original = self.store.put_job
        def fail_retry(job):
            if job.retry_count:
                raise OSError('disk full')
            return original(job)
        with patch.object(self.store, 'put_job', side_effect=fail_retry):
            with self.assertRaisesRegex(OSError, 'disk full'):
                await self.submit()

    async def test_initial_job_save_failure_never_publishes(self):
        with patch.object(self.store, 'put_job', side_effect=OSError('no space')):
            with self.assertRaisesRegex(OSError, 'no space'):
                await self.submit()
        self.qdrant.check_existence.assert_not_awaited()

    async def test_false_write_ack_is_a_retryable_failure_not_published(self):
        self.qdrant.upsert_file_points.return_value = False
        result = await self.submit()
        self.assertFalse(result.published)
        self.assertTrue(result.retry_job_saved)
        self.assertIn('not confirmed', result.error)

    async def test_cleanup_error_preserves_confirmed_result(self):
        with patch.object(self.store, 'remove_job', side_effect=OSError('cleanup denied')):
            result = await self.submit()
        self.assertTrue(result.published)
        self.assertFalse(result.retry_job_saved)

    async def test_memory_cancellation_is_not_swallowed(self):
        self.service.upsert_memory_from_qdrant.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.submit()

    async def test_retry_rereads_after_lock_instead_of_publishing_stale_snapshot(self):
        self.store.put_job(KnowledgeSyncJob('project','demo.py','blob','version','client_submit',[point('old')]))
        self.store.set_latest('project','demo.py','version')
        lock = self.service._version_locks['project:version']
        await lock.acquire()
        started = asyncio.Event()
        original = self.store.iter_all_jobs
        def scan():
            jobs = original()
            started.set()
            return jobs
        with patch.object(self.store, 'iter_all_jobs', side_effect=scan):
            task = asyncio.create_task(self.service.retry_ready_jobs())
            await asyncio.wait_for(started.wait(), 1)
            self.store.put_job(KnowledgeSyncJob('project','demo.py','blob','version','client_submit',[point('new')]))
            lock.release()
            self.assertEqual(await asyncio.wait_for(task, 1), (1,0))
        self.assertEqual(self.qdrant.upsert_file_points.await_args.args[2][0].business_description, 'new')

    async def test_retry_skips_deleted_or_cancelled_jobs_after_lock(self):
        for cancelled in [False, True]:
            with self.subTest(cancelled=cancelled):
                self.store.put_job(KnowledgeSyncJob('project','demo.py','blob','version','client_submit',[point()]))
                lock = self.service._version_locks['project:version']
                await lock.acquire()
                started = asyncio.Event()
                original = self.store.iter_all_jobs
                def scan():
                    jobs = original()
                    started.set()
                    return jobs
                with patch.object(self.store, 'iter_all_jobs', side_effect=scan):
                    task = asyncio.create_task(self.service.retry_ready_jobs())
                    await asyncio.wait_for(started.wait(), 1)
                    if cancelled:
                        job = self.store.get_job('project','version')
                        job.status = 'cancelled'
                        self.store.put_job(job)
                    else:
                        self.store.remove_job('project','version')
                    lock.release()
                    self.assertEqual(await asyncio.wait_for(task, 1), (0,0))
        self.qdrant.upsert_file_points.assert_not_awaited()

    async def test_stale_version_is_cancelled_without_publishing(self):
        self.store.set_latest('project','demo.py','newer-version')
        result = await self.submit(update_latest=False)
        self.assertTrue(result.cancelled_stale)
        self.assertFalse(result.retry_job_saved)
        self.qdrant.upsert_file_points.assert_not_awaited()

    async def test_retry_availability_requires_initialized_service_and_true_callback(self):
        self.assertTrue(self.service.can_retry_pending())
        for callback in [lambda:False, lambda:1, Mock(side_effect=RuntimeError('probe failed'))]:
            self.service.set_retry_availability_provider(callback)
            self.assertFalse(self.service.can_retry_pending())
        self.service._retry_availability_provider = None
        self.assertFalse(self.service.can_retry_pending())
        self.service.set_retry_availability_provider(lambda:True)
        await self.service.shutdown()
        self.assertFalse(self.service.can_retry_pending())
