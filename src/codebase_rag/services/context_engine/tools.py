"""Tool schemas and handler helpers for Context Engine agent.

Defines:
  - ToolSchema instances for local tools (search_code, read_file, list_directory, submit_report)
  - Helper functions for ripgrep execution, file reading, and directory listing
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from codebase_rag.common.logging import get_logger
from codebase_rag.config.schema import ContextEngineConfig
from codebase_rag.services.agent.models import ToolSchema

logger = get_logger(__name__)

# =============================================================================
# Tool Schemas (registered with Agent Service)
# =============================================================================

SEARCH_CODE_SCHEMA = ToolSchema(
    name="search_code",
    description=(
        "在 Git 仓库中搜索代码。使用 ripgrep 执行文本/正则搜索，返回匹配的文件路径、"
        "行号和上下文。适用于追踪符号定义、查找引用、搜索 import/依赖关系。"
        "不指定 git_repo_path 则搜索所有目标仓库。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "搜索模式（默认正则表达式，设 fixed_strings=true 则为字面字符串）",
            },
            "git_repo_path": {
                "type": "string",
                "description": "指定搜索的 Git 仓库绝对路径。可选，不填则搜索所有目标仓库",
            },
            "file_glob": {
                "type": "string",
                "description": "文件过滤 glob，如 '*.java'、'*.py'。可选，不填则搜索所有文件",
            },
            "fixed_strings": {
                "type": "boolean",
                "description": "若为 true，pattern 按字面字符串匹配（不解释为正则）。默认 false",
                "default": False,
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回匹配数。默认 30",
                "default": 30,
            },
            "search_filenames": {
                "type": "boolean",
                "description": "若为 true，搜索文件名而非文件内容。pattern 将匹配文件路径。默认 false",
                "default": False,
            },
        },
        "required": ["pattern"],
    },
)

READ_FILE_SCHEMA = ToolSchema(
    name="read_file",
    description=(
        "读取指定文件的源码内容。支持绝对路径和相对路径。"
        "支持两种模式：(1) 全文读取：不指定行范围，返回文件完整内容；"
        "(2) 范围读取：指定 start_line/end_line，读取指定区域。根据需要选择模式——"
        "小文件或需要全局理解时用全文读取，大文件或已知目标位置时用范围读取。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径。支持绝对路径（如 codebase_search 返回的路径）或相对路径（自动在各仓库中查找）",
            },
            "start_line": {
                "type": "integer",
                "description": "起始行号（1-based，含）。可选，不填则从第 1 行开始",
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号（1-based，含）。可选，不填则读到文件末尾",
            },
        },
        "required": ["file_path"],
    },
)

LIST_DIRECTORY_SCHEMA = ToolSchema(
    name="list_directory",
    description="列出指定目录下的文件和子目录。用于了解项目结构、发现相关模块目录。",
    parameters={
        "type": "object",
        "properties": {
            "dir_path": {
                "type": "string",
                "description": (
                    "目录路径。支持绝对路径或相对路径（自动在各仓库中查找）。"
                    "传空字符串或 '.' 列出所有目标仓库根目录"
                ),
            },
            "max_depth": {
                "type": "integer",
                "description": "递归深度，默认 2",
                "default": 2,
            },
        },
        "required": ["dir_path"],
    },
)

SUBMIT_REPORT_SCHEMA = ToolSchema(
    name="submit_report",
    description=(
        "提交最终的 codebase 上下文分析报告，结束任务。报告应包含业务架构分析、"
        "关键文件引用（绝对路径 + 行范围）、调用链路、依赖关系。"
        "在你认为已充分了解目标业务的实现全貌后调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "report": {
                "type": "string",
                "description": "Markdown 格式的完整分析报告",
            },
        },
        "required": ["report"],
    },
)

ALL_LOCAL_TOOL_SCHEMAS = [
    SEARCH_CODE_SCHEMA,
    READ_FILE_SCHEMA,
    LIST_DIRECTORY_SCHEMA,
    SUBMIT_REPORT_SCHEMA,
]


# =============================================================================
# Per-invocation state (created fresh for each context_analysis call)
# =============================================================================

@dataclass
class ContextEngineToolState:
    """Mutable state shared across tool handlers within a single analysis run."""

    visited_files: Set[str] = field(default_factory=set)
    final_report: Optional[str] = None


# =============================================================================
# Tool handler helpers
# =============================================================================

async def run_ripgrep(
    project_path: str,
    pattern: str,
    kwargs: Dict[str, Any],
    config: ContextEngineConfig,
) -> str:
    """Execute ripgrep and return raw stdout."""
    file_glob = kwargs.get("file_glob")
    fixed_strings = kwargs.get("fixed_strings", False)
    max_results = kwargs.get("max_results", 30)
    search_filenames = kwargs.get("search_filenames", False)

    if search_filenames:
        return await _run_ripgrep_filenames(
            project_path, pattern, file_glob, max_results, config,
        )

    cmd: List[str] = [
        "rg", pattern, project_path,
        "--max-count", "5",
        "--max-columns", "200",
        "--context", "2",
        "--line-number",
        "--heading",
        "--color", "never",
    ]
    if file_glob:
        cmd.extend(["--glob", file_glob.replace("\\", "/")])
    if fixed_strings:
        cmd.append("--fixed-strings")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_path,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=config.rg_timeout,
        )
    except asyncio.TimeoutError:
        return f"错误：ripgrep 搜索超时（{config.rg_timeout}s）。请尝试更精确的搜索模式。"
    except FileNotFoundError:
        return "错误：ripgrep (rg) 未安装。无法执行代码搜索。"

    if proc.returncode == 1:
        return f"搜索 \"{pattern}\" 无匹配结果。"
    if proc.returncode and proc.returncode > 1:
        err = stderr.decode(errors="replace").strip()[:200]
        return f"ripgrep 错误（exit {proc.returncode}）：{err}"

    output = stdout.decode(errors="replace")

    # Truncate to approximate max_results
    lines = output.split("\n")
    max_lines = max_results * 8  # ~8 lines per match with context
    if len(lines) > max_lines:
        output = "\n".join(lines[:max_lines]) + f"\n\n... 输出已截断（显示前 {max_results} 条匹配）"

    return output


async def _run_ripgrep_filenames(
    project_path: str,
    pattern: str,
    file_glob: Optional[str],
    max_results: int,
    config: ContextEngineConfig,
) -> str:
    """Search file names/paths using rg --files, then filter by pattern in Python.

    Note: pipe between asyncio subprocesses is not supported on Windows
    (ProactorEventLoop), so we read list output first, then filter.
    """
    list_cmd: List[str] = ["rg", "--files", project_path, "--color", "never"]
    if file_glob:
        list_cmd.extend(["--glob", file_glob.replace("\\", "/")])

    try:
        list_proc = await asyncio.create_subprocess_exec(
            *list_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_path,
        )
        stdout_bytes, stderr = await asyncio.wait_for(
            list_proc.communicate(),
            timeout=config.rg_timeout,
        )
    except asyncio.TimeoutError:
        return f"错误：文件名搜索超时（{config.rg_timeout}s）。"
    except FileNotFoundError:
        return "错误：ripgrep (rg) 未安装。"

    if list_proc.returncode == 1 or not stdout_bytes:
        return f"文件名搜索 \"{pattern}\" 无匹配结果。"

    # Filter file paths by pattern (case-insensitive) in Python
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)

    all_files = stdout_bytes.decode(errors="replace").strip().split("\n")
    matched = [f for f in all_files if f and regex.search(f)]

    if not matched:
        return f"文件名搜索 \"{pattern}\" 无匹配结果。"

    # Make paths relative to project_path for readability
    prefix = project_path.replace("\\", "/").rstrip("/") + "/"
    rel_lines = []
    for line in matched:
        normed = line.replace("\\", "/")
        if normed.startswith(prefix):
            rel_lines.append(normed[len(prefix):])
        else:
            rel_lines.append(normed)

    if len(rel_lines) > max_results:
        rel_lines = rel_lines[:max_results]
        return "\n".join(rel_lines) + f"\n\n... 共显示前 {max_results} 个匹配文件"

    return f"找到 {len(rel_lines)} 个匹配文件：\n" + "\n".join(rel_lines)


def format_search_results(pattern: str, output: str) -> str:
    """Format ripgrep output for the agent."""
    if output.startswith("错误：") or output.startswith("搜索 ") or output.startswith("找到 ") or output.startswith("文件名搜索"):
        return output
    match_count = sum(1 for line in output.split("\n") if line and line[0].isdigit())
    header = f"搜索 \"{pattern}\" 找到匹配：\n\n"
    return header + output


async def read_file_lines(
    abs_path: str,
    kwargs: Dict[str, Any],
    config: ContextEngineConfig,
) -> str:
    """Read file content, optionally sliced by line range."""
    start_line = kwargs.get("start_line")
    end_line = kwargs.get("end_line")

    if not os.path.isfile(abs_path):
        return f"错误：文件不存在 — {abs_path}"

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        return f"错误：无法读取文件 — {e}"

    total = len(all_lines)

    # Determine slice
    s = (start_line - 1) if start_line and start_line >= 1 else 0
    e = end_line if end_line and end_line <= total else total

    selected = all_lines[s:e]
    truncated = False
    if len(selected) > config.max_file_read_lines:
        selected = selected[: config.max_file_read_lines]
        truncated = True
        e = s + config.max_file_read_lines

    # Format with line numbers
    numbered = []
    for i, line in enumerate(selected, start=s + 1):
        numbered.append(f"{i:>5}│ {line.rstrip()}")
    body = "\n".join(numbered)

    rel_path = os.path.basename(abs_path)
    header = f"文件：{rel_path}（第 {s + 1}-{e} 行，共 {total} 行）"
    if truncated:
        header += f"\n⚠️ 输出已截断至 {config.max_file_read_lines} 行。请指定 start_line/end_line 分段读取。"

    return f"{header}\n\n{body}"


async def list_dir_tree(
    abs_path: str,
    kwargs: Dict[str, Any],
) -> str:
    """List directory tree up to max_depth."""
    max_depth = kwargs.get("max_depth", 2)

    if not os.path.isdir(abs_path):
        return f"错误：目录不存在 — {abs_path}"

    lines: List[str] = []
    file_count = 0
    dir_count = 0

    def _walk(path: str, prefix: str, depth: int) -> None:
        nonlocal file_count, dir_count
        if depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            lines.append(f"{prefix}[权限不足]")
            return

        dirs = []
        files = []
        for entry in entries:
            full = os.path.join(path, entry)
            if os.path.isdir(full):
                # Skip hidden/common non-essential dirs
                if entry.startswith(".") or entry in ("node_modules", "__pycache__", ".git"):
                    continue
                dirs.append(entry)
            else:
                files.append(entry)

        items = [(d, True) for d in dirs] + [(f, False) for f in files]

        for i, (name, is_dir) in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            full_path = os.path.join(path, name)

            if is_dir:
                dir_count += 1
                lines.append(f"{prefix}{connector}{name}/")
                child_prefix = prefix + ("    " if is_last else "│   ")
                _walk(full_path, child_prefix, depth + 1)
            else:
                file_count += 1
                # Try to get line count for text files
                try:
                    size = os.path.getsize(full_path)
                    if size < 1_000_000:  # < 1MB
                        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                            lc = sum(1 for _ in f)
                        lines.append(f"{prefix}{connector}{name}  ({lc} 行)")
                    else:
                        lines.append(f"{prefix}{connector}{name}  ({size // 1024} KB)")
                except Exception:
                    lines.append(f"{prefix}{connector}{name}")

    _walk(abs_path, "", 1)

    rel = os.path.basename(abs_path) or abs_path
    header = f"目录：{rel}/（{file_count} 个文件，{dir_count} 个子目录）\n"
    return header + "\n".join(lines)


async def check_ripgrep_available() -> bool:
    """Check if ripgrep is installed and accessible."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "rg", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
        return proc.returncode == 0
    except (FileNotFoundError, asyncio.TimeoutError):
        return False
