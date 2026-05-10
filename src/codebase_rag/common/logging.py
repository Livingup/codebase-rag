"""Unified logging configuration using structlog."""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

import structlog


class RetentionTimedRotatingFileHandler(TimedRotatingFileHandler):
    def __init__(
        self,
        filename: str,
        retention_seconds: int,
        **kwargs,
    ) -> None:
        self.retention_seconds = retention_seconds
        super().__init__(filename, **kwargs)

    def doRollover(self) -> None:
        super().doRollover()
        self.delete_expired_logs()

    def delete_expired_logs(self) -> None:
        if self.retention_seconds <= 0:
            return
        cutoff = time.time() - self.retention_seconds
        base = Path(self.baseFilename)
        for path in base.parent.glob(f"{base.name}.*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue


def _ensure_parent_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def setup_logging(
    level: str = "INFO",
    format: str = "json",
    file: Optional[str] = None,
    error_file: Optional[str] = None,
    retention_days: int = 1,
    max_bytes: int = 50 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure structured logging for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        format: Output format ("json" or "text").
        file: Optional log file path for all levels. None means stdout only.
        error_file: Optional log file path for WARNING and above only.
        retention_days: Days to keep regular rotated log files.
        max_bytes: Max size per error log file before rotation (default 50MB).
        backup_count: Number of rotated error log files to keep (default 5).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
    ]

    if format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handlers: list[logging.Handler] = []

    if not file:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    if file:
        _ensure_parent_dir(file)
        file_handler = RetentionTimedRotatingFileHandler(
            file,
            retention_seconds=max(0, retention_days) * 24 * 60 * 60,
            when="H",
            interval=1,
            backupCount=0,
            encoding="utf-8",
        )
        file_handler.delete_expired_logs()
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    if error_file:
        _ensure_parent_dir(error_file)
        error_handler = RotatingFileHandler(
            error_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
        )
        error_handler.setLevel(logging.WARNING)
        error_handler.setFormatter(formatter)
        handlers.append(error_handler)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    for handler in handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)
