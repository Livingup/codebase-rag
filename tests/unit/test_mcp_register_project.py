from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codebase_rag.services.management_api.models import (
    ManagementApiError,
    ProjectResponse,
)
from codebase_rag.services.mcp_service.server import (
    TOOL_DEFINITIONS,
    handle_call_tool,
    handle_register_project,
)


class _ManagementApi:
    def __init__(self, response: ProjectResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def register_project(self, git_repo_path: str, display_name: str = "") -> ProjectResponse:
        self.calls.append(
            {
                "git_repo_path": git_repo_path,
                "display_name": display_name,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _sample_project(**overrides) -> ProjectResponse:
    data = {
        "project_id": "proj-123",
        "git_repo_path": "G:/project/demo",
        "display_name": "demo",
        "status": "registered",
        "branch": "main",
        "commit_hash": "abcdef12",
        "knowledge_points": 0,
    }
    data.update(overrides)
    return ProjectResponse(**data)


class McpRegisterProjectTest(unittest.IsolatedAsyncioTestCase):
    def test_tool_definition_contains_mandatory_call_boundary(self) -> None:
        tool = next(t for t in TOOL_DEFINITIONS if t.name == "register_project")
        self.assertIn("仅当用户明确要求", tool.description)
        self.assertIn("禁止未经用户同意私自调用", tool.description)
        self.assertIn("禁止因为“后续搜索更方便”", tool.description)
        self.assertEqual(tool.inputSchema["required"], ["git_repo_path"])
        self.assertIn("git_repo_path", tool.inputSchema["properties"])
        self.assertIn("display_name", tool.inputSchema["properties"])

    async def test_success_path_returns_key_fields_and_async_init_note(self) -> None:
        api = _ManagementApi(response=_sample_project())
        contents = await handle_register_project(
            {
                "git_repo_path": "G:/project/demo",
                "display_name": "demo",
            },
            api,  # type: ignore[arg-type]
        )
        text = contents[0].text
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0]["git_repo_path"], "G:/project/demo")
        self.assertEqual(api.calls[0]["display_name"], "demo")
        self.assertIn("project_id: proj-123", text)
        self.assertIn("display_name: demo", text)
        self.assertIn("git_repo_path: G:/project/demo", text)
        self.assertIn("status: registered", text)
        self.assertIn("branch: main", text)
        self.assertIn("commit_hash: abcdef12", text)
        self.assertIn("knowledge_points: 0", text)
        self.assertIn("入队异步初始化", text)
        self.assertIn("不代表索引已 ready", text)

    async def test_missing_git_repo_path_raises_value_error(self) -> None:
        api = _ManagementApi(response=_sample_project())
        with self.assertRaises(ValueError) as ctx:
            await handle_register_project({}, api)  # type: ignore[arg-type]
        self.assertIn("git_repo_path", str(ctx.exception))
        self.assertEqual(api.calls, [])

    async def test_management_api_error_is_mapped_to_value_error(self) -> None:
        api = _ManagementApi(error=ManagementApiError("Invalid Git repository: missing .git"))
        with self.assertRaises(ValueError) as ctx:
            await handle_register_project(
                {"git_repo_path": "G:/not-a-repo"},
                api,  # type: ignore[arg-type]
            )
        self.assertIn("Invalid Git repository", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, ManagementApiError)

    async def test_handle_call_tool_dispatches_register_project(self) -> None:
        api = _ManagementApi(response=_sample_project(display_name="from-dispatch"))
        contents = await handle_call_tool(
            name="register_project",
            arguments={
                "git_repo_path": "G:/project/demo",
                "display_name": "from-dispatch",
            },
            mcp_service=SimpleNamespace(),  # type: ignore[arg-type]
            management_api=api,  # type: ignore[arg-type]
        )
        self.assertIn("display_name: from-dispatch", contents[0].text)
        self.assertEqual(api.calls[0]["display_name"], "from-dispatch")

    async def test_empty_display_name_is_forwarded_as_empty_string(self) -> None:
        api = _ManagementApi(response=_sample_project(display_name="fallback-name"))
        await handle_register_project(
            {"git_repo_path": "G:/project/demo"},
            api,  # type: ignore[arg-type]
        )
        self.assertEqual(api.calls[0]["display_name"], "")

    async def test_explicit_display_name_is_forwarded(self) -> None:
        api = _ManagementApi(response=_sample_project(display_name="custom"))
        await handle_register_project(
            {
                "git_repo_path": "G:/project/demo",
                "display_name": "custom",
            },
            api,  # type: ignore[arg-type]
        )
        self.assertEqual(api.calls[0]["display_name"], "custom")


if __name__ == "__main__":
    unittest.main()
