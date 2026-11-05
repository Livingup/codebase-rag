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
    ImageContent,
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

ToolHandlerResult = list[TextContent | ImageContent] | CallToolResult


# =============================================================================
# MCP Tool definitions (schemas)
# =============================================================================

TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="codebase_search",
        description=(
            "基于 Git 仓库与 Qdrant 向量数据库的 Codebase RAG 语义搜索工具。"
            "通过自然语言查询检索已索引的 Git 仓库代码知识库，"
            "返回按相关性排序的源码文件业务描述。"
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
            "查询指定仓库在当前服务中的登记情况及正在执行的初始化任务；"
            "不保证可检索。"
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
        description="列出当前服务已登记的 Git 仓库名称与路径。",
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
            "读取指定文件当前已提交版本的可读知识说明。"
            "逐文件返回说明、空结果或错误文本。"
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
                    "description": "文件的绝对路径列表。服务端将根据 git_repo_path 动态计算相对路径。例如 ['G:/project/MyRepo/src/main.py']",
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
            "仅支持文本文件与 .png / .jpg / .jpeg 图片文件（单张图片不超过 20MB）。"
            "单条知识点最大支持 8192 tokens（图片文件每条知识点固定占用 258 图片 tokens，服务端对每条知识点以「文字描述 + 原始图片」一起生成多模态向量）。"
            "调用方必须提供当前文件内容对应的 git blob hash，"
            "后端会校验 blob_hash、自动识别文件类型（text / image）、生成嵌入向量并通过统一同步协调器落地。"
            "已受理不代表立即可检索；同一文件版本已有知识时不覆盖已有内容。"
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
                    "description": "文件的绝对路径（仅支持文本文件或 .png / .jpg / .jpeg 图片文件）。服务端将根据 git_repo_path 动态计算相对路径并自动识别文件类型",
                },
                "blob_hash": {
                    "type": "string",
                    "description": (
                        "当前工作区文件内容的 Git blob SHA-1（不是 commit/HEAD/path hash）。"
                        "后端会根据计算出的相对路径按当前工作区内容复算校验。"
                    ),
                },
                "knowledge_descriptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "该文件的知识描述文本列表，每条对应一个独立知识点（单条上限 8192 tokens；图片文件额外计入 258 tokens）。"
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
        name="manage_project_ignore",
        description=(
            "查看或编辑指定 Git 仓库的知识点跟踪忽略规则文件（存放在 .git/codebase-rag/ignore）。"
            "语法规则与 .gitignore 完全一致（支持通配符 *、**、目录后缀 /、注释 # 以及 ! 反选规则），"
            "仅用于控制 codebase-rag 是否拦截该仓库中文件的知识点生成与提交，不影响 Git 本身的 .gitignore。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_repo_path": {
                    "type": "string",
                    "description": "Git 仓库根目录的绝对路径（即 .git/ 所在的父目录）",
                },
                "action": {
                    "type": "string",
                    "enum": ["get", "add", "remove", "set"],
                    "description": "操作类型：get（查看当前规则）、add（追加规则）、remove（移除指定规则）、set（整体替换规则内容）",
                },
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "当 action 为 add / remove / set 时传入的 .gitignore 语法规则列表（例如 ['assets/raw/', '*.psd', '!assets/raw/keep.png']）",
                },
                "content": {
                    "type": "string",
                    "description": "当 action 为 set 时可选直接传入完整的 .git/codebase-rag/ignore 多行文本内容",
                },
            },
            "required": ["git_repo_path", "action"],
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
            "登记完成不代表检索已就绪。"
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
) -> ToolHandlerResult:
    """Dispatch tools, preserving explicit submission failure results."""
    if name == "codebase_search":
        return await handle_codebase_search(arguments, mcp_service)
    elif name == "project_status":
        return await handle_project_status(arguments, management_api)
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
    elif name == "manage_project_ignore":
        return await handle_manage_project_ignore(arguments, mcp_service)
    else:
        raise ValueError(f"Unknown tool: '{name}'")


async def handle_codebase_search(
    args: dict, mcp_service: McpService,
) -> list[TextContent | ImageContent]:
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

    try:
        all_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        logger.error("handle_codebase_search_timeout", queries_count=len(queries), tasks_count=len(tasks))
        all_results = [
            TimeoutError("Search timed out after 120s") for _ in tasks
        ]

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
    contents: list[TextContent | ImageContent] = [TextContent(type="text", text=text)]

    # Append multimodal ImageContent parts for any matched image knowledge points
    seen_image_paths: set[str] = set()
    if hasattr(mcp_service, "read_repo_image_base64"):
        for qg in grouped:
            for rg in qg.repos:
                if rg.error:
                    continue
                for r in rg.results:
                    if getattr(r, "file_type", "text") == "image" and r.file_path not in seen_image_paths:
                        seen_image_paths.add(r.file_path)
                        img_data = await mcp_service.read_repo_image_base64(rg.repo_path, r.file_path)
                        if img_data is not None:
                            b64_str, mime_type = img_data
                            contents.append(
                                ImageContent(
                                    type="image",
                                    data=b64_str,
                                    mimeType=mime_type,
                                )
                            )

    return contents


async def handle_project_status(
    args: dict, management_api: ManagementApiService,
) -> list[TextContent]:
    """Report registration facts without probing or rebuilding an index."""
    git_repo_path = args.get("git_repo_path", "")
    if not isinstance(git_repo_path, str) or not git_repo_path.strip():
        raise ValueError("Missing required parameter: 'git_repo_path'.")
    info = await management_api.get_project_registration_status(git_repo_path)
    lines = [f"仓库：{info.git_repo_path}"]
    if info.registered:
        lines.append("登记状态：已登记")
        if info.initialization_running:
            lines.append("初始化进行中。")
    else:
        lines.extend(["登记状态：未登记", "本服务尚未登记此仓库。"])
    return [TextContent(type="text", text="\n".join(lines))]


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
    projects = await management_api.list_registered_projects()

    if not projects:
        return [TextContent(type="text", text="未注册任何 Git 仓库。")]

    lines = [f"已登记 Git 仓库（{len(projects)}）："]
    for p in projects:
        lines.append(f"- {p.display_name}：{p.git_repo_path}")

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
        f"项目已登记，初始化已安排。\n"
        f"名称：{project.display_name}\n"
        f"仓库：{project.git_repo_path}\n"
        f"project_id：{project.project_id}\n"
        f"登记时 HEAD：{project.branch or 'N/A'} @ {project.commit_hash or 'N/A'}\n"
        f"登记完成不代表检索已就绪。"
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
                f"⚠ 查询失败：{r.error}\n"
            )
        elif not r.descriptions:
            parts.append(
                f"## {r.file_path}（返回 0 条知识说明；当前已提交版本）\n\n"
                f"本次未返回该文件当前已提交版本的可读知识说明。\n"
            )
        else:
            parts.append(
                f"## {r.file_path}（返回 {len(r.descriptions)} 条知识说明；当前已提交版本）\n"
            )
            for i, desc in enumerate(r.descriptions, 1):
                parts.append(f"### [{i}]\n{desc}\n")

    return [TextContent(type="text", text="\n".join(parts))]


async def handle_add_file_knowledge(
    args: dict, mcp_service: McpService,
) -> CallToolResult:
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

    if len(result.succeeded) == 1 and not result.failed:
        success = result.succeeded[0]
        if success.outcome == "accepted":
            text = (
                f"知识提交已受理。\n文件：{success.file_path}\n"
                "无需重复提交；不代表立即可检索。"
            )
        elif success.outcome == "unchanged":
            text = (
                "该文件版本已有知识，本次未覆盖已有内容。\n"
                f"文件：{success.file_path}"
            )
        else:
            raise McpServiceError("知识提交返回了无法识别的结果，不能确认本次提交。")
        return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)
    if len(result.failed) == 1 and not result.succeeded:
        failure = result.failed[0]
        return CallToolResult(
            content=[TextContent(
                type="text", text=f"提交失败：{failure.error}\n文件：{failure.file_path}",
            )],
            isError=True,
        )
    raise McpServiceError("知识提交返回了不一致的单文件结果，不能确认本次提交。")


async def handle_manage_project_ignore(
    args: dict, mcp_service: McpService,
) -> list[TextContent]:
    """Handle manage_project_ignore tool call."""
    git_repo_path = args.get("git_repo_path", "")
    action = args.get("action", "")
    patterns = args.get("patterns")
    content = args.get("content")

    if not git_repo_path or not isinstance(git_repo_path, str):
        raise ValueError("Missing required parameter: 'git_repo_path'.")
    if not action or not isinstance(action, str):
        raise ValueError("Missing required parameter: 'action'.")
    if patterns is not None and not isinstance(patterns, list):
        raise ValueError("Optional parameter 'patterns' must be an array of strings.")

    info = mcp_service.manage_project_ignore(
        project_path=git_repo_path,
        action=action,
        patterns=patterns,
        content=content,
    )
    rules = info.get("rules") or []
    lines = [
        f"仓库：{info['git_repo_path']}",
        f"忽略规则文件：{info['ignore_file']}",
        f"操作：{info['action']}",
        f"当前生效自定义规则数：{len(rules)}",
    ]
    raw_content = (info.get("content") or "").strip()
    if raw_content:
        lines.append("\n```gitignore\n" + raw_content + "\n```")
    else:
        lines.append("\n（当前无自定义忽略规则，仅使用全局默认 exclude_patterns）")
    return [TextContent(type="text", text="\n".join(lines))]


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
    async def call_tool(name: str, arguments: dict) -> ToolHandlerResult:
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
        return (
            f"本次检索未返回结果。\n查询：{queries_str}\n"
            "这不表示源码中不存在相关实现。"
        )

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
                f"## Repo: {repo_name} ({rg.repo_path}) — 本次返回 {count} 条\n"
            )

            for i, r in enumerate(rg.results, 1):
                file_type_tag = " [image]" if getattr(r, "file_type", "text") == "image" else ""
                parts.append(
                    f"### [{i}] {r.file_path}{file_type_tag}\n\n"
                    f"{r.business_description}\n"
                )

    return "\n".join(parts)


