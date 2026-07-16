"""MCP Server — registers tools and resources, handles MCP protocol.

Uses the official Python MCP SDK to expose codebase_search, project_status,
list_projects, register_project and knowledge tools via Streamable HTTP or SSE
transport.

Tool handler logic is implemented as module-level async functions (handle_*)
so they can be unit-tested independently of the MCP SDK Server internals.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from mcp.server import Server
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)

from codebase_rag.common.logging import get_logger
from codebase_rag.services.management_api.models import ManagementApiError
from codebase_rag.services.management_api.service import ManagementApiService
from codebase_rag.services.mcp_service.models import (
    McpServiceError,
    ProjectNotIndexedError,
    ProjectNotRegisteredError,
    SearchResultPoint,
)
from codebase_rag.services.context_engine.service import ContextEngineService
from codebase_rag.services.mcp_service.service import McpService

logger = get_logger(__name__)


# =============================================================================
# MCP Tool definitions (schemas)
# =============================================================================

TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="codebase_search",
        description=(
            "基于 Git 仓库与 Qdrant 向量数据库的 Codebase RAG 语义搜索工具。"
            "通过自然语言查询检索已索引的 Git 仓库代码知识库，"
            "返回经 AI Reranker 排序的源码文件业务描述。"
            "支持单次调用传入多条查询，每条查询可指定一个或多个 Git 仓库。"
            "结果按查询和仓库两层分组返回，单个仓库失败不影响其他仓库。"
            "使用前需确保目标 Git 仓库已通过管理 API 注册并完成索引。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "description": "查询列表，支持多条查询并发执行，结果按查询和仓库两层分组返回。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "git_repo_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Git 仓库根目录的绝对路径列表。每个路径应为仓库根目录本身（即 .git/ 所在的父目录），例如 'G:/project/MyRepo'，而非 'G:/project/MyRepo/.git'。支持同时搜索多个仓库。",
                            },
                            "query": {
                                "type": "string",
                                "description": "自然语言搜索查询，描述你要查找的代码功能或业务逻辑",
                            },
                            "top_k": {
                                "type": "integer",
                                "description": "每个仓库中该查询返回的最大结果数（默认: 10，上限: 50）",
                                "default": 10,
                            },
                        },
                        "required": ["git_repo_paths", "query"],
                    },
                },
            },
            "required": ["queries"],
        },
    ),
    Tool(
        name="project_status",
        description=(
            "查询已注册 Git 仓库的索引状态，"
            "包括分支、提交、索引状态和知识点数量。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_repo_path": {
                    "type": "string",
                    "description": "Git 仓库根目录的绝对路径，即 .git/ 所在的父目录。例如 'G:/project/MyRepo'，而非 'G:/project/MyRepo/.git'。",
                },
            },
            "required": ["git_repo_path"],
        },
    ),
    Tool(
        name="list_projects",
        description="列出所有已注册的 Git 仓库及其当前索引状态。",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="codebase_context_analysis",
        description=(
            "对目标代码库进行深度上下文分析。根据编程任务描述，"
            "自主执行多轮「搜索→阅读→分析」循环，"
            "输出包含精确源码引用的结构化分析报告。"
            "支持同时分析多个 Git 仓库。典型耗时 30s-10min。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_repo_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Git 仓库根目录的绝对路径列表。每个路径应为仓库根目录本身（即 .git/ 所在的父目录），例如 'G:/project/MyRepo'，而非 'G:/project/MyRepo/.git'。支持同时分析多个仓库。",
                },
                "task_description": {
                    "type": "string",
                    "description": "编程任务描述或问题（自然语言）",
                },
            },
            "required": ["git_repo_paths", "task_description"],
        },
    ),
    Tool(
        name="get_file_knowledge",
        description=(
            "批量获取指定文件当前已提交版本的知识点描述。"
            "返回每个文件的全部知识描述文本列表。"
            "文件不存在已提交知识点时返回空列表。"
            "典型用途：修改文件前获取已有知识描述，便于后续更新。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_repo_path": {
                    "type": "string",
                    "description": "Git 仓库根目录的绝对路径（即 .git/ 所在的父目录）",
                },
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "文件路径列表，相对于仓库根目录。例如 ['src/main.py', 'lib/utils.ts']",
                },
            },
            "required": ["git_repo_path", "file_paths"],
        },
    ),
    Tool(
        name="add_file_knowledge",
        description=(
            "提交单个文件版本的知识点描述。"
            "用于编程 Agent 修改或创建文件后、git commit 之前，"
            "主动提供该文件当前工作区版本的业务知识描述。"
            "调用方必须提供当前文件内容对应的 git blob hash，"
            "后端会校验 blob_hash、生成嵌入向量并通过统一同步协调器落地。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_repo_path": {
                    "type": "string",
                    "description": "Git 仓库根目录的绝对路径（即 .git/ 所在的父目录）",
                },
                "file_path": {
                    "type": "string",
                    "description": "文件路径，相对于仓库根目录",
                },
                "blob_hash": {
                    "type": "string",
                    "description": (
                        "当前工作区文件内容的 Git blob SHA-1（不是 commit/HEAD/path hash）。"
                        "用 git -C <git_repo_path> hash-object -- <file_path> 计算；"
                        "<file_path> 必须相对仓库根目录且与本参数 file_path 一致。"
                        "后端会按当前工作区内容复算校验。"
                    ),
                },
                "knowledge_descriptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "该文件的知识描述文本列表，每条对应一个独立知识点。"
                        "描述应遵循 5 层结构化模板"
                        "（项目结构域/业务域/功能描述/目的描述/业务逻辑细节）。"
                    ),
                },
            },
            "required": [
                "git_repo_path",
                "file_path",
                "blob_hash",
                "knowledge_descriptions",
            ],
        },
    ),
    Tool(
        name="register_project",
        description=(
            "把本地 Git 仓库注册到 codebase-rag，并触发异步初始化/索引。\n"
            "\n"
            "【调用边界 · 强制】\n"
            "- 仅当用户明确要求“注册项目/把仓库加入 codebase-rag”时才可调用本工具。\n"
            "- 禁止未经用户同意私自调用。\n"
            "- 禁止因为“后续搜索更方便”“分析前先注册一下”“自动补全索引”等理由默认调用。\n"
            "\n"
            "入参使用仓库根目录绝对路径（含 .git 的父目录）。注册成功只表示已登记并入队初始化，"
            "不代表索引已 ready；请再用 project_status / list_projects / codebase_search 验收可用性。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_repo_path": {
                    "type": "string",
                    "description": (
                        "Git 仓库根目录的绝对路径（即 .git/ 所在的父目录）。"
                        "例如 'G:/project/MyRepo'，而非 'G:/project/MyRepo/.git'。"
                    ),
                },
                "display_name": {
                    "type": "string",
                    "description": "可选的项目展示名；不传时由服务端使用目录名或已有注册名称。",
                },
            },
            "required": ["git_repo_path"],
        },
    ),
]


# =============================================================================
# Tool handler implementations (testable standalone functions)
# =============================================================================


async def handle_call_tool(
    name: str,
    arguments: dict,
    mcp_service: McpService,
    management_api: ManagementApiService,
    context_engine: Optional[ContextEngineService] = None,
) -> list[TextContent]:
    """Dispatch a tool call to the appropriate handler.

    Returns list[TextContent] — the MCP SDK's @server.call_tool()
    decorator wraps it into CallToolResult(content=list(results)).
    """
    if name == "codebase_search":
        return await handle_codebase_search(arguments, mcp_service)
    elif name == "project_status":
        return await handle_project_status(arguments, mcp_service)
    elif name == "list_projects":
        return await handle_list_projects(management_api)
    elif name == "register_project":
        return await handle_register_project(arguments, management_api)
    elif name == "codebase_context_analysis":
        return await handle_context_analysis(arguments, context_engine)
    elif name == "get_file_knowledge":
        return await handle_get_file_knowledge(arguments, mcp_service)
    elif name == "add_file_knowledge":
        return await handle_add_file_knowledge(arguments, mcp_service)
    else:
        raise ValueError(f"Unknown tool: '{name}'")


async def handle_codebase_search(
    args: dict, mcp_service: McpService,
) -> list[TextContent]:
    """Handle codebase_search tool call.

    Accepts a list of queries, each targeting one or more Git repos.
    All (query × repo) combinations run concurrently; results are
    grouped by query → repo.  Per-repo failures are reported inline
    without blocking other repos.
    """
    queries = args.get("queries", [])
    if not queries:
        raise ValueError("Missing required parameter: 'queries'.")

    for q in queries:
        repo_paths = q.get("git_repo_paths")
        if not repo_paths or not isinstance(repo_paths, list):
            raise ValueError(
                "Each query must have 'git_repo_paths' (non-empty array) and 'query'."
            )
        if not q.get("query"):
            raise ValueError(
                "Each query must have 'git_repo_paths' and 'query'."
            )

    # Build task index: (query_idx, repo_idx) → task position
    tasks: list = []
    task_index: List[tuple] = []  # (query_idx, repo_idx)

    for q_idx, q in enumerate(queries):
        top_k = min(q.get("top_k", 10), 50)
        for r_idx, repo_path in enumerate(q["git_repo_paths"]):
            tasks.append(
                mcp_service.search(
                    project_path=repo_path,
                    query=q["query"],
                    top_k=top_k,
                )
            )
            task_index.append((q_idx, r_idx))

    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Assemble grouped results
    grouped: List[QuerySearchGroup] = []
    for q_idx, q in enumerate(queries):
        repo_groups: List[RepoSearchGroup] = []
        for r_idx, repo_path in enumerate(q["git_repo_paths"]):
            repo_groups.append(RepoSearchGroup(repo_path=repo_path))
        grouped.append(QuerySearchGroup(query=q["query"], repos=repo_groups))

    for pos, (q_idx, r_idx) in enumerate(task_index):
        res = all_results[pos]
        repo_group = grouped[q_idx].repos[r_idx]
        if isinstance(res, Exception):
            repo_group.error = str(res)
        else:
            repo_group.results = res

    text = _format_grouped_results(grouped)
    return [TextContent(type="text", text=text)]


async def handle_project_status(
    args: dict, mcp_service: McpService,
) -> list[TextContent]:
    """Handle project_status tool call."""
    git_repo_path = args.get("git_repo_path", "")
    if not git_repo_path:
        raise ValueError("Missing required parameter: 'git_repo_path'.")

    info = await mcp_service.get_active_instance_info(git_repo_path)

    repo_name = os.path.basename(git_repo_path.rstrip("/\\"))
    if info:
        text = (
            f"Git 仓库: {repo_name} ({git_repo_path})\n"
            f"Branch: {info.branch_name}\n"
            f"Commit: {info.commit_hash[:8]}\n"
            f"Status: ready\n"
            f"Knowledge Points: {info.point_count}\n"
            f"Memory Usage: {info.memory_usage_mb:.1f} MB"
        )
    else:
        text = (
            f"Git 仓库: {repo_name} ({git_repo_path})\n"
            f"Status: not indexed\n"
            f"未找到活跃实例。该仓库可能需要先注册或重建索引。"
        )

    return [TextContent(type="text", text=text)]


async def handle_context_analysis(
    args: dict, context_engine: Optional[ContextEngineService],
) -> list[TextContent]:
    """Handle codebase_context_analysis tool call."""
    if context_engine is None:
        raise ValueError("Context Engine service is not configured.")

    git_repo_paths = args.get("git_repo_paths")
    task_description = args.get("task_description", "")

    if not git_repo_paths or not isinstance(git_repo_paths, list):
        raise ValueError(
            "Missing required parameter: 'git_repo_paths' (non-empty array)."
        )
    if not task_description:
        raise ValueError(
            "Missing required parameter: 'task_description'."
        )

    report = await context_engine.context_analysis(git_repo_paths, task_description)
    return [TextContent(type="text", text=report)]


async def handle_list_projects(
    management_api: ManagementApiService,
) -> list[TextContent]:
    """Handle list_projects tool call."""
    projects = await management_api.list_projects()

    if not projects:
        return [TextContent(type="text", text="未注册任何 Git 仓库。")]

    lines = [f"已注册 Git 仓库 ({len(projects)}):\n"]
    for p in projects:
        lines.append(
            f"- **{p.display_name}** ({p.git_repo_path})\n"
            f"  Status: {p.status} | "
            f"Branch: {p.branch or 'N/A'} | "
            f"Points: {p.knowledge_points}"
        )

    return [TextContent(type="text", text="\n".join(lines))]


async def handle_register_project(
    args: dict,
    management_api: ManagementApiService,
) -> list[TextContent]:
    """Handle register_project tool call.

    Thin northbound adapter over ManagementApiService.register_project.
    Does not wait for index readiness.
    """
    git_repo_path = args.get("git_repo_path", "")
    if not git_repo_path or not isinstance(git_repo_path, str):
        raise ValueError("Missing required parameter: 'git_repo_path'.")

    display_name = args.get("display_name", "")
    if display_name is None:
        display_name = ""
    if not isinstance(display_name, str):
        raise ValueError("Optional parameter 'display_name' must be a string.")

    try:
        project = await management_api.register_project(
            git_repo_path=git_repo_path,
            display_name=display_name,
        )
    except ManagementApiError as e:
        raise ValueError(str(e)) from e

    text = (
        f"项目已注册并入队异步初始化/索引。\n"
        f"project_id: {project.project_id}\n"
        f"display_name: {project.display_name}\n"
        f"git_repo_path: {project.git_repo_path}\n"
        f"status: {project.status}\n"
        f"branch: {project.branch or 'N/A'}\n"
        f"commit_hash: {project.commit_hash or 'N/A'}\n"
        f"knowledge_points: {project.knowledge_points}\n"
        f"\n"
        f"注意：注册成功只表示已登记并入队初始化，不代表索引已 ready。"
        f"请再用 project_status / list_projects / codebase_search 验收可用性。"
    )
    return [TextContent(type="text", text=text)]


async def handle_get_file_knowledge(
    args: dict, mcp_service: McpService,
) -> list[TextContent]:
    """Handle get_file_knowledge tool call.

    Batch-retrieves committed knowledge descriptions for given files.
    Per-file failures are reported inline without blocking other files.
    """
    git_repo_path = args.get("git_repo_path")
    file_paths = args.get("file_paths")

    if not git_repo_path:
        raise ValueError("Missing required parameter: 'git_repo_path'.")
    if not file_paths or not isinstance(file_paths, list):
        raise ValueError("Missing required parameter: 'file_paths' (non-empty array).")

    results = await mcp_service.get_file_knowledge(git_repo_path, file_paths)

    parts: List[str] = []
    for r in results:
        if r.error:
            parts.append(
                f"## {r.file_path}\n\n"
                f"⚠ 查询失败: {r.error}\n"
            )
        elif not r.descriptions:
            parts.append(
                f"## {r.file_path} (0 knowledge points)\n\n"
                f"该文件暂无已提交的知识点。\n"
            )
        else:
            parts.append(
                f"## {r.file_path} ({len(r.descriptions)} knowledge points)\n"
            )
            for i, desc in enumerate(r.descriptions, 1):
                parts.append(f"### [{i}]\n{desc}\n")

    return [TextContent(type="text", text="\n".join(parts))]


async def handle_add_file_knowledge(
    args: dict, mcp_service: McpService,
) -> list[TextContent]:
    """Handle add_file_knowledge tool call.

    Validates descriptions, generates embeddings, and submits knowledge
    points through KnowledgeSyncService.
    """
    git_repo_path = args.get("git_repo_path")
    file_path = args.get("file_path")
    blob_hash = args.get("blob_hash")
    descs = args.get("knowledge_descriptions")

    if not git_repo_path:
        raise ValueError("Missing required parameter: 'git_repo_path'.")
    if not file_path or not isinstance(file_path, str):
        raise ValueError("Missing required parameter: 'file_path'.")
    if not blob_hash or not isinstance(blob_hash, str):
        raise ValueError("Missing required parameter: 'blob_hash'.")
    if not descs or not isinstance(descs, list):
        raise ValueError(
            "Missing required parameter: 'knowledge_descriptions' (non-empty array)."
        )
    for idx, d in enumerate(descs):
        if not d or not isinstance(d, str) or not d.strip():
            raise ValueError(
                f"knowledge_descriptions[{idx}] must be a non-empty string."
            )

    result = await mcp_service.add_file_knowledge(
        git_repo_path, file_path, blob_hash, descs,
    )

    parts: List[str] = []

    succeeded_count = len(result.succeeded)
    failed_count = len(result.failed)
    total_points = sum(s.points_count for s in result.succeeded)

    if succeeded_count > 0 and failed_count == 0:
        parts.append(
            f"已提交 {succeeded_count} 个文件版本共 {total_points} 条知识点，"
            f"本地可用并已进入统一同步流程。\n"
        )
        for s in result.succeeded:
            parts.append(f"- {s.file_path}: {s.points_count} 条 ✓")
    elif succeeded_count > 0 and failed_count > 0:
        parts.append(
            f"已提交 {succeeded_count} 个文件版本共 {total_points} 条知识点，"
            f"{failed_count} 个文件失败。\n"
        )
        parts.append("成功:")
        for s in result.succeeded:
            parts.append(f"- {s.file_path}: {s.points_count} 条 ✓")
        parts.append("\n失败:")
        for fail in result.failed:
            parts.append(f"- {fail.file_path}: {fail.error}")
    else:
        parts.append(f"全部 {failed_count} 个文件失败。\n")
        for fail in result.failed:
            parts.append(f"- {fail.file_path}: {fail.error}")

    return [TextContent(type="text", text="\n".join(parts))]


# =============================================================================
# MCP Server factory
# =============================================================================


def create_mcp_server(
    server_name: str,
    mcp_service: McpService,
    management_api: ManagementApiService,
    context_engine: Optional[ContextEngineService] = None,
) -> Server:
    """Create and configure an MCP Server with all tools and resources.

    Args:
        server_name: MCP Server name (protocol metadata).
        mcp_service: McpService instance (two-stage search).
        management_api: ManagementApiService instance (project listing/status).
        context_engine: Optional ContextEngineService instance (lazy-loaded).

    Returns:
        Configured mcp.server.Server ready to be mounted on an ASGI app.
    """
    server = Server(server_name)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOL_DEFINITIONS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await handle_call_tool(
            name, arguments, mcp_service, management_api, context_engine,
        )

    return server


# =============================================================================
# Grouped result structures
# =============================================================================


@dataclass
class RepoSearchGroup:
    """Search results for a single (query, repo) pair."""

    repo_path: str
    results: List[SearchResultPoint] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class QuerySearchGroup:
    """Search results for a single query across all targeted repos."""

    query: str
    repos: List[RepoSearchGroup] = field(default_factory=list)


# =============================================================================
# Formatting helpers
# =============================================================================


def _format_grouped_results(groups: List[QuerySearchGroup]) -> str:
    """Format search results grouped by query → repo as Markdown."""
    total_results = sum(
        len(rg.results) for qg in groups for rg in qg.repos if not rg.error
    )
    total_errors = sum(
        1 for qg in groups for rg in qg.repos if rg.error
    )

    if total_results == 0 and total_errors == 0:
        queries_str = "; ".join(f'"{qg.query}"' for qg in groups)
        return f"No results found for queries: {queries_str}"

    parts: List[str] = []

    for q_idx, qg in enumerate(groups):
        parts.append(f"# Query {q_idx + 1}: \"{qg.query}\"\n")

        for rg in qg.repos:
            repo_name = os.path.basename(rg.repo_path.rstrip("/\\"))

            if rg.error:
                parts.append(
                    f"## Repo: {repo_name} ({rg.repo_path})\n\n"
                    f"⚠ 查询失败: {rg.error}\n"
                )
                continue

            count = len(rg.results)
            parts.append(
                f"## Repo: {repo_name} ({rg.repo_path}) — {count} results\n"
            )

            for i, r in enumerate(rg.results, 1):
                parts.append(
                    f"### [{i}] {r.file_path} (score: {r.score})\n\n"
                    f"{r.business_description}\n"
                )

    return "\n".join(parts)


