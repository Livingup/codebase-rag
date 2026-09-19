"""MCP Client Service — connects to MCP Servers via standard MCP protocol.

Supports two transport modes:
  - HTTP (streamable_http / sse): connects to a running MCP HTTP endpoint
  - stdio: spawns a subprocess and communicates via stdin/stdout

Internal dependencies: None (pure infrastructure).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client

from codebase_rag.common.logging import get_logger
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import McpClientConfig, McpServerEntryConfig
from codebase_rag.services.mcp_client.models import (
    ServerNotFoundError,
    ToolInfo,
)

logger = get_logger(__name__)


# =============================================================================
# Transport context managers — each yields (read_stream, write_stream)
# =============================================================================

@asynccontextmanager
async def _open_streamable_http_transport(
    cfg: McpServerEntryConfig,
) -> AsyncIterator[Tuple[Any, Any]]:
    """Open a streamable HTTP MCP connection."""
    from mcp.client.streamable_http import streamablehttp_client  # noqa: lazy
    async with streamablehttp_client(
        cfg.url,
        headers=cfg.headers if cfg.headers else None,
    ) as (read, write, _):
        yield read, write


@asynccontextmanager
async def _open_sse_transport(
    cfg: McpServerEntryConfig,
) -> AsyncIterator[Tuple[Any, Any]]:
    """Open an SSE MCP connection."""
    async with sse_client(
        cfg.url,
        headers=cfg.headers if cfg.headers else None,
    ) as (read, write):
        yield read, write


@asynccontextmanager
async def _open_stdio_transport(
    cfg: McpServerEntryConfig,
) -> AsyncIterator[Tuple[Any, Any]]:
    """Spawn a stdio MCP subprocess."""
    params = StdioServerParameters(
        command=cfg.command,
        args=cfg.args or [],
        env=cfg.env if cfg.env else None,
    )
    async with stdio_client(params) as (read, write):
        yield read, write


def _open_transport(cfg: McpServerEntryConfig):
    """Return the appropriate transport context manager for the config."""
    if cfg.transport == "stdio":
        if not cfg.command:
            raise ValueError("stdio transport requires 'command' field")
        return _open_stdio_transport(cfg)
    elif cfg.transport == "sse":
        if not cfg.url:
            raise ValueError("sse transport requires 'url' field")
        return _open_sse_transport(cfg)
    else:  # streamable_http (default)
        if not cfg.url:
            raise ValueError(f"{cfg.transport} transport requires 'url' field")
        return _open_streamable_http_transport(cfg)


# =============================================================================
# Server connection — wraps config + cached tool definitions
# =============================================================================

class _ServerConnection:
    """Manages connection to a single MCP Server."""

    def __init__(self, cfg: McpServerEntryConfig) -> None:
        self._cfg = cfg
        self.tools: List[ToolInfo] = []

    @property
    def display_endpoint(self) -> str:
        if self._cfg.transport == "stdio":
            return f"stdio:{self._cfg.command}"
        return self._cfg.url or ""

    async def discover_tools(self) -> None:
        """Connect and fetch tool definitions."""
        async with _open_transport(self._cfg) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                self.tools = [
                    ToolInfo(
                        name=t.name,
                        description=t.description or "",
                        parameters=t.inputSchema if hasattr(t, "inputSchema") else {},
                    )
                    for t in result.tools
                ]

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool via MCP protocol."""
        async with _open_transport(self._cfg) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                texts = []
                for block in (result.content or []):
                    if hasattr(block, "text"):
                        texts.append(block.text)
                return "\n".join(texts) if texts else ""


# =============================================================================
# McpClientService
# =============================================================================

class McpClientService:
    """MCP Client service — connects to MCP Servers via standard protocol.

    Supports streamable_http, sse, and stdio transports.

    Args:
        config: MCP Client configuration section from AppConfig.
    """

    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        self._servers: Dict[str, _ServerConnection] = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._config_manager.register_reconciler("mcp_client", self.reconcile)

    @property
    def _config(self) -> McpClientConfig:
        return self._config_manager.config.mcp_client

    # =========================================================================
    # Lifecycle (lazy — auto-connects on first use)
    # =========================================================================

    async def _ensure_initialized(self) -> None:
        """Lazy init: connect to all MCP Servers on first use."""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            logger.info("mcp_client_service_initializing")
            for name, server_cfg in self._config.servers.items():
                conn = _ServerConnection(server_cfg)
                try:
                    await conn.discover_tools()
                    self._servers[name] = conn
                    logger.info(
                        "mcp_client_server_connected",
                        server=name,
                        endpoint=conn.display_endpoint,
                        transport=server_cfg.transport,
                        tools_discovered=len(conn.tools),
                    )
                except Exception as e:
                    logger.warning(
                        "mcp_client_server_connect_failed",
                        server=name,
                        endpoint=conn.display_endpoint,
                        transport=server_cfg.transport,
                        error=str(e)[:200],
                    )
            self._initialized = True
            logger.info(
                "mcp_client_service_initialized",
                servers=list(self._servers.keys()),
            )

    async def reconcile(self, old_config: AppConfig, new_config: AppConfig) -> None:
        """Reconcile MCP server connections with new configuration."""
        if not self._initialized:
            return

        logger.info("mcp_client_service_reconciling")
        old_servers = old_config.mcp_client.servers
        new_servers = new_config.mcp_client.servers

        async with self._init_lock:
            # 1. Remove deleted servers
            for name in set(old_servers.keys()) - set(new_servers.keys()):
                self._servers.pop(name, None)
                logger.info("mcp_client_server_removed", server=name)

            # 2. Add or update servers
            for name, server_cfg in new_servers.items():
                if name not in self._servers or old_servers.get(name) != server_cfg:
                    conn = _ServerConnection(server_cfg)
                    try:
                        await conn.discover_tools()
                        self._servers[name] = conn
                        logger.info("mcp_client_server_reconciled", server=name)
                    except Exception as e:
                        logger.warning("mcp_client_server_reconcile_failed", server=name, error=str(e)[:200])

    async def shutdown(self) -> None:
        """Clear all server connections."""
        self._config_manager.unregister_reconciler("mcp_client")
        logger.info("mcp_client_service_shutting_down")
        self._servers.clear()
        self._initialized = False
        logger.info("mcp_client_service_shutdown")

    # =========================================================================
    # Tool discovery
    # =========================================================================

    async def list_tools(self, server_name: str) -> List[ToolInfo]:
        """List available tools on a specific server."""
        await self._ensure_initialized()
        conn = self._get_server(server_name)
        return list(conn.tools)

    # =========================================================================
    # Tool execution
    # =========================================================================

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> str:
        """Execute a tool on the specified server via MCP protocol."""
        await self._ensure_initialized()
        conn = self._get_server(server_name)
        return await conn.call_tool(tool_name, arguments)

    # =========================================================================
    # Internal
    # =========================================================================

    def _get_server(self, server_name: str) -> _ServerConnection:
        conn = self._servers.get(server_name)
        if not conn:
            raise ServerNotFoundError(
                f"MCP server '{server_name}' not configured. "
                f"Available: {list(self._servers.keys())}"
            )
        return conn
