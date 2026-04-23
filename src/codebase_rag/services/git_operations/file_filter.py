"""Three-layer file filter: blacklist → whitelist → uncertain (gray area).

Layer 1: Exclude patterns (glob blacklist) — highest priority, definitive exclude
Layer 2: Include extensions/filenames (whitelist) — definitive include
Layer 3: Gray area — files not matched by L1 or L2, deferred to caller (LLM)

This module handles L1 + L2 only. L3 gray area classification is the
responsibility of the Agent service, invoked by the Workflow layer.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import List, Set, Tuple

from codebase_rag.config.schema import FileFilterConfig


@dataclass
class FilterResult:
    """Result of applying the three-layer file filter."""

    included: List[str]     # Paths that passed L1+L2 (definitive include)
    excluded: List[str]     # Paths caught by L1 blacklist (definitive exclude)
    uncertain: List[str]    # Paths not matched by L1 or L2 (gray area)


class FileFilter:
    """Compiled file filter with pre-compiled glob patterns.

    Designed for high performance: 10k files < 10ms.
    """

    def __init__(self, config: FileFilterConfig) -> None:
        self._config = config

        # Pre-compile L1 blacklist patterns to regex
        self._exclude_regexes: List[re.Pattern] = [
            re.compile(fnmatch.translate(p)) for p in config.exclude_patterns
        ]

        # Pre-compile L2 whitelist extensions (lowercase, no dot)
        self._include_extensions: Set[str] = {
            ext.lower().lstrip(".") for ext in config.include_extensions
        }

        # Pre-compile L2 whitelist filenames (exact match)
        self._include_filenames: Set[str] = set(config.include_filenames)

    def filter(self, file_paths: List[str]) -> FilterResult:
        """Apply three-layer filtering to a list of file paths.

        Args:
            file_paths: Repository-relative paths (/ separator).

        Returns:
            FilterResult with included, excluded, and uncertain lists.
        """
        included: List[str] = []
        excluded: List[str] = []
        uncertain: List[str] = []

        for path in file_paths:
            # Normalize to / separator
            normalized = path.replace("\\", "/")

            # Layer 1: Blacklist check (highest priority)
            if self._is_blacklisted(normalized):
                excluded.append(normalized)
                continue

            # Layer 2: Whitelist check
            if self._is_whitelisted(normalized):
                included.append(normalized)
                continue

            # Layer 3: Gray area
            uncertain.append(normalized)

        return FilterResult(
            included=included,
            excluded=excluded,
            uncertain=uncertain,
        )

    def _is_blacklisted(self, path: str) -> bool:
        """Check if path matches any L1 exclude pattern."""
        for regex in self._exclude_regexes:
            if regex.match(path):
                return True
        return False

    def _is_whitelisted(self, path: str) -> bool:
        """Check if path matches L2 include extension or filename."""
        # Check filename (basename)
        filename = path.rsplit("/", 1)[-1] if "/" in path else path

        # Exact filename match
        if filename in self._include_filenames:
            return True

        # Extension match
        if "." in filename:
            ext = filename.rsplit(".", 1)[-1].lower()
            if ext in self._include_extensions:
                return True

        return False

    def update_config(self, config: FileFilterConfig) -> None:
        """Re-compile filter rules from updated config (hot reload)."""
        self.__init__(config)
