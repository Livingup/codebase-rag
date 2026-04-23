"""Orphan record persistence — simple JSON file store.

Structure: { file_version_id: { "project_id": str, "first_marked_at": ISO datetime } }
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, Optional

from codebase_rag.common.logging import get_logger
from codebase_rag.services.gc.models import OrphanStoreError

logger = get_logger(__name__)


class OrphanRecord:
    """In-memory representation of a single orphan entry."""

    __slots__ = ("project_id", "first_marked_at")

    def __init__(self, project_id: str, first_marked_at: datetime) -> None:
        self.project_id = project_id
        self.first_marked_at = first_marked_at

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "first_marked_at": self.first_marked_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> OrphanRecord:
        return cls(
            project_id=data["project_id"],
            first_marked_at=datetime.fromisoformat(data["first_marked_at"]),
        )


class OrphanStore:
    """JSON-file backed orphan record store.

    Thread-safe via external locking (caller is responsible).
    """

    def __init__(self, path: str) -> None:
        self._path = path
        # file_version_id → OrphanRecord
        self._records: Dict[str, OrphanRecord] = {}

    @property
    def records(self) -> Dict[str, OrphanRecord]:
        return self._records

    def load(self) -> None:
        """Load orphan records from disk. No-op if file doesn't exist."""
        if not os.path.exists(self._path):
            self._records = {}
            logger.info("orphan_store_no_file", path=self._path)
            return

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw: dict = json.load(f)
            self._records = {
                fvid: OrphanRecord.from_dict(data) for fvid, data in raw.items()
            }
            logger.info(
                "orphan_store_loaded",
                path=self._path,
                count=len(self._records),
            )
        except Exception as e:
            raise OrphanStoreError(f"Failed to load orphan store: {e}") from e

    def save(self) -> None:
        """Persist current orphan records to disk."""
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            raw = {fvid: rec.to_dict() for fvid, rec in self._records.items()}
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False)
            # Atomic rename (best-effort on Windows)
            if os.path.exists(self._path):
                os.replace(tmp_path, self._path)
            else:
                os.rename(tmp_path, self._path)
            logger.debug(
                "orphan_store_saved",
                path=self._path,
                count=len(self._records),
            )
        except Exception as e:
            raise OrphanStoreError(f"Failed to save orphan store: {e}") from e

    def mark(self, file_version_id: str, project_id: str) -> bool:
        """Mark a file_version_id as orphan. Returns True if newly marked."""
        if file_version_id in self._records:
            return False
        self._records[file_version_id] = OrphanRecord(
            project_id=project_id,
            first_marked_at=datetime.utcnow(),
        )
        return True

    def unmark(self, file_version_id: str) -> bool:
        """Remove orphan mark (file is referenced again). Returns True if was marked."""
        return self._records.pop(file_version_id, None) is not None

    def remove(self, file_version_id: str) -> None:
        """Remove record after successful deletion."""
        self._records.pop(file_version_id, None)

    def get(self, file_version_id: str) -> Optional[OrphanRecord]:
        return self._records.get(file_version_id)

    def get_by_project(self, project_id: str) -> Dict[str, OrphanRecord]:
        """Get all orphan records for a specific project."""
        return {
            fvid: rec
            for fvid, rec in self._records.items()
            if rec.project_id == project_id
        }

    def remove_project(self, project_id: str) -> int:
        """Remove all orphan records for a project. Returns count removed."""
        to_remove = [
            fvid for fvid, rec in self._records.items()
            if rec.project_id == project_id
        ]
        for fvid in to_remove:
            del self._records[fvid]
        return len(to_remove)
