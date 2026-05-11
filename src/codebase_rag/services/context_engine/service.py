"""Context Engine Service — autonomous codebase analysis agent.

Runs the context_engine agent to perform multi-round codebase exploration
and produce a structured context report for a given programming task.

Internal dependencies:
  - 03-Agent Service (agent execution loop + LLM calls)
  - 12-MCP Client Service (codebase_search via MCP protocol)
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import List

from codebase_rag.common.logging import get_logger
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import ContextEngineConfig
from codebase_rag.services.agent.errors import AgentTaskComplete
from codebase_rag.services.agent.models import AgentDefinition
from codebase_rag.services.agent.service import AgentService
from codebase_rag.services.context_engine.prompts import CONTEXT_ENGINE_PROMPT
from codebase_rag.services.context_engine.tools import (
    ALL_LOCAL_TOOL_SCHEMAS,
    ContextEngineToolState,
    check_ripgrep_available,
    format_search_results,
    list_dir_tree,
    read_file_lines,
    run_ripgrep,
)
from codebase_rag.services.mcp_client.service import McpClientService

logger = get_logger(__name__)

_AGENT_NAME = "context_engine"


class ContextEngineService:
    """Autonomous codebase context analysis service.

    Registers the ``context_engine`` agent definition and provides
    :meth:`context_analysis` to run deep codebase exploration.

    Args:
        config: Context Engine configuration section from AppConfig.
        agent: Agent service instance.
        mcp_client: MCP Client service instance (for ``codebase_search``).
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        agent: AgentService,
        mcp_client: McpClientService,
    ) -> None:
        self._config_manager = config_manager
        self._agent = agent
        self._mcp_client = mcp_client
        self._ripgrep_available = False
        self._agent_names: List[str] = []
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @property
    def _config(self) -> ContextEngineConfig:
        return self._config_manager.config.context_engine

    # =========================================================================
    # Core: context analysis
    # =========================================================================

    async def context_analysis(
        self, git_repo_paths: List[str], task_description: str
    ) -> str:
        """Run the context engine agent and return the analysis report.

        Args:
            git_repo_paths: List of absolute paths to Git repo root directories.
            task_description: Natural language description of the programming
                task or question.

        Returns:
            Markdown-formatted context analysis report.
        """
        await self._ensure_initialized()

        if not self._ripgrep_available:
            return "错误：ripgrep (rg) 未安装，无法执行代码搜索。请安装 ripgrep 后重试。"

        start = time.monotonic()
        logger.info(
            "context_analysis_started",
            git_repo_paths=git_repo_paths,
            task_preview=task_description[:100],
        )

        # Per-invocation mutable state (independent across concurrent calls)
        state = ContextEngineToolState()
        config = self._config

        # -----------------------------------------------------------------
        # Build local tool handler closures (capture git_repo_paths + state)
        # -----------------------------------------------------------------

        async def handle_search_code(
            pattern: str, **kwargs
        ) -> str:
            git_repo_path = kwargs.pop("git_repo_path", None)
            if git_repo_path:
                output = await run_ripgrep(git_repo_path, pattern, kwargs, config)
                return format_search_results(pattern, output)
            # No specific repo → search all repos, merge results
            parts: list[str] = []
            for repo_path in git_repo_paths:
                output = await run_ripgrep(repo_path, pattern, kwargs, config)
                if not output.startswith("搜索 ") and not output.startswith("错误"):
                    repo_name = os.path.basename(repo_path.rstrip("/\\"))
                    parts.append(f"--- {repo_name} ({repo_path}) ---\n{output}")
            if not parts:
                return f'搜索 "{pattern}" 在所有仓库中无匹配结果。'
            return format_search_results(pattern, "\n\n".join(parts))

        async def handle_read_file(file_path: str, **kwargs) -> str:
            state.visited_files.add(file_path)
            # Support absolute paths (returned by codebase_search)
            if os.path.isabs(file_path):
                return await read_file_lines(file_path, kwargs, config)
            # Relative path → try each repo until found
            for repo_path in git_repo_paths:
                abs_path = os.path.join(repo_path, file_path)
                if os.path.isfile(abs_path):
                    return await read_file_lines(abs_path, kwargs, config)
            return f"错误：文件不存在 — {file_path}"

        async def handle_list_directory(dir_path: str, **kwargs) -> str:
            # Support absolute paths
            if os.path.isabs(dir_path):
                return await list_dir_tree(dir_path, kwargs)
            # Relative path or root → try each repo
            if not dir_path or dir_path == ".":
                # List all repos
                parts: list[str] = []
                for repo_path in git_repo_paths:
                    parts.append(await list_dir_tree(repo_path, kwargs))
                return "\n\n".join(parts)
            for repo_path in git_repo_paths:
                abs_path = os.path.join(repo_path, dir_path)
                if os.path.isdir(abs_path):
                    return await list_dir_tree(abs_path, kwargs)
            return f"错误：目录不存在 — {dir_path}"

        async def handle_submit_report(report: str) -> str:
            state.final_report = report
            raise AgentTaskComplete()

        tool_handlers = {
            "search_code": handle_search_code,
            "read_file": handle_read_file,
            "list_directory": handle_list_directory,
            "submit_report": handle_submit_report,
        }

        # -----------------------------------------------------------------
        # Run agent
        # -----------------------------------------------------------------
        repos_str = "\n".join(f"  - {p}" for p in git_repo_paths)
        user_msg = (
            f"目标 Git 仓库：\n{repos_str}\n"
            f"任务描述：{task_description}"
        )

        last_error: Exception | None = None
        for agent_name in self._agent_names:
            state.final_report = ""
            try:
                await self._agent.run_agent(
                    agent_name=agent_name,
                    input_variables={},
                    tool_handlers=tool_handlers,
                    user_message=user_msg,
                )
                last_error = None
                break
            except AgentTaskComplete:
                last_error = None
                break
            except Exception as e:
                last_error = e
                logger.warning(
                    "context_analysis_agent_error",
                    agent_name=agent_name,
                    error=str(e)[:300],
                    visited_files=len(state.visited_files),
                )
                continue

        if last_error is not None:
            elapsed = time.monotonic() - start
            logger.warning(
                "context_analysis_all_agents_failed",
                error=str(last_error)[:300],
                elapsed_s=round(elapsed, 1),
                visited_files=len(state.visited_files),
            )
            return (
                f"⚠️ 分析过程异常：{last_error}\n"
                f"已分析 {len(state.visited_files)} 个文件。"
            )

        elapsed = time.monotonic() - start

        if state.final_report:
            logger.info(
                "context_analysis_completed",
                elapsed_s=round(elapsed, 1),
                visited_files=len(state.visited_files),
                report_length=len(state.final_report),
            )
            return state.final_report
        else:
            logger.warning(
                "context_analysis_incomplete",
                elapsed_s=round(elapsed, 1),
                visited_files=len(state.visited_files),
            )
            return (
                f"⚠️ 分析未完成（Agent 未调用 submit_report），"
                f"已分析 {len(state.visited_files)} 个文件。"
            )

    # =========================================================================
    # Internal
    # =========================================================================

    async def _ensure_initialized(self) -> None:
        """Lazy init: register agent + check ripgrep on first call."""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            logger.info("context_engine_lazy_initializing")
            candidates = (
                [
                    (entry.provider, entry.model)
                    for entry in sorted(self._config.providers, key=lambda e: e.priority)
                ]
                if self._config.providers else [
                    (self._config.provider, self._config.model)
                ]
            )
            self._agent_names = []
            for idx, (provider, model) in enumerate(candidates):
                agent_name = _AGENT_NAME if idx == 0 else f"{_AGENT_NAME}_{idx}"
                agent_def = AgentDefinition(
                    name=agent_name,
                    description="Codebase context analysis agent — autonomous code exploration",
                    system_prompt=CONTEXT_ENGINE_PROMPT,
                    provider=provider,
                    model=model,
                    tools=ALL_LOCAL_TOOL_SCHEMAS,
                    mcp_servers={
                        name: binding.include_tools
                        for name, binding in self._config.mcp_servers.items()
                    } if self._config.mcp_servers else None,
                    temperature=0.0,
                    max_rate_limit_retries=self._config.max_rate_limit_retries,
                )
                await self._agent.register_agent(agent_def)
                self._agent_names.append(agent_name)
            self._ripgrep_available = await check_ripgrep_available()
            if not self._ripgrep_available:
                logger.warning("context_engine_ripgrep_not_found")
            self._initialized = True
            logger.info(
                "context_engine_initialized",
                ripgrep_available=self._ripgrep_available,
            )
