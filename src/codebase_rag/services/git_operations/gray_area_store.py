"""Persistent classification cache for gray-area file types.

When the three-layer file filter encounters a file that doesn't match
L1 (blacklist) or L2 (whitelist), it falls into the gray area (L3).

This store caches LLM classification decisions so the same file type
never needs to be classified twice.  Keys are either ``ext:<extension>``
(e.g. ``ext:pyx``) or ``name:<filename>`` (e.g. ``name:Makefile``).

Values are ``"include"`` or ``"exclude"``.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional, Set, Tuple

from codebase_rag.common.logging import get_logger

logger = get_logger(__name__)


class GrayAreaStore:
    """Thread-safe, JSON-backed gray-area classification cache."""

    def __init__(self, store_path: str) -> None:
        self._path = store_path
        self._data: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Key extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_type_key(file_path: str) -> str:
        """Derive a classification key from a repository-relative path.

        Rules:
            - ``src/main.py``       → ``ext:py``
            - ``logo.svg``          → ``ext:svg``
            - ``.dockerignore``     → ``name:.dockerignore``
            - ``Makefile``          → ``name:Makefile``
            - ``README``            → ``name:README``
        """
        filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
        if "." in filename:
            ext = filename.rsplit(".", 1)[-1].lower()
            if ext:
                return f"ext:{ext}"
        return f"name:{filename}"

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def lookup(self, type_key: str) -> Optional[str]:
        """Return ``"include"`` / ``"exclude"`` or ``None`` if unknown."""
        return self._data.get(type_key)

    def resolve(
        self, uncertain_paths: List[str],
    ) -> Tuple[List[str], List[str], List[str]]:
        """Classify *uncertain_paths* using cached decisions.

        Returns:
            ``(included, excluded, unknown)`` — *unknown* paths still need
            LLM classification.
        """
        included: List[str] = []
        excluded: List[str] = []
        unknown: List[str] = []

        for path in uncertain_paths:
            key = self.extract_type_key(path)
            decision = self._data.get(key)
            if decision == "include":
                included.append(path)
            elif decision == "exclude":
                excluded.append(path)
            else:
                unknown.append(path)

        return included, excluded, unknown

    def get_unknown_keys(self, paths: List[str]) -> Set[str]:
        """Return the set of type-keys not yet present in the store."""
        keys: Set[str] = set()
        for path in paths:
            key = self.extract_type_key(path)
            if key not in self._data:
                keys.add(key)
        return keys

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def update(self, classifications: Dict[str, str]) -> None:
        """Merge *classifications* into the store and persist to disk.

        Args:
            classifications: Mapping of type-key → ``"include"``/``"exclude"``.
        """
        with self._lock:
            self._data.update(classifications)
            self._save()
            logger.info(
                "gray_area_store_updated",
                new_entries=len(classifications),
                total_entries=len(self._data),
            )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            logger.info(
                "gray_area_store_loaded",
                path=self._path,
                entries=len(self._data),
            )
        except Exception:
            logger.warning("gray_area_store_load_failed", path=self._path)
            self._data = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def entries(self) -> Dict[str, str]:
        """Read-only snapshot of current classifications."""
        return dict(self._data)

    def __len__(self) -> int:
        return len(self._data)
