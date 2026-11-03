"""Event detection service — detects Git HEAD changes via hooks and filesystem watcher.

Dual detection: Git Hooks (primary) + filesystem watcher (fallback).
Classifies events into BRANCH_SWITCH or CONTENT_COMMIT and publishes
structured GitEvent to subscribers (Workflow Orchestrator).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import stat
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from codebase_rag.common.logging import get_logger
from codebase_rag.common.paths import normalize_repo_key
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import EventDetectionConfig
from codebase_rag.services.event_detection.errors import (
    GitRepoNotFoundError,
    HookConflictError,
    HookInjectionError,
    ProjectNotRegisteredError,
    WatcherError,
)
from codebase_rag.services.event_detection.hook_receiver import HookReceiver
from codebase_rag.services.event_detection.models import (
    EventSource,
    EventType,
    GitEvent,
    GitEventCallback,
    HookEvent,
    HookInstallStatus,
    HookStatus,
    HookType,
    ProjectHeadState,
    ProjectHeadStatus,
)
from codebase_rag.services.event_detection.watcher import GitWatcher
from codebase_rag.services.git_operations.service import GitOperationsService

logger = get_logger(__name__)

# Marker comment to identify hooks managed by this service
HOOK_MARKER = "# Codebase RAG Event Hook"
HOOK_BACKUP_SUFFIX = ".codebase-rag-backup"

# All hook types we manage
MANAGED_HOOKS = [
    HookType.POST_COMMIT,
    HookType.POST_MERGE,
    HookType.POST_CHECKOUT,
    HookType.POST_REWRITE,
]


class EventDetectionService:
    """Detects Git HEAD changes and publishes structured events.

    Args:
        config: Event detection configuration section from AppConfig.
        git_ops: Git operations service for reading HEAD state.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        git_ops: GitOperationsService,
    ) -> None:
        self._config_manager = config_manager
        self._git_ops = git_ops

        # Project registry: project_id → state
        self._projects: Dict[str, ProjectHeadState] = {}
        self._projects_lock = asyncio.Lock()

        # Per-project watchers
        self._watchers: Dict[str, GitWatcher] = {}

        # Event subscribers (callbacks)
        self._subscribers: List[GitEventCallback] = []

        # Dedup tracking: project_id → last notification monotonic time
        self._dedup_timestamps: Dict[str, float] = {}

        # Hook receiver (HTTP mode)
        self._hook_receiver: Optional[HookReceiver] = None

        self._initialized = False

        # Periodic hook health check task
        self._hook_check_task: Optional[asyncio.Task] = None
        self._hook_check_interval_s: float = 60.0  # check every 60s

    @property
    def _config(self) -> EventDetectionConfig:
        return self._config_manager.config.event_detection

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Initialize the service: start hook receiver channel."""
        logger.info("event_detection_service_initializing")

        if self._config.hook_receiver.mode == "http":
            self._hook_receiver = HookReceiver(
                config=self._config.hook_receiver.http,
                on_hook_event=self._on_hook_event,
            )
            await self._hook_receiver.start()

        # Start periodic hook health check
        self._hook_check_task = asyncio.create_task(self._periodic_hook_check())

        self._initialized = True
        self._config_manager.register_reconciler("event_detection", self.reconcile)
        logger.info(
            "event_detection_service_initialized",
            mode=self._config.hook_receiver.mode,
            watcher_enabled=self._config.watcher.enabled,
        )

    async def reconcile(self, old_config: AppConfig, new_config: AppConfig) -> None:
        """Reconcile event detection configuration (watchers)."""
        await self.reconcile_watchers()

    async def shutdown(self) -> None:
        """Graceful shutdown: stop watchers and hook receiver.

        Does NOT remove hook scripts (they can be reused on next startup).
        """
        self._config_manager.unregister_reconciler("event_detection")
        logger.info("event_detection_service_shutting_down")

        # Cancel periodic hook health check
        if self._hook_check_task is not None:
            self._hook_check_task.cancel()
            try:
                await self._hook_check_task
            except asyncio.CancelledError:
                pass
            self._hook_check_task = None

        # Stop all watchers
        for project_id, watcher in list(self._watchers.items()):
            await watcher.stop()
        self._watchers.clear()

        # Stop hook receiver
        if self._hook_receiver:
            await self._hook_receiver.stop()
            self._hook_receiver = None

        self._projects.clear()
        self._dedup_timestamps.clear()
        self._subscribers.clear()
        self._initialized = False

        logger.info("event_detection_service_shutdown")

    def is_healthy(self) -> bool:
        if not self._initialized:
            return False
        if self._config.hook_receiver.mode == "http":
            return self._hook_receiver is not None and self._hook_receiver.is_running
        return True

    async def reconcile_watchers(self) -> None:
        started = 0
        stopped = 0
        async with self._projects_lock:
            for state in self._projects.values():
                action = await self._sync_project_watcher(state)
                if action == "started":
                    started += 1
                elif action == "stopped":
                    stopped += 1

        logger.info(
            "watchers_reconciled",
            enabled=self._config.watcher.enabled,
            started=started,
            stopped=stopped,
        )

    # =========================================================================
    # 2.1 Project registration
    # =========================================================================

    async def register_project(self, project_id: str, git_repo_path: str) -> bool:
        """Register a repository copy for event monitoring.

        1. Validates .git/ exists
        2. Injects hook scripts
        3. Starts filesystem watcher (if enabled)
        4. Reads initial HEAD state as baseline

        Registration is keyed by the canonical repo_key of the local copy:
        registering another copy of the same project (same project_id,
        different path) adds an independent entry instead of replacing
        this one.

        Returns True on success.
        """
        git_dir = os.path.join(git_repo_path, ".git")
        if not os.path.isdir(git_dir):
            raise GitRepoNotFoundError(git_repo_path)

        head = await self._git_ops.get_head_info(git_repo_path)
        repo_key = normalize_repo_key(git_repo_path)

        async with self._projects_lock:
            existing = self._projects.get(repo_key)
            if existing is not None:
                path_changed = existing.git_repo_path != git_repo_path
                old_path = existing.git_repo_path
                existing.project_id = project_id
                existing.git_repo_path = git_repo_path

                await self.inject_hooks(project_id, git_repo_path)
                existing.branch_name = head.branch_name
                existing.commit_hash = head.commit_hash
                action = await self._sync_project_watcher(existing)

                logger.info(
                    "project_registration_refreshed",
                    project_id=project_id,
                    repo=git_repo_path,
                    old_repo=old_path if path_changed else None,
                    path_changed=path_changed,
                    watcher_action=action,
                    branch=head.branch_name,
                    commit=head.commit_hash[:8] if head.commit_hash else None,
                )
                return True

            # Inject hooks
            await self.inject_hooks(project_id, git_repo_path)

            state = ProjectHeadState(
                project_id=project_id,
                git_repo_path=git_repo_path,
                branch_name=head.branch_name,
                commit_hash=head.commit_hash,
                repo_key=repo_key,
            )

            await self._sync_project_watcher(state)

            self._projects[repo_key] = state

        logger.info(
            "project_registered",
            project_id=project_id,
            repo=git_repo_path,
            branch=head.branch_name,
            commit=head.commit_hash[:8] if head.commit_hash else None,
        )
        return True

    # =========================================================================
    # 2.1b Project restoration (restart recovery — section 3.4)
    # =========================================================================

    async def restore_project(
        self,
        project_id: str,
        git_repo_path: str,
        last_commit_hash: str,
        last_branch_name: Optional[str],
    ) -> bool:
        """Restore a previously registered project after process restart.

        Like register_project, but uses the caller-provided last known HEAD
        state as the baseline.  If the current HEAD differs from that baseline,
        a catch-up GitEvent is published immediately so the Workflow
        Orchestrator can reconcile any changes that happened while the process
        was down.

        Returns True on success.
        """
        git_dir = os.path.join(git_repo_path, ".git")
        if not os.path.isdir(git_dir):
            raise GitRepoNotFoundError(git_repo_path)

        repo_key = normalize_repo_key(git_repo_path)

        async with self._projects_lock:
            if repo_key in self._projects:
                logger.info(
                    "project_already_registered",
                    project_id=project_id,
                )
                return True

            # Inject / refresh hooks
            await self.inject_hooks(project_id, git_repo_path)

            # Read current HEAD
            head = await self._git_ops.get_head_info(git_repo_path)

            # Register with the *old* baseline so _on_raw_notification can
            # detect the delta later.  But we also want to emit an immediate
            # catch-up event if there is a delta.
            state = ProjectHeadState(
                project_id=project_id,
                git_repo_path=git_repo_path,
                branch_name=last_branch_name,
                commit_hash=last_commit_hash,
                repo_key=repo_key,
            )

            await self._sync_project_watcher(state)

            self._projects[repo_key] = state

        logger.info(
            "project_restored",
            project_id=project_id,
            repo=git_repo_path,
            last_branch=last_branch_name,
            last_commit=last_commit_hash[:8] if last_commit_hash else None,
            current_branch=head.branch_name,
            current_commit=head.commit_hash[:8] if head.commit_hash else None,
        )

        # Detect delta and publish catch-up event if needed
        current_branch = head.branch_name
        current_commit = head.commit_hash

        event_type: Optional[EventType] = None
        if current_branch != last_branch_name:
            event_type = EventType.BRANCH_SWITCH
        elif current_commit != last_commit_hash:
            event_type = EventType.CONTENT_COMMIT

        if event_type is not None:
            # Update state to current
            state.branch_name = current_branch
            state.commit_hash = current_commit
            state.last_event_time = datetime.utcnow()

            git_event = GitEvent(
                event_id=str(uuid.uuid4()),
                project_id=project_id,
                event_type=event_type,
                source=EventSource.WATCHER,  # treat startup recovery as watcher source
                branch_name=current_branch,
                commit_hash=current_commit,
                previous_branch_name=last_branch_name,
                previous_commit_hash=last_commit_hash or "",
                git_repo_path=git_repo_path,
                timestamp=datetime.utcnow(),
            )
            logger.info(
                "restart_catchup_event",
                event_id=git_event.event_id,
                project_id=project_id,
                event_type=event_type.value,
                prev_commit=last_commit_hash[:8] if last_commit_hash else None,
                curr_commit=current_commit[:8] if current_commit else None,
            )
            await self._publish_event(git_event)

        return True

    # =========================================================================
    # 2.2 Hook script management
    # =========================================================================

    async def inject_hooks(self, project_id: str, git_repo_path: str) -> None:
        """Inject all managed hook scripts into .git/hooks/."""
        git_dir = os.path.join(git_repo_path, ".git")
        if not os.path.exists(git_dir):
            # The copy is gone (moved or deleted): creating the directory
            # here would resurrect a ghost .git at a dead path.
            logger.warning(
                "hook_injection_skipped_missing_repo",
                project_id=project_id,
                repo=git_repo_path,
            )
            return

        hooks_dir = os.path.join(git_dir, "hooks")
        os.makedirs(hooks_dir, exist_ok=True)

        for hook_type in MANAGED_HOOKS:
            try:
                self._inject_single_hook(
                    project_id, git_repo_path, hooks_dir, hook_type.value
                )
            except Exception as e:
                logger.error(
                    "hook_injection_failed",
                    project_id=project_id,
                    hook_type=hook_type.value,
                    error=str(e),
                )
                raise HookInjectionError(
                    hook_type.value, git_repo_path, str(e)
                ) from e

        logger.info(
            "hooks_injected",
            project_id=project_id,
            repo=git_repo_path,
            hooks=[h.value for h in MANAGED_HOOKS],
        )

    async def remove_hooks(self, project_id: str, git_repo_path: str) -> None:
        """Remove injected hook scripts, restoring user originals if backed up."""
        hooks_dir = os.path.join(git_repo_path, ".git", "hooks")

        for hook_type in MANAGED_HOOKS:
            hook_path = os.path.join(hooks_dir, hook_type.value)
            backup_path = hook_path + HOOK_BACKUP_SUFFIX

            if not os.path.exists(hook_path):
                continue

            # Only remove if it's ours
            try:
                with open(hook_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue

            if HOOK_MARKER not in content:
                continue

            os.remove(hook_path)

            # Restore backup if exists
            if os.path.exists(backup_path):
                os.rename(backup_path, hook_path)
                logger.info(
                    "hook_backup_restored",
                    hook_type=hook_type.value,
                    repo=git_repo_path,
                )

        logger.info(
            "hooks_removed",
            project_id=project_id,
            repo=git_repo_path,
        )

    async def check_hooks_status(
        self, project_id: str, git_repo_path: str
    ) -> HookStatus:
        """Check installation status of all managed hooks."""
        hooks_dir = os.path.join(git_repo_path, ".git", "hooks")
        status = HookStatus()

        for hook_type in MANAGED_HOOKS:
            hook_path = os.path.join(hooks_dir, hook_type.value)
            install_status = self._check_single_hook_status(hook_path)
            setattr(status, hook_type.value.replace("-", "_"), install_status)

        return status

    # =========================================================================
    # 2.3 Hook event reception
    # =========================================================================

    async def _on_hook_event(self, event: HookEvent) -> None:
        """Handle a raw hook event from the hook receiver."""
        if event.git_repo_path:
            await self._on_raw_notification(
                normalize_repo_key(event.git_repo_path), EventSource.HOOK,
            )
            return
        # Legacy hooks without repo_path: route only when the project has
        # exactly one registered copy; otherwise the source is ambiguous.
        candidates = [
            key for key, state in self._projects.items()
            if state.project_id == event.project_id
        ]
        if len(candidates) == 1:
            await self._on_raw_notification(candidates[0], EventSource.HOOK)
        elif len(candidates) > 1:
            logger.warning(
                "hook_event_ambiguous_project_copies",
                project_id=event.project_id,
                copies=len(candidates),
            )

    # =========================================================================
    # 2.4 Watcher change callback
    # =========================================================================

    async def _on_watcher_change(self, repo_key: str) -> None:
        """Handle a filesystem change detected by the watcher."""
        await self._on_raw_notification(repo_key, EventSource.WATCHER)

    # =========================================================================
    # 2.5 Event classification (core logic)
    # =========================================================================

    async def _on_raw_notification(
        self, repo_key: str, source: EventSource
    ) -> None:
        """Process a raw notification: dedup → read HEAD → classify → publish."""
        # Check repository copy is registered
        state = self._projects.get(repo_key)
        if state is None:
            logger.debug(
                "notification_for_unregistered_repo",
                repo_key=repo_key,
                source=source.value,
            )
            return

        # Dedup check
        now = time.monotonic()
        window_s = self._config.dedup.window_ms / 1000.0
        last = self._dedup_timestamps.get(repo_key, 0.0)
        if (now - last) < window_s:
            logger.debug(
                "event_deduped",
                repo_key=repo_key,
                source=source.value,
            )
            return
        self._dedup_timestamps[repo_key] = now

        # Read current HEAD state
        try:
            head = await self._git_ops.get_head_info(state.git_repo_path)
        except Exception:
            logger.exception(
                "head_read_failed",
                project_id=state.project_id,
                repo_key=repo_key,
                repo=state.git_repo_path,
            )
            return

        current_branch = head.branch_name
        current_commit = head.commit_hash
        prev_branch = state.branch_name
        prev_commit = state.commit_hash

        # Classify event
        event_type: Optional[EventType] = None

        if current_branch != prev_branch:
            event_type = EventType.BRANCH_SWITCH
        elif current_commit != prev_commit:
            event_type = EventType.CONTENT_COMMIT
        else:
            # No change
            logger.debug(
                "event_no_change",
                repo_key=repo_key,
                source=source.value,
                branch=current_branch,
                commit=current_commit[:8] if current_commit else None,
            )
            return

        # Update state
        state.branch_name = current_branch
        state.commit_hash = current_commit
        state.last_event_time = datetime.utcnow()

        # Build and publish event
        git_event = GitEvent(
            event_id=str(uuid.uuid4()),
            project_id=state.project_id,
            event_type=event_type,
            source=source,
            branch_name=current_branch,
            commit_hash=current_commit,
            previous_branch_name=prev_branch,
            previous_commit_hash=prev_commit or "",
            git_repo_path=state.git_repo_path,
            timestamp=datetime.utcnow(),
        )

        logger.info(
            "event_classified",
            event_id=git_event.event_id,
            project_id=state.project_id,
            repo_key=repo_key,
            event_type=event_type.value,
            source=source.value,
            branch=current_branch,
            commit=current_commit[:8] if current_commit else None,
            prev_branch=prev_branch,
            prev_commit=prev_commit[:8] if prev_commit else None,
        )

        await self._publish_event(git_event)

    # =========================================================================
    # 2.6 Event publishing
    # =========================================================================

    def subscribe(self, callback: GitEventCallback) -> None:
        """Register a subscriber callback for GitEvent notifications."""
        self._subscribers.append(callback)
        logger.debug("subscriber_added", total=len(self._subscribers))

    def unsubscribe(self, callback: GitEventCallback) -> None:
        """Remove a subscriber callback."""
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    async def _publish_event(self, event: GitEvent) -> None:
        """Publish event to all subscribers (fire-and-forget)."""
        for callback in self._subscribers:
            try:
                asyncio.create_task(callback(event))
            except Exception:
                logger.exception(
                    "event_publish_error",
                    event_id=event.event_id,
                )

        logger.info(
            "event_published",
            event_id=event.event_id,
            project_id=event.project_id,
            event_type=event.event_type.value,
            subscribers=len(self._subscribers),
        )

    # =========================================================================
    # 2.6b Periodic hook health check (section 3.4)
    # =========================================================================

    async def _periodic_hook_check(self) -> None:
        """Periodically verify hooks are intact; re-inject if missing."""
        try:
            while True:
                await asyncio.sleep(self._hook_check_interval_s)
                await self._check_and_repair_hooks()
        except asyncio.CancelledError:
            return

    async def _check_and_repair_hooks(self) -> None:
        """Check all registered repositories' hooks and re-inject if needed."""
        # Snapshot current registrations (avoid holding lock during I/O)
        async with self._projects_lock:
            snapshot = list(self._projects.items())

        for repo_key, state in snapshot:
            if not os.path.exists(os.path.join(state.git_repo_path, ".git")):
                # Never repair hooks for a copy that no longer exists; the
                # repair itself would recreate the directory tree.
                logger.warning(
                    "hook_health_check_skipped_missing_repo",
                    project_id=state.project_id,
                    repo_key=repo_key,
                )
                continue
            try:
                status = await self.check_hooks_status(
                    state.project_id, state.git_repo_path,
                )
                needs_repair = any(
                    getattr(status, hook.value.replace("-", "_"))
                    != HookInstallStatus.INSTALLED
                    for hook in MANAGED_HOOKS
                )
                if needs_repair:
                    logger.warning(
                        "hooks_damaged_reinjecting",
                        project_id=state.project_id,
                        repo_key=repo_key,
                        post_commit=status.post_commit.value,
                        post_merge=status.post_merge.value,
                        post_checkout=status.post_checkout.value,
                        post_rewrite=status.post_rewrite.value,
                    )
                    await self.inject_hooks(state.project_id, state.git_repo_path)
            except Exception:
                logger.exception(
                    "hook_health_check_error",
                    project_id=state.project_id,
                    repo_key=repo_key,
                )

    # =========================================================================
    # 2.7 Project status queries
    # =========================================================================

    def get_project_status(self, project_id: str) -> ProjectHeadStatus:
        """Get the current HEAD status of a monitored project.

        With multiple registered copies of the same project, the first
        registered copy is reported.
        """
        state = next(
            (s for s in self._projects.values() if s.project_id == project_id),
            None,
        )
        if state is None:
            raise ProjectNotRegisteredError(project_id)
        return self._build_head_status(state)

    def get_repo_status(self, repo_key: str) -> ProjectHeadStatus:
        """Get the current HEAD status of one registered repository copy."""
        state = self._projects.get(repo_key)
        if state is None:
            raise ProjectNotRegisteredError(repo_key)
        return self._build_head_status(state)

    def list_monitored_projects(self) -> List[ProjectHeadStatus]:
        """List all monitored repository copies with their status."""
        return [self._build_head_status(state) for state in self._projects.values()]

    def _build_head_status(self, state: ProjectHeadState) -> ProjectHeadStatus:
        hooks_dir = os.path.join(state.git_repo_path, ".git", "hooks")
        hooks_status = HookStatus()
        for hook_type in MANAGED_HOOKS:
            hook_path = os.path.join(hooks_dir, hook_type.value)
            install_status = self._check_single_hook_status(hook_path)
            setattr(hooks_status, hook_type.value.replace("-", "_"), install_status)

        return ProjectHeadStatus(
            project_id=state.project_id,
            git_repo_path=state.git_repo_path,
            branch_name=state.branch_name,
            commit_hash=state.commit_hash,
            hooks_status=hooks_status,
            watcher_active=state.watcher_active,
            last_event_time=state.last_event_time,
        )

    # =========================================================================
    # 2.8 Project unregistration
    # =========================================================================

    async def unregister_project(self, project_id: str) -> bool:
        """Unregister every local copy of a project.

        Stops watchers, removes hooks and clears state for all registered
        repository copies whose project_id matches.
        """
        removed = False
        async with self._projects_lock:
            targets = [
                (key, state) for key, state in self._projects.items()
                if state.project_id == project_id
            ]
            for repo_key, state in targets:
                # Stop watcher
                await self._stop_watcher(repo_key)

                # Remove hooks (restore user originals)
                try:
                    await self.remove_hooks(state.project_id, state.git_repo_path)
                except Exception:
                    logger.exception(
                        "hook_removal_failed_on_unregister",
                        project_id=state.project_id,
                        repo_key=repo_key,
                    )

                # Clear state
                del self._projects[repo_key]
                self._dedup_timestamps.pop(repo_key, None)
                removed = True

        logger.info("project_unregistered", project_id=project_id)
        return removed

    async def unregister_repo(self, git_repo_path: str) -> bool:
        """Unregister a single local repository copy.

        Other copies of the same project (same project_id, different
        paths) keep their watchers and hooks.
        """
        repo_key = normalize_repo_key(git_repo_path)
        async with self._projects_lock:
            state = self._projects.get(repo_key)
            if state is None:
                return False

            # Stop watcher
            await self._stop_watcher(repo_key)

            # Remove hooks (restore user originals)
            try:
                await self.remove_hooks(state.project_id, state.git_repo_path)
            except Exception:
                logger.exception(
                    "hook_removal_failed_on_unregister",
                    project_id=state.project_id,
                    repo_key=repo_key,
                )

            # Clear state
            del self._projects[repo_key]
            self._dedup_timestamps.pop(repo_key, None)

        logger.info(
            "repo_unregistered",
            project_id=state.project_id,
            repo_key=repo_key,
        )
        return True

    # =========================================================================
    # Internal: watcher lifecycle
    # =========================================================================

    async def _sync_project_watcher(self, state: ProjectHeadState) -> str:
        watcher = self._watchers.get(state.repo_key)
        if not self._config.watcher.enabled:
            if watcher:
                await self._stop_watcher(state.repo_key)
                return "stopped"
            state.watcher_active = False
            return "none"

        if watcher and watcher.is_active:
            state.watcher_active = True
            return "active"

        watcher = GitWatcher(
            project_id=state.project_id,
            git_repo_path=state.git_repo_path,
            repo_key=state.repo_key,
            config=self._config.watcher,
            on_change=self._on_watcher_change,
        )
        watcher.start()
        self._watchers[state.repo_key] = watcher
        state.watcher_active = True
        return "started"

    async def _stop_watcher(self, repo_key: str) -> bool:
        watcher = self._watchers.pop(repo_key, None)
        if watcher is None:
            state = self._projects.get(repo_key)
            if state is not None:
                state.watcher_active = False
            return False

        await watcher.stop()
        state = self._projects.get(repo_key)
        if state is not None:
            state.watcher_active = False
        return True

    # =========================================================================
    # Internal: hook script generation and management
    # =========================================================================

    def _inject_single_hook(
        self,
        project_id: str,
        git_repo_path: str,
        hooks_dir: str,
        hook_name: str,
    ) -> None:
        """Inject a single hook script, handling conflicts with user hooks."""
        hook_path = os.path.join(hooks_dir, hook_name)
        backup_path = hook_path + HOOK_BACKUP_SUFFIX
        new_content = self._generate_hook_script(project_id, git_repo_path, hook_name)

        if os.path.exists(hook_path):
            with open(hook_path, "r", encoding="utf-8") as f:
                existing = f.read()

            if HOOK_MARKER in existing:
                # Our hook already exists — overwrite with updated version
                with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(new_content)
            else:
                # User's own hook — backup and wrap
                if not os.path.exists(backup_path):
                    os.rename(hook_path, backup_path)
                    logger.info(
                        "user_hook_backed_up",
                        hook=hook_name,
                        backup=backup_path,
                    )

                wrapper = self._generate_wrapper_script(
                    project_id, git_repo_path, hook_name, backup_path
                )
                with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(wrapper)
        else:
            # No existing hook — create new
            with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_content)

        # Make executable on Unix
        if platform.system() != "Windows":
            os.chmod(hook_path, os.stat(hook_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _generate_hook_script(
        self, project_id: str, git_repo_path: str, hook_name: str,
    ) -> str:
        """Generate a hook script that notifies this service.

        The payload carries both the project_id (data partition) and the
        repository path (local copy identity) so events from multiple
        registered copies of the same project stay distinguishable.
        """
        payload = self._hook_payload(project_id, git_repo_path, hook_name)
        if self._config.hook_receiver.mode == "http":
            http_cfg = self._config.hook_receiver.http
            url = f"http://{http_cfg.host}:{http_cfg.port}{http_cfg.endpoint}"
            return (
                "#!/bin/sh\n"
                f"{HOOK_MARKER} - DO NOT EDIT\n"
                "# Managed by Codebase RAG Event Detection Service\n"
                "\n"
                f'curl -s -X POST "{url}" \\\n'
                '  -H "Content-Type: application/json" \\\n'
                f"  -d '{payload}' \\\n"
                "  --max-time 2 \\\n"
                "  || true  # Failure must not block Git operations\n"
            )
        else:
            # Named pipe mode
            pipe_path = self._config.hook_receiver.named_pipe.path
            return (
                "#!/bin/sh\n"
                f"{HOOK_MARKER} - DO NOT EDIT\n"
                "# Managed by Codebase RAG Event Detection Service\n"
                "\n"
                f"echo '{payload}' > \"{pipe_path}\" 2>/dev/null || true\n"
            )

    def _generate_wrapper_script(
        self, project_id: str, git_repo_path: str, hook_name: str,
        backup_path: str,
    ) -> str:
        """Generate a wrapper script that runs user's original hook first, then ours."""
        payload = self._hook_payload(project_id, git_repo_path, hook_name)
        if self._config.hook_receiver.mode == "http":
            http_cfg = self._config.hook_receiver.http
            url = f"http://{http_cfg.host}:{http_cfg.port}{http_cfg.endpoint}"
            return (
                "#!/bin/sh\n"
                f"{HOOK_MARKER} - DO NOT EDIT (wrapper)\n"
                "# Managed by Codebase RAG Event Detection Service\n"
                "# This script wraps the original user hook.\n"
                "\n"
                "# Execute original user hook\n"
                f'if [ -x "{backup_path}" ]; then\n'
                f'  "{backup_path}" "$@"\n'
                "fi\n"
                "\n"
                "# Notify Codebase RAG\n"
                f'curl -s -X POST "{url}" \\\n'
                '  -H "Content-Type: application/json" \\\n'
                f"  -d '{payload}' \\\n"
                "  --max-time 2 \\\n"
                "  || true\n"
            )
        else:
            pipe_path = self._config.hook_receiver.named_pipe.path
            return (
                "#!/bin/sh\n"
                f"{HOOK_MARKER} - DO NOT EDIT (wrapper)\n"
                "# Managed by Codebase RAG Event Detection Service\n"
                "\n"
                f'if [ -x "{backup_path}" ]; then\n'
                f'  "{backup_path}" "$@"\n'
                "fi\n"
                "\n"
                f"echo '{payload}' > \"{pipe_path}\" 2>/dev/null || true\n"
            )

    def _check_single_hook_status(self, hook_path: str) -> HookInstallStatus:
        """Check the installation status of a single hook file."""
        if not os.path.exists(hook_path):
            return HookInstallStatus.MISSING

        try:
            with open(hook_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return HookInstallStatus.CONFLICT

        if HOOK_MARKER not in content:
            return HookInstallStatus.CONFLICT

        # Check if the hook content matches what we'd generate
        # (simplified: just verify the marker is present)
        return HookInstallStatus.INSTALLED

    @staticmethod
    def _hook_payload(project_id: str, git_repo_path: str, hook_name: str) -> str:
        """Build the JSON notification payload embedded in hook scripts.

        ``args`` keeps the literal ``"$@"`` placeholder — the receiver
        treats it as opaque.  Single quotes are shell-escaped so the
        payload stays one argument inside ``-d '<payload>'`` / ``echo``.
        """
        payload = json.dumps({
            "project_id": project_id,
            "repo_path": git_repo_path,
            "hook_type": hook_name,
            "args": "$@",
        })
        return payload.replace("'", "'\\''")
