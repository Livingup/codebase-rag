"""Codebase RAG — unified service entry point.

Starts all services as a single process with two HTTP servers:
  1. Load configuration (YAML + env vars)
  2. Instantiate services in dependency order
  3. Initialize all services (async lifecycle)
  4. Start Management API server (default port 8080)
  5. Start MCP Server (default port 9090)

Usage:
    python -m codebase_rag                        # default config.yaml
    python -m codebase_rag --config /path/to.yaml # explicit config
    codebase-rag                                  # via pyproject.toml script entry
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from codebase_rag.common.logging import get_logger, setup_logging
from codebase_rag.common.token_counter import TokenCounter
from codebase_rag.config.loader import ConfigManager, load_config, resolve_config_path
from codebase_rag.config.schema import AppConfig

# Services
from codebase_rag.services.agent.service import AgentService
from codebase_rag.services.embedding.service import EmbeddingService
from codebase_rag.services.event_detection.service import EventDetectionService
from codebase_rag.services.git_operations.service import GitOperationsService
from codebase_rag.services.management_api.admin_ui import create_admin_router
from codebase_rag.services.management_api.routes import create_router as create_api_router
from codebase_rag.services.management_api.service import ManagementApiService
from codebase_rag.services.context_engine.service import ContextEngineService
from codebase_rag.services.mcp_client.service import McpClientService
from codebase_rag.services.knowledge_sync.service import KnowledgeSyncService
from codebase_rag.services.knowledge_sync.store import KnowledgeSyncStore
from codebase_rag.services.mcp_service.server import create_mcp_server
from codebase_rag.services.mcp_service.service import McpService
from codebase_rag.services.memory_qdrant.service import MemoryQdrantService
from codebase_rag.services.qdrant_storage.service import QdrantStorageService
from codebase_rag.services.workflow.service import WorkflowOrchestratorService

logger = get_logger(__name__)


class ServiceContainer:
    """Holds all service instances and manages their lifecycle."""

    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        cm = config_manager  # shorthand

        # --- Layer 0: no internal dependencies ---
        self.embedding = EmbeddingService(cm)
        self.git_ops = GitOperationsService(cm)
        self.qdrant_storage = QdrantStorageService(cm)
        self.memory_qdrant = MemoryQdrantService(cm)
        self.mcp_client = McpClientService(cm)  # lazy init
        self.token_counter = TokenCounter(embedding=self.embedding)
        self.agent = AgentService(cm, mcp_client=self.mcp_client, token_counter=self.token_counter)

        # --- Layer 1: single dependency ---
        self.event_detection = EventDetectionService(
            config_manager=cm,
            git_ops=self.git_ops,
        )
        self.knowledge_sync_store = KnowledgeSyncStore(
            store_dir=os.path.join(cm.config.workflow.data_dir, "knowledge_sync"),
        )
        self.knowledge_sync = KnowledgeSyncService(
            config_manager=cm,
            store=self.knowledge_sync_store,
            qdrant_storage=self.qdrant_storage,
            memory_qdrant=self.memory_qdrant,
        )

        # --- Layer 2: multiple dependencies ---
        self.workflow = WorkflowOrchestratorService(
            config_manager=cm,
            git_ops=self.git_ops,
            qdrant_storage=self.qdrant_storage,
            memory_qdrant=self.memory_qdrant,
            agent=self.agent,
            embedding=self.embedding,
            token_counter=self.token_counter,
            knowledge_sync=self.knowledge_sync,
        )

        # --- Layer 3: top-level entry services ---
        self.management_api = ManagementApiService(
            config_manager=cm,
            git_ops=self.git_ops,
            event_detection=self.event_detection,
            workflow=self.workflow,
            memory_qdrant=self.memory_qdrant,
            qdrant_storage=self.qdrant_storage,
            embedding=self.embedding,
            knowledge_sync=self.knowledge_sync,
        )
        self.mcp_service = McpService(
            config_manager=cm,
            embedding=self.embedding,
            memory_qdrant=self.memory_qdrant,
            agent=self.agent,
            git_ops=self.git_ops,
            qdrant_storage=self.qdrant_storage,
            knowledge_sync=self.knowledge_sync,
        )
        self.context_engine = ContextEngineService(
            config_manager=cm,
            agent=self.agent,
            mcp_client=self.mcp_client,
        )

    async def initialize(self) -> None:
        """Initialize all services in dependency order."""
        logger.info("services_initializing")

        # Layer 0
        await self.embedding.initialize()
        await self.git_ops.initialize()
        await self.qdrant_storage.initialize()
        await self.memory_qdrant.initialize()
        await self.agent.initialize()

        # Layer 1
        await self.event_detection.initialize()
        await self.knowledge_sync.initialize()

        # Wire event detection → workflow
        self.event_detection.subscribe(self.workflow.on_git_event)

        # Layer 2
        await self.workflow.initialize()

        # Wire lazy rebuild: memory_qdrant → workflow
        self.memory_qdrant.set_rebuild_handler(self.workflow.rebuild_memory_instance)

        # Layer 3
        await self.management_api.initialize()
        await self.mcp_service.initialize()

        logger.info("services_initialized")

    async def shutdown(self) -> None:
        """Shutdown all services in reverse order."""
        logger.info("services_shutting_down")

        # Layer 3 (reverse)
        await self.mcp_client.shutdown()
        await self.mcp_service.shutdown()
        await self.management_api.shutdown()

        # Layer 2
        await self.workflow.shutdown()
        await self.knowledge_sync.shutdown()

        # Layer 1
        await self.event_detection.shutdown()

        # Layer 0
        await self.agent.shutdown()
        await self.memory_qdrant.shutdown()
        await self.qdrant_storage.shutdown()
        await self.git_ops.shutdown()
        await self.embedding.shutdown()

        logger.info("services_shutdown_complete")


# =============================================================================
# Management API app (port 8080)
# =============================================================================


def create_api_app(config: AppConfig, container: ServiceContainer) -> FastAPI:
    """Create the Management API FastAPI application.

    Lifespan is managed by the main loop, not by this app.
    """
    app = FastAPI(
        title="Codebase RAG — Management API",
        version="0.1.0",
    )

    admin_router = create_admin_router(container.management_api)
    app.include_router(admin_router)

    api_router = create_api_router(container.management_api)
    app.include_router(api_router, prefix="/api/v1")

    return app


# =============================================================================
# MCP Server app (port 9090)
# =============================================================================


def create_mcp_app(server_name: str, container: ServiceContainer):
    """Create an MCP Server ASGI application.

    Supports two transports simultaneously on the same port:
      - SSE:              GET /sse + POST /messages  (legacy, MCP ≤ 2024-11-05)
      - Streamable HTTP:  POST|GET|DELETE /mcp       (MCP ≥ 2025-03-26)

    Clients connecting via SSE use ``http://host:port/sse``.
    Clients connecting via Streamable HTTP use ``http://host:port/mcp``.
    """
    import contextlib

    from mcp.server.sse import SseServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Route

    mcp_server = create_mcp_server(
        server_name, container.mcp_service, container.management_api,
        context_engine=container.context_engine,
    )

    # --- SSE transport (legacy) ---
    sse = SseServerTransport("/messages")

    class _SseHandler:
        async def __call__(self, scope, receive, send):
            async with sse.connect_sse(scope, receive, send) as streams:
                await mcp_server.run(
                    streams[0], streams[1],
                    mcp_server.create_initialization_options(),
                )

    class _JsonRpcHandler:
        async def __call__(self, scope, receive, send):
            await sse.handle_post_message(scope, receive, send)

    # --- Streamable HTTP transport (modern) ---
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        stateless=True,
    )

    class _StreamableHTTPHandler:
        async def __call__(self, scope, receive, send):
            await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            yield

    app = Starlette(
        routes=[
            # SSE transport endpoints
            Route("/sse", endpoint=_SseHandler()),
            Route("/messages", endpoint=_JsonRpcHandler(), methods=["POST"]),
            # Streamable HTTP transport endpoint
            Route("/mcp", endpoint=_StreamableHTTPHandler()),
        ],
        lifespan=lifespan,
    )

    return app


# =============================================================================
# Async main loop
# =============================================================================


async def run_servers(config_manager: ConfigManager) -> None:
    """Initialize services and run both HTTP servers concurrently."""
    config = config_manager.config
    container = ServiceContainer(config_manager)
    await container.initialize()

    # Create ASGI apps
    api_app = create_api_app(config, container)

    # Management API server
    api_cfg = config.management_api.server
    api_server = uvicorn.Server(uvicorn.Config(
        api_app,
        host=api_cfg.host,
        port=api_cfg.port,
        log_level="warning",
    ))

    # MCP servers (one per config entry)
    mcp_servers = []
    for server_id, srv_cfg in config.mcp.servers.items():
        mcp_app = create_mcp_app(srv_cfg.name, container)
        uvi_server = uvicorn.Server(uvicorn.Config(
            mcp_app,
            host=srv_cfg.host,
            port=srv_cfg.port,
            log_level="warning",
        ))
        mcp_servers.append(uvi_server)
        logger.info(
            "mcp_server_configured",
            server_id=server_id,
            name=srv_cfg.name,
            host=srv_cfg.host,
            port=srv_cfg.port,
        )

    logger.info(
        "servers_starting",
        api_host=api_cfg.host, api_port=api_cfg.port,
        mcp_server_count=len(mcp_servers),
    )

    try:
        await asyncio.gather(
            api_server.serve(),
            *(s.serve() for s in mcp_servers),
        )
    finally:
        await container.shutdown()


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codebase-rag",
        description="Codebase RAG — Git-aware semantic code knowledge base",
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=None,
        help="Path to config YAML file (default: auto-detect)",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point for the unified Codebase RAG service."""
    args = parse_args()

    # Resolve config path
    config_path = args.config or resolve_config_path()

    # Load config
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: Failed to load config: {e}", file=sys.stderr)
        sys.exit(1)

    # Create ConfigManager (Single Source of Truth for runtime config)
    config_manager = ConfigManager(config, Path(config_path))

    # Setup logging
    setup_logging(
        level=config.system.log.level,
        format=config.system.log.format,
        file=config.system.log.file,
        error_file=config.system.log.error_file,
        retention_days=config.system.log.retention_days,
        max_bytes=config.system.log.max_bytes,
        backup_count=config.system.log.backup_count,
    )

    mcp_ports = [srv.port for srv in config.mcp.servers.values()]
    logger.info(
        "codebase_rag_starting",
        config_path=str(config_path),
        api_port=config.management_api.server.port,
        mcp_ports=mcp_ports,
    )

    # Run both servers in a single event loop
    asyncio.run(run_servers(config_manager))


if __name__ == "__main__":
    main()
