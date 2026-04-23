"""File system watcher — fallback detection for Git HEAD changes.

Polls .git/HEAD and branch ref files to detect HEAD changes that may not
be captured by Git hooks (e.g., git reset --hard, external tools).

Uses polling because watchfiles (ReadDirectoryChangesW) on Windows cannot
reliably detect changes inside .git/ directories.  Polling is acceptable
here because this is a *fallback* mechanism — hooks are the primary
detection path — and the poll interval matches `debounce_ms` (default 500ms).
"""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional, Tuple

from codebase_rag.common.logging import get_logger
from codebase_rag.config.schema import WatcherConfig

logger = get_logger(__name__)


class GitWatcher:
    """Per-project poller monitoring .git/HEAD for HEAD changes.

    Args:
        project_id: The project being watched.
        git_repo_path: Absolute path to the Git repository root.
        config: Watcher configuration.
        on_change: Async callback invoked when a change is detected.
    """

    def __init__(
        self,
        project_id: str,
        git_repo_path: str,
        config: WatcherConfig,
        on_change: Callable[[str], Awaitable[None]],
    ) -> None:
        self._project_id = project_id
        self._git_repo_path = git_repo_path
        self._config = config
        self._on_change = on_change
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start watching in a background task."""
        if self.is_active:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "watcher_started",
            project_id=self._project_id,
            repo=self._git_repo_path,
        )

    async def stop(self) -> None:
        """Stop the watcher and wait for cleanup."""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info(
            "watcher_stopped",
            project_id=self._project_id,
        )

    # ------------------------------------------------------------------
    # Internal polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll .git/HEAD and resolved branch ref for changes."""
        git_dir = os.path.join(self._git_repo_path, ".git")
        if not os.path.isdir(git_dir):
            logger.error(
                "watcher_git_dir_not_found",
                project_id=self._project_id,
                git_dir=git_dir,
            )
            return

        poll_interval_s = self._config.debounce_ms / 1000.0

        # Capture initial snapshot
        last_snapshot = self._read_head_snapshot(git_dir)

        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(poll_interval_s)

                current = self._read_head_snapshot(git_dir)
                if current != last_snapshot:
                    logger.debug(
                        "watcher_change_detected",
                        project_id=self._project_id,
                        prev=last_snapshot,
                        curr=current,
                    )
                    last_snapshot = current

                    try:
                        await self._on_change(self._project_id)
                    except Exception:
                        logger.exception(
                            "watcher_callback_error",
                            project_id=self._project_id,
                        )
        except asyncio.CancelledError:
            return

    @staticmethod
    def _read_head_snapshot(git_dir: str) -> Tuple[str, str]:
        """Read .git/HEAD content and resolved commit hash.

        Returns a tuple (head_content, resolved_ref_content) that changes
        whenever the branch pointer or HEAD itself changes.
        """
        head_path = os.path.join(git_dir, "HEAD")
        try:
            with open(head_path, "r", encoding="utf-8") as f:
                head_content = f.read().strip()
        except OSError:
            return ("", "")

        # If HEAD points to a ref (e.g. "ref: refs/heads/master"),
        # also read the ref file to detect new commits on that branch.
        resolved = ""
        if head_content.startswith("ref: "):
            ref_rel = head_content[5:].strip()
            ref_path = os.path.join(git_dir, ref_rel)
            try:
                with open(ref_path, "r", encoding="utf-8") as f:
                    resolved = f.read().strip()
            except OSError:
                pass

        return (head_content, resolved)
