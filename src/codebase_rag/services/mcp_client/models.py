"""Data models and errors for MCP Client Service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class ToolInfo:
    """MCP Tool description — used by Agent Service to build tools list."""

    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class McpClientError(Exception):
    """Base class for MCP Client errors."""


class ServerNotFoundError(McpClientError):
    """Requested MCP server name not configured."""


class ToolNotFoundError(McpClientError):
    """Requested tool not registered/available on the target server."""
