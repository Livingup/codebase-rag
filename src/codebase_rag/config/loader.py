"""Configuration loader — YAML parsing + environment variable resolution.

Automatically loads .env file (if present) so API keys don't need to be
set manually every time.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import asyncio

import yaml
from dotenv import load_dotenv

from codebase_rag.common.logging import get_logger
from codebase_rag.config.schema import AppConfig

logger = get_logger(__name__)


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a .env file directly into a dictionary without relying on os.environ."""
    result: Dict[str, str] = {}
    if not path.exists():
        return result
    try:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            idx = line.find("=")
            if idx <= 0:
                continue
            key = line[:idx].strip()
            val = line[idx + 1:].strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            result[key] = val
    except Exception as e:
        logger.warning("env_file_parse_warning", path=str(path), error=str(e))
    return result


def _resolve_env_vars(value: Any, env_overlay: Optional[Dict[str, str]] = None) -> Any:
    """Recursively resolve ${ENV_VAR} references using env_overlay first, then os.environ."""
    overlay = env_overlay or {}
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            env_value = overlay.get(var_name)
            if env_value is None:
                env_value = os.environ.get(var_name)
            if env_value is None:
                raise ValueError(
                    f"Environment variable '{var_name}' is not set "
                    f"(referenced in config as ${{{var_name}}})"
                )
            return env_value
        return _ENV_VAR_PATTERN.sub(_replace, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v, overlay) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item, overlay) for item in value]
    return value


def load_config(path: str | Path) -> AppConfig:
    """Load and validate configuration from a YAML file.

    Args:
        path: Path to the YAML config file.

    Returns:
        Validated AppConfig instance.

    Raises:
        FileNotFoundError: If config file does not exist.
        ValueError: If environment variables are missing.
        ValidationError: If config fails schema validation.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Auto-load .env file (same directory as config, or project root)
    env_file = config_path.parent / ".env"
    env_overlay: Dict[str, str] = {}
    if env_file.exists():
        env_overlay = parse_env_file(env_file)
        load_dotenv(env_file, override=True)

    with open(config_path, "r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    resolved = _resolve_env_vars(raw, env_overlay)
    return AppConfig.model_validate(resolved)


ReconcilerCallback = Callable[[AppConfig, AppConfig], Awaitable[None]]


class ConfigManager:
    """Configuration manager — Single Source of Truth for runtime config.

    Holds the current AppConfig and supports real-time reconciliation reload from disk.
    Services register their reconcile callbacks to respond dynamically to configuration changes.
    """

    def __init__(self, config: AppConfig, config_path: Path) -> None:
        self._config = config
        self._config_path = config_path
        self._reconcilers: Dict[str, ReconcilerCallback] = {}
        self._lock = asyncio.Lock()
        self.last_reconciled_services: List[str] = []
        self.last_restart_reasons: List[str] = []

    @property
    def config(self) -> AppConfig:
        """Current active configuration (read-only reference)."""
        return self._config

    @property
    def config_path(self) -> Path:
        return self._config_path

    def register_reconciler(self, name: str, callback: ReconcilerCallback) -> None:
        """Register a downstream service reconciler callback."""
        self._reconcilers[name] = callback
        logger.info("config_reconciler_registered", service=name)

    def unregister_reconciler(self, name: str) -> None:
        """Unregister a service reconciler callback."""
        self._reconcilers.pop(name, None)

    @staticmethod
    def detect_immutable_changes(old_cfg: AppConfig, new_cfg: AppConfig) -> List[str]:
        """Detect changes in Level 0 immutable process configuration that require restart."""
        reasons: List[str] = []
        if old_cfg.management_api.server.port != new_cfg.management_api.server.port:
            reasons.append(
                f"Management API port changed ({old_cfg.management_api.server.port} -> {new_cfg.management_api.server.port})"
            )
        if old_cfg.management_api.server.host != new_cfg.management_api.server.host:
            reasons.append(
                f"Management API host changed ({old_cfg.management_api.server.host} -> {new_cfg.management_api.server.host})"
            )
        old_mcp_ports = {k: (v.host, v.port) for k, v in old_cfg.mcp.servers.items()}
        new_mcp_ports = {k: (v.host, v.port) for k, v in new_cfg.mcp.servers.items()}
        if old_mcp_ports != new_mcp_ports:
            reasons.append(f"MCP server endpoint changed ({old_mcp_ports} -> {new_mcp_ports})")

        if old_cfg.system.data_dir != new_cfg.system.data_dir:
            reasons.append(
                f"System data directory changed ({old_cfg.system.data_dir} -> {new_cfg.system.data_dir})"
            )
        if old_cfg.system.log.level != new_cfg.system.log.level:
            reasons.append(
                f"Logging root level changed ({old_cfg.system.log.level} -> {new_cfg.system.log.level})"
            )
        return reasons

    async def reload(self) -> AppConfig:
        """Reload config from disk, reconcile all registered services, and swap in-memory AppConfig.

        Returns:
            The new AppConfig instance.

        Raises:
            FileNotFoundError: Config file missing.
            ValueError: Env var unresolved.
            ValidationError: Schema validation failed.
        """
        async with self._lock:
            old_config = self._config
            new_config = load_config(self._config_path)

            restart_reasons = self.detect_immutable_changes(old_config, new_config)
            self.last_restart_reasons = restart_reasons
            if restart_reasons:
                logger.warning(
                    "config_reload_immutable_fields_changed",
                    reasons=restart_reasons,
                )

            reconciled_services: List[str] = []
            for name, reconciler in list(self._reconcilers.items()):
                try:
                    await reconciler(old_config, new_config)
                    reconciled_services.append(name)
                    logger.info("service_reconciled_successfully", service=name)
                except Exception as e:
                    logger.exception(
                        "service_reconcile_failed",
                        service=name,
                        error=str(e),
                    )
                    raise

            self._config = new_config
            self.last_reconciled_services = reconciled_services

            logger.info(
                "config_reloaded",
                config_path=str(self._config_path),
                reconciled_services=reconciled_services,
                requires_restart=bool(restart_reasons),
            )

            return new_config


def resolve_config_path() -> Path:
    """Determine config file path by priority:

    1. CODEBASE_RAG_CONFIG environment variable
    2. ./config.yaml (current directory)
    """
    env_path = os.environ.get("CODEBASE_RAG_CONFIG")
    if env_path:
        return Path(env_path)
    return Path("config.yaml")
