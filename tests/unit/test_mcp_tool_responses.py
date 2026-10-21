from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult

from codebase_rag.services.context_engine.service import ContextEngineService
from codebase_rag.services.management_api.models import ProjectRecord, ProjectRegistrationStatus
from codebase_rag.services.mcp_service.models import (
    AddFileFailure, AddFileSuccess, AddKnowledgeResult, FileKnowledgeResult, McpServiceError, SearchResultPoint,
 )
from codebase_rag.services.mcp_service.server import (
    create_mcp_server, handle_add_file_knowledge, handle_call_tool, handle_codebase_search,
    handle_context_analysis, handle_get_file_knowledge, handle_list_projects, handle_project_status,
 )


class McpToolResponsesTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_empty_and_nonempty_exact_text(self):
        api = SimpleNamespace(list_registered_projects=AsyncMock(return_value=[]))
        self.assertEqual((await handle_list_projects(api))[0].text, '未注册任何 Git 仓库。')
        api.list_registered_projects.return_value = [
            ProjectRecord('a','G:/project/demo','demo'), ProjectRecord('b','G:/project/sample','sample'),
        ]
        api.list_registered_projects.reset_mock()
        result = await handle_call_tool('list_projects', {}, SimpleNamespace(), api)
        self.assertEqual(result[0].text, '已登记 Git 仓库（2）：\n- demo：G:/project/demo\n- sample：G:/project/sample')
        api.list_registered_projects.assert_awaited_once_with()
        for label in ['Points','Status','Branch']:
            self.assertNotIn(label, result[0].text)

    async def test_project_status_registered_initializing_and_unregistered(self):
        for registered, running in [(True,False),(True,True),(False,False)]:
            with self.subTest(registered=registered, running=running):
                api = SimpleNamespace(get_project_registration_status=AsyncMock(return_value=ProjectRegistrationStatus(
                    'G:/project/demo', registered, running,
                )))
                result = await handle_call_tool('project_status', {'git_repo_path':'G:/project/demo'}, SimpleNamespace(), api)
                text = result[0].text
                expected = '仓库：G:/project/demo\n登记状态：' + ('已登记' if registered else '未登记')
                if running:
                    expected += '\n初始化进行中。'
                if not registered:
                    expected += '\n本服务尚未登记此仓库。'
                self.assertEqual(text, expected)
                api.get_project_registration_status.assert_awaited_once_with('G:/project/demo')
                self.assertNotIn('ready',text)
                self.assertNotIn('Points',text)
        api = SimpleNamespace(get_project_registration_status=AsyncMock())
        for value in [None, '', 123, ' ']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                await handle_project_status({'git_repo_path':value}, api)
        api.get_project_registration_status.assert_not_awaited()

    async def test_get_knowledge_preserves_actual_count_content_and_file_errors(self):
        service = SimpleNamespace(get_file_knowledge=AsyncMock(return_value=[
            FileKnowledgeResult('G:/demo/a.py',['first','second']),
            FileKnowledgeResult('G:/demo/b.py'),
            FileKnowledgeResult('G:/demo/c.py', error='Git read failed'),
        ]))
        text = (await handle_get_file_knowledge({'git_repo_path':'G:/demo','file_paths':['a.py','b.py','c.py']},service))[0].text
        self.assertIn('a.py（返回 2 条知识说明；当前已提交版本）',text)
        self.assertIn('### [1]\nfirst',text)
        self.assertIn('### [2]\nsecond',text)
        self.assertIn('b.py（返回 0 条知识说明；当前已提交版本）',text)
        self.assertIn('本次未返回该文件当前已提交版本的可读知识说明。',text)
        self.assertIn('## G:/demo/c.py\n\n⚠ 查询失败：Git read failed',text)

    def add_args(self):
        return {'git_repo_path':'G:/demo','file_path':'G:/demo/a.py','blob_hash':'hash','knowledge_descriptions':['description']}

    async def test_add_accepted_unchanged_and_failure_results(self):
        for outcome, expected in [
            ('accepted','知识提交已受理。\n文件：G:/demo/a.py\n无需重复提交；不代表立即可检索。'),
            ('unchanged','该文件版本已有知识，本次未覆盖已有内容。\n文件：G:/demo/a.py'),
        ]:
            with self.subTest(outcome=outcome):
                service = SimpleNamespace(add_file_knowledge=AsyncMock(return_value=AddKnowledgeResult(
                    succeeded=[AddFileSuccess('G:/demo/a.py',99,outcome)],
                )))
                result = await handle_add_file_knowledge(self.add_args(),service)
                self.assertIsInstance(result,CallToolResult)
                self.assertFalse(result.isError)
                self.assertIsNone(result.structuredContent)
                self.assertEqual(result.content[0].text,expected)
        service = SimpleNamespace(add_file_knowledge=AsyncMock(return_value=AddKnowledgeResult(
            failed=[AddFileFailure('G:/demo/a.py','blob_hash mismatch; recompute')],
        )))
        result = await handle_call_tool('add_file_knowledge',self.add_args(),service,SimpleNamespace())
        self.assertTrue(result.isError)
        self.assertEqual(result.content[0].text,'提交失败：blob_hash mismatch; recompute\n文件：G:/demo/a.py')

    async def test_invalid_single_file_result_shapes_are_not_success(self):
        cases = [
            AddKnowledgeResult(),
            AddKnowledgeResult(succeeded=[AddFileSuccess('a',1),AddFileSuccess('b',1)]),
            AddKnowledgeResult(succeeded=[AddFileSuccess('a',1)],failed=[AddFileFailure('a','error')]),
            AddKnowledgeResult(succeeded=[AddFileSuccess('a',1,'unknown')]),
        ]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(McpServiceError):
                await handle_add_file_knowledge(self.add_args(),SimpleNamespace(add_file_knowledge=AsyncMock(return_value=result)))

    async def test_add_parameter_failures_do_not_call_service(self):
        service = SimpleNamespace(add_file_knowledge=AsyncMock())
        for changes in [
            {'git_repo_path':''}, {'file_path':''}, {'blob_hash':''},
            {'knowledge_descriptions':[]}, {'knowledge_descriptions':[' ']},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await handle_add_file_knowledge({**self.add_args(),**changes},service)
        service.add_file_knowledge.assert_not_awaited()

    async def test_sdk_preserves_explicit_add_error_and_does_not_change_list_error_protocol(self):
        service = SimpleNamespace(add_file_knowledge=AsyncMock(return_value=AddKnowledgeResult(
            failed=[AddFileFailure('G:/demo/a.py','version mismatch')],
        )))
        api = SimpleNamespace(list_registered_projects=AsyncMock(return_value=[]))
        server = create_mcp_server('test',service,api)
        response = await server.request_handlers[CallToolRequest](CallToolRequest(
            method='tools/call', params=CallToolRequestParams(name='add_file_knowledge',arguments=self.add_args()),
        ))
        self.assertTrue(response.root.isError)
        self.assertIn('提交失败',response.root.content[0].text)
        listing = await server.request_handlers[CallToolRequest](CallToolRequest(
            method='tools/call', params=CallToolRequestParams(name='list_projects',arguments={}),
        ))
        self.assertFalse(listing.root.isError)
        self.assertEqual(listing.root.content[0].text,'未注册任何 Git 仓库。')

    async def test_search_keeps_order_count_and_errors_without_scores(self):
        async def search(project_path,query,top_k):
            if project_path.endswith('bad'):
                raise RuntimeError('search failed')
            return [SearchResultPoint(0.9,'first.py','first'),SearchResultPoint(0.001,'second.py','second')]
        result = await handle_codebase_search({'queries':[{'query':'task','git_repo_paths':['G:/good','G:/bad']}]},SimpleNamespace(search=search))
        text = result[0].text
        self.assertIn('本次返回 2 条',text)
        self.assertIn('### [1] first.py\n\nfirst',text)
        self.assertLess(text.index('first.py'),text.index('second.py'))
        self.assertIn('⚠ 查询失败: search failed',text)
        self.assertNotIn('score:',text)
        result = await handle_codebase_search({'queries':[{'query':'task','git_repo_paths':['G:/repo']}]},SimpleNamespace(search=AsyncMock(return_value=[])))
        self.assertEqual(result[0].text,'本次检索未返回结果。\n查询："task"\n这不表示源码中不存在相关实现。')

    async def test_context_handler_preserves_report_verbatim(self):
        report = '# Report\nInspected findings 12; source.py:42\nLimitation: partial coverage'
        engine = SimpleNamespace(context_analysis=AsyncMock(return_value=report))
        result = await handle_context_analysis({'git_repo_paths':['G:/demo'],'task_description':'task'},engine)
        self.assertEqual(result[0].text,report)


class ContextResultTextTest(unittest.IsolatedAsyncioTestCase):
    def engine(self, run):
        engine = object.__new__(ContextEngineService)
        engine._ensure_initialized = AsyncMock()
        engine._ripgrep_available = True
        engine._agent_names = ['test-agent']
        engine._agent = SimpleNamespace(run_agent=run)
        engine._config_manager = SimpleNamespace(config=SimpleNamespace(context_engine=SimpleNamespace()))
        return engine

    async def test_failed_and_incomplete_reports_do_not_claim_analyzed_count(self):
        failed = self.engine(AsyncMock(side_effect=RuntimeError('model failed')))
        self.assertEqual(await failed.context_analysis(['G:/demo'],'task'),'⚠️ 分析过程异常：model failed')
        incomplete = self.engine(AsyncMock())
        self.assertEqual(await incomplete.context_analysis(['G:/demo'],'task'),'⚠️ 分析未完成：未生成最终报告，不能据此确认任务结论。')

    async def test_success_report_and_missing_rg_are_preserved(self):
        report = '# Findings\n3 actual facts; file.py:8'
        async def run(**kwargs):
            await kwargs['tool_handlers']['submit_report'](report)
        engine = self.engine(run)
        self.assertEqual(await engine.context_analysis(['G:/demo'],'task'),report)
        engine._ripgrep_available = False
        self.assertEqual(await engine.context_analysis(['G:/demo'],'task'),'错误：ripgrep (rg) 未安装，无法执行代码搜索。请安装 ripgrep 后重试。')
