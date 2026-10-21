from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codebase_rag.services.git_operations.errors import GitCommandError, GitTimeoutError
from codebase_rag.services.git_operations.service import GitOperationsService
from codebase_rag.services.mcp_service.service import McpService


class GitBlobReadStrictTest(unittest.IsolatedAsyncioTestCase):
    async def test_strict_valid_and_absent_blob(self):
        service = object.__new__(GitOperationsService)
        with tempfile.TemporaryDirectory() as repo:
            for size in [40, 64]:
                blob = 'a' * size
                service._run_git = AsyncMock(return_value=f'100644 blob {blob}\tsrc/demo.py\n')
                self.assertEqual(await service.get_blob_hash(repo, 'HEAD', 'src/demo.py', strict=True), blob)
            service._run_git = AsyncMock(return_value='')
            self.assertIsNone(await service.get_blob_hash(repo, 'HEAD', 'missing.py', strict=True))

    async def test_strict_errors_not_converted_to_absence(self):
        service = object.__new__(GitOperationsService)
        with tempfile.TemporaryDirectory() as repo:
            for error in [GitCommandError('git ls-tree', 128, 'bad ref'), GitTimeoutError('git', 1)]:
                service._run_git = AsyncMock(side_effect=error)
                with self.subTest(error=error), self.assertRaises(type(error)):
                    await service.get_blob_hash(repo, 'HEAD', 'demo.py', strict=True)
            for output in [
                'unparseable', '100644 blob invalid\tdemo.py',
                f'040000 tree {"a" * 40}\tdir', f'160000 commit {"a" * 40}\tsubmodule',
                f'100644 blob {"a" * 40}\tone\n100644 blob {"b" * 40}\ttwo',
            ]:
                service._run_git = AsyncMock(return_value=output)
                with self.subTest(output=output), self.assertRaises(GitCommandError):
                    await service.get_blob_hash(repo, 'HEAD', 'demo.py', strict=True)

    async def test_default_compatibility(self):
        service = object.__new__(GitOperationsService)
        with tempfile.TemporaryDirectory() as repo:
            service._run_git = AsyncMock(side_effect=GitCommandError('git', 1, 'failure'))
            self.assertIsNone(await service.get_blob_hash(repo, 'HEAD', 'demo.py'))
            service._run_git = AsyncMock(return_value='invalid')
            self.assertIsNone(await service.get_blob_hash(repo, 'HEAD', 'demo.py'))

    async def test_knowledge_query_separates_absence_and_per_file_git_failure(self):
        service = object.__new__(McpService)
        service._read_project_id = lambda path: 'project'
        git = object.__new__(GitOperationsService)
        git.get_head_info = AsyncMock(return_value=SimpleNamespace(commit_hash='head'))
        async def run_git(repo, args):
            if args[-1] == 'broken.py':
                raise GitCommandError('git ls-tree', 1, 'read failed')
            if args[-1] == 'missing.py':
                return ''
            return f'100644 blob {"a" * 40}\tgood.py'
        git._run_git = run_git
        service._git_ops = git
        service._qdrant_storage = SimpleNamespace(get_file_points=AsyncMock(return_value=[
            SimpleNamespace(payload={'business_description':'valid description'}),
        ]))
        with tempfile.TemporaryDirectory() as repo:
            result = await service.get_file_knowledge(repo, ['broken.py','missing.py','good.py'])
            self.assertIn('read failed', result[0].error)
            self.assertEqual(result[1].descriptions, [])
            self.assertIsNone(result[1].error)
            self.assertEqual(result[2].descriptions, ['valid description'])
            service._qdrant_storage.get_file_points.assert_awaited_once()
            git.get_head_info.side_effect = RuntimeError('HEAD failed')
            with self.assertRaisesRegex(RuntimeError, 'HEAD failed'):
                await service.get_file_knowledge(repo, ['good.py'])
