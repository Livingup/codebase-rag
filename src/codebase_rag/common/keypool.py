"""API Key pool with round-robin scheduling and health tracking.

Shared by Embedding service and Agent service (同构设计).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Optional

from codebase_rag.common.logging import get_logger

logger = get_logger(__name__)


@dataclass
class KeyState:
    """Health state for a single API key."""

    key: str
    healthy: bool = True
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    cooldown_until: float = 0.0


class KeyPool:
    """Round-robin API key pool with health tracking and cooldown.

    Level 1: Single-key retry (caller handles via retry logic).
    Level 2: Key rotation — on persistent failure, mark key unhealthy and try next.

    Args:
        keys: List of API key strings.
        cooldown_seconds: Seconds to keep a key in cooldown after marking unhealthy.
        max_consecutive_failures: Failures before marking a key unhealthy.
    """

    def __init__(
        self,
        keys: List[str],
        cooldown_seconds: int = 60,
        max_consecutive_failures: int = 3,
    ) -> None:
        if not keys:
            raise ValueError("KeyPool requires at least one API key")

        self._states: List[KeyState] = [KeyState(key=k) for k in keys]
        self._cooldown_seconds = cooldown_seconds
        self._max_consecutive_failures = max_consecutive_failures
        self._index = 0
        self._lock = asyncio.Lock()

    @property
    def total_keys(self) -> int:
        return len(self._states)

    @property
    def healthy_keys(self) -> int:
        now = time.monotonic()
        return sum(1 for s in self._states if s.healthy or now >= s.cooldown_until)

    async def get_key(self) -> Optional[str]:
        """Get next available API key via round-robin.

        Returns None if all keys are unhealthy and in cooldown.
        Automatically recovers keys whose cooldown has expired.
        """
        async with self._lock:
            now = time.monotonic()

            # Try to recover cooled-down keys
            for state in self._states:
                if not state.healthy and now >= state.cooldown_until:
                    state.healthy = True
                    state.consecutive_failures = 0
                    logger.info("key_recovered", key_suffix=state.key[-6:])

            # Round-robin through all keys, find next healthy one
            for _ in range(len(self._states)):
                state = self._states[self._index]
                self._index = (self._index + 1) % len(self._states)
                if state.healthy:
                    return state.key

            return None

    async def report_success(self, key: str) -> None:
        """Report a successful API call with this key."""
        async with self._lock:
            for state in self._states:
                if state.key == key:
                    state.consecutive_failures = 0
                    break

    async def report_failure(self, key: str) -> None:
        """Report a failed API call. May mark key unhealthy after threshold."""
        async with self._lock:
            for state in self._states:
                if state.key == key:
                    state.consecutive_failures += 1
                    state.last_failure_time = time.monotonic()

                    if state.consecutive_failures >= self._max_consecutive_failures:
                        state.healthy = False
                        state.cooldown_until = time.monotonic() + self._cooldown_seconds
                        logger.warning(
                            "key_marked_unhealthy",
                            key_suffix=key[-6:],
                            consecutive_failures=state.consecutive_failures,
                            cooldown_seconds=self._cooldown_seconds,
                        )
                    break

    async def mark_unhealthy(self, key: str) -> None:
        """Immediately mark a key as unhealthy (e.g., invalid key, auth error)."""
        async with self._lock:
            for state in self._states:
                if state.key == key:
                    state.healthy = False
                    state.cooldown_until = time.monotonic() + self._cooldown_seconds
                    logger.warning("key_force_unhealthy", key_suffix=key[-6:])
                    break

    def peek(self) -> str:
        """Return a healthy key without advancing round-robin. Non-async, best-effort."""
        now = time.monotonic()
        for state in self._states:
            if state.healthy or now >= state.cooldown_until:
                return state.key
        return self._states[0].key if self._states else ""

    def get_status(self) -> List[dict]:
        """Get status of all keys for diagnostics."""
        now = time.monotonic()
        return [
            {
                "key_suffix": s.key[-6:],
                "healthy": s.healthy,
                "consecutive_failures": s.consecutive_failures,
                "cooldown_remaining": max(0, s.cooldown_until - now) if not s.healthy else 0,
            }
            for s in self._states
        ]
