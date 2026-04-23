"""Configuration loader — YAML parsing + environment variable resolution.

Automatically loads .env file (if present) so API keys don't need to be
set manually every time.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

from codebase_rag.common.logging import get_logger
from codebase_rag.config.schema import AppConfig

logger = get_logger(__name__)


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env_vars(value: Any) -> Any:
    """Recursively resolve ${ENV_VAR} references in config values."""
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise ValueError(
                    f"Environment variable '{var_name}' is not set "
                    f"(referenced in config as ${{{var_name}}})"
                )
            return env_value
        return _ENV_VAR_PATTERN.sub(_replace, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
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
    if env_file.exists():
        load_dotenv(env_file, override=False)

    with open(config_path, "r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    resolved = _resolve_env_vars(raw)
    return AppConfig.model_validate(resolved)


class ConfigManager:
    """Configuration manager — Single Source of Truth for runtime config.

    Holds the current AppConfig and supports reload from disk.
    Services hold a reference to this instance and access their config
    section via ``@property`` that reads ``self._config_manager.config.<section>``.
    """

    def __init__(self, config: AppConfig, config_path: Path) -> None:
        self._config = config
        self._config_path = config_path

    @property
    def config(self) -> AppConfig:
        """Current active configuration (read-only reference)."""
        return self._config

    @property
    def config_path(self) -> Path:
        return self._config_path

    async def reload(self) -> AppConfig:
        """Reload config from disk and replace the in-memory AppConfig.

        All services that hold a reference to this ConfigManager will
        automatically see the new config on their next ``_config`` access.

        Returns:
            The new AppConfig instance.

        Raises:
            FileNotFoundError: Config file missing.
            ValueError: Env var unresolved.
            ValidationError: Schema validation failed.
        """
        new_config = load_config(self._config_path)
        self._config = new_config

        logger.info(
            "config_reloaded",
            config_path=str(self._config_path),
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
