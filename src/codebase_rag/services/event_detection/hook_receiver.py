"""HTTP hook receiver — lightweight local server for Git hook notifications.

Receives POST requests from injected Git hook scripts and forwards them
to the event detection service for classification.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from aiohttp import web

from codebase_rag.common.logging import get_logger
from codebase_rag.config.schema import HttpReceiverConfig
from codebase_rag.services.event_detection.models import HookEvent

logger = get_logger(__name__)


class HookReceiver:
    """Local HTTP server that receives Git hook notifications.

    Args:
        config: HTTP receiver configuration.
        on_hook_event: Async callback invoked for each received hook event.
    """

    def __init__(
        self,
        config: HttpReceiverConfig,
        on_hook_event: Callable[[HookEvent], Awaitable[None]],
    ) -> None:
        self._config = config
        self._on_hook_event = on_hook_event
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    @property
    def is_running(self) -> bool:
        return self._site is not None

    async def start(self) -> None:
        """Start the HTTP hook receiver server."""
        self._app = web.Application()
        self._app.router.add_post(self._config.endpoint, self._handle_hook)

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()

        self._site = web.TCPSite(
            self._runner,
            host=self._config.host,
            port=self._config.port,
        )
        await self._site.start()

        logger.info(
            "hook_receiver_started",
            host=self._config.host,
            port=self._config.port,
            endpoint=self._config.endpoint,
        )

    async def stop(self) -> None:
        """Stop the HTTP hook receiver server."""
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("hook_receiver_stopped")

    async def _handle_hook(self, request: web.Request) -> web.Response:
        """Handle incoming hook notification."""
        try:
            data = await request.json()
        except Exception:
            logger.warning("hook_receiver_invalid_json")
            return web.json_response(
                {"error": "invalid JSON"}, status=400
            )

        project_id = data.get("project_id")
        hook_type = data.get("hook_type")

        if not project_id or not hook_type:
            logger.warning(
                "hook_receiver_missing_fields",
                project_id=project_id,
                hook_type=hook_type,
            )
            return web.json_response(
                {"error": "missing project_id or hook_type"}, status=400
            )

        event = HookEvent(
            project_id=project_id,
            hook_type=hook_type,
            args=data.get("args", ""),
        )

        logger.info(
            "hook_event_received",
            project_id=project_id,
            hook_type=hook_type,
        )

        # Fire-and-forget: schedule callback without blocking the response
        asyncio.create_task(self._safe_callback(event))

        return web.json_response({"status": "ok"})

    async def _safe_callback(self, event: HookEvent) -> None:
        """Invoke callback with error handling."""
        try:
            await self._on_hook_event(event)
        except Exception:
            logger.exception(
                "hook_event_callback_error",
                project_id=event.project_id,
                hook_type=event.hook_type,
            )
