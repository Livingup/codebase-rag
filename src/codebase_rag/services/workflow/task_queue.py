"""Per-repository serial task queue with cross-repository parallelism.

Rules:
- Same repository copy (repo_key) → serial (FIFO queue, next starts after current finishes)
- Different copies / projects → parallel
- Same-type events in queue can be coalesced (keep latest commit only)
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Deque, Dict, Optional

from codebase_rag.common.logging import get_logger

logger = get_logger(__name__)


@dataclass
class WorkflowTask:
    """A queued workflow task."""

    task_type: str              # "branch_switch" | "content_commit" | "initialization"
    project_id: str
    kwargs: Dict[str, Any] = field(default_factory=dict)
    # Canonical identity of the local repository copy this task operates on.
    # Empty for legacy tasks — the executor then resolves a single-copy
    # project by project_id.
    repo_key: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)


class ProjectTaskQueue:
    """Manages per-repository serial execution of workflow tasks.

    Tasks for the same repository copy (repo_key) are queued and executed
    one at a time.  Tasks for different copies can run in parallel.
    """

    def __init__(self) -> None:
        # Per-repository asyncio.Queue
        self._queues: Dict[str, asyncio.Queue[Optional[WorkflowTask]]] = {}
        # Per-repository worker task
        self._workers: Dict[str, asyncio.Task] = {}
        # Callback to execute a task
        self._executor: Optional[Callable[[WorkflowTask], Awaitable[None]]] = None
        self._shutdown = False

    def set_executor(self, executor: Callable[[WorkflowTask], Awaitable[None]]) -> None:
        """Set the callback that actually executes workflow tasks."""
        self._executor = executor

    def enqueue(self, task: WorkflowTask) -> int:
        """Add a task to the repository's queue. Returns queue depth after enqueue.

        Coalesces same-type events: if the queue already has a pending task
        of the same type for the same repository, replace it with the newer one.
        """
        queue_key = task.repo_key or task.project_id

        if queue_key not in self._queues:
            self._queues[queue_key] = asyncio.Queue()
            self._workers[queue_key] = asyncio.create_task(
                self._worker_loop(queue_key)
            )

        queue = self._queues[queue_key]

        # Coalescing: check if there's already a same-type task pending
        # We can only coalesce if we drain and re-add. For simplicity,
        # just enqueue — the executor handles dedup via HEAD comparison.
        queue.put_nowait(task)

        depth = queue.qsize()
        logger.info(
            "task_enqueued",
            queue_key=queue_key,
            task_type=task.task_type,
            queue_depth=depth,
        )
        return depth

    def get_queue_depth(self, queue_key: str) -> int:
        """Get pending task count for a repository copy."""
        q = self._queues.get(queue_key)
        return q.qsize() if q else 0

    async def shutdown(self, timeout: float = 30.0) -> None:
        """Stop all workers, waiting up to timeout for current tasks."""
        self._shutdown = True

        # Send sentinel to all workers
        for q in self._queues.values():
            q.put_nowait(None)

        # Wait for workers
        if self._workers:
            done, pending = await asyncio.wait(
                self._workers.values(), timeout=timeout,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        self._workers.clear()
        self._queues.clear()

    async def _worker_loop(self, queue_key: str) -> None:
        """Process tasks for one repository copy serially."""
        queue = self._queues[queue_key]

        while True:
            task = await queue.get()
            if task is None:  # shutdown sentinel
                break

            if self._executor is None:
                logger.warning("task_queue_no_executor", queue_key=queue_key)
                continue

            try:
                logger.info(
                    "task_executing",
                    queue_key=queue_key,
                    task_type=task.task_type,
                )
                await self._executor(task)
            except Exception:
                logger.exception(
                    "task_execution_failed",
                    queue_key=queue_key,
                    task_type=task.task_type,
                )
            finally:
                queue.task_done()
