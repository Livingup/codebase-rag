"""Pending Knowledge Store — Git-style hash-addressed pre-commit knowledge cache.

Manages knowledge points submitted by programming agents before git commit.
Entries include pre-generated embedding vectors so that commit-time processing
can skip both the LLM Agent and Embedding service calls.

Storage layout (Git object store style):
    {store_dir}/{fvid[:2]}/{fvid[2:]}.json

Each file_version_id maps to exactly one JSON file.  put() = write file,
consume() = read + delete file, get() = read file.  No locks needed —
different file_version_ids never touch the same file.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import List, Optional

from codebase_rag.common.logging import get_logger
from codebase_rag.common.types import SparseVector
from codebase_rag.services.qdrant_storage.models import PointData

logger = get_logger(__name__)


@dataclass
class PendingKnowledgeEntry:
    """单个文件版本的预提交知识点（含预生成的 Embedding 向量）。"""

    project_id: str
    file_path: str                          # 仓库相对路径
    blob_hash: str                          # 工作区文件的 git blob hash
    file_version_id: str                    # hash(file_path + blob_hash)
    points: List[PointData]                 # 预构建的知识点列表（含向量）
    created_at: float                       # time.time() 时间戳


class PendingKnowledgeStore:
    """Git-style hash-addressed pre-commit knowledge cache.

    Storage: {store_dir}/{fvid[:2]}/{fvid[2:]}.json — one file per entry.
    Lock-free: different file_version_ids are independent files.

    Args:
        store_dir: Root directory for hash-addressed storage.
    """

    def __init__(self, store_dir: str) -> None:
        self._store_dir = store_dir

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def ensure_dir(self) -> None:
        """Create store directory if it doesn't exist (called at startup)."""
        os.makedirs(self._store_dir, exist_ok=True)
        logger.info("pending_knowledge_store_ready", store_dir=self._store_dir)

    # =========================================================================
    # Path helpers
    # =========================================================================

    def _entry_path(self, file_version_id: str) -> str:
        """Compute file path: {store_dir}/{fvid[:2]}/{fvid[2:]}.json."""
        return os.path.join(
            self._store_dir, file_version_id[:2], file_version_id[2:] + ".json",
        )

    # =========================================================================
    # CRUD
    # =========================================================================

    async def put(
        self,
        project_id: str,
        file_version_id: str,
        entry: PendingKnowledgeEntry,
    ) -> None:
        """Write or overwrite a cache entry to disk."""
        path = self._entry_path(file_version_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        data = self._serialize_entry(entry)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, path)

        logger.debug(
            "pending_knowledge_put",
            project_id=project_id,
            file_version_id=file_version_id[:16],
            file_path=entry.file_path,
            points=len(entry.points),
        )

    def get(
        self, project_id: str, file_version_id: str,
    ) -> Optional[PendingKnowledgeEntry]:
        """Look up an entry by exact file_version_id. Returns None if not found."""
        path = self._entry_path(file_version_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._deserialize_entry(data)
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning(
                "pending_knowledge_get_failed",
                file_version_id=file_version_id[:16],
                error=str(e),
            )
            return None

    async def consume(
        self, project_id: str, file_version_id: str,
    ) -> Optional[PendingKnowledgeEntry]:
        """Read and delete an entry (consume semantics for commit processing).

        Returns the entry if found, None otherwise.
        """
        path = self._entry_path(file_version_id)
        if not os.path.exists(path):
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entry = self._deserialize_entry(data)
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning(
                "pending_knowledge_consume_failed",
                file_version_id=file_version_id[:16],
                error=str(e),
            )
            return None

        # Delete the file
        try:
            os.remove(path)
        except OSError:
            pass

        logger.debug(
            "pending_knowledge_consumed",
            project_id=project_id,
            file_version_id=file_version_id[:16],
            file_path=entry.file_path,
        )
        return entry

    # =========================================================================
    # TTL cleanup
    # =========================================================================

    async def cleanup_expired(self, now: float, ttl_minutes: int) -> int:
        """Remove entries older than ttl_minutes by walking the store directory.

        Uses file mtime for fast filtering, then checks created_at inside.
        Does nothing if ttl_minutes <= 0 (disabled).
        Returns count removed.
        """
        if ttl_minutes <= 0:
            return 0

        max_age_s = ttl_minutes * 60
        removed = 0

        if not os.path.isdir(self._store_dir):
            return 0

        for prefix_dir in os.listdir(self._store_dir):
            prefix_path = os.path.join(self._store_dir, prefix_dir)
            if not os.path.isdir(prefix_path) or len(prefix_dir) != 2:
                continue

            for fname in os.listdir(prefix_path):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(prefix_path, fname)

                # Quick mtime check to avoid reading most files
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    continue
                if (now - mtime) < max_age_s:
                    continue

                # Confirm via created_at inside the file
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    created_at = data.get("created_at", 0)
                    if (now - created_at) > max_age_s:
                        os.remove(fpath)
                        removed += 1
                except (json.JSONDecodeError, OSError):
                    pass

            # Remove empty prefix directory
            try:
                if not os.listdir(prefix_path):
                    os.rmdir(prefix_path)
            except OSError:
                pass

        if removed > 0:
            logger.info("pending_knowledge_ttl_cleanup", removed=removed)

        return removed

    # =========================================================================
    # Stats
    # =========================================================================

    @property
    def total_entries(self) -> int:
        """Count JSON files in store directory."""
        if not os.path.isdir(self._store_dir):
            return 0
        count = 0
        for prefix_dir in os.listdir(self._store_dir):
            prefix_path = os.path.join(self._store_dir, prefix_dir)
            if not os.path.isdir(prefix_path) or len(prefix_dir) != 2:
                continue
            for fname in os.listdir(prefix_path):
                if fname.endswith(".json"):
                    count += 1
        return count

    # =========================================================================
    # Serialization helpers
    # =========================================================================

    @staticmethod
    def _serialize_entry(entry: PendingKnowledgeEntry) -> dict:
        """Convert entry to JSON-serializable dict."""
        points_list = []
        for p in entry.points:
            points_list.append({
                "file_path": p.file_path,
                "entity_type": p.entity_type,
                "entity_name": p.entity_name,
                "business_description": p.business_description,
                "blob_hash": p.blob_hash,
                "dense_vector": p.dense_vector,
                "sparse_vector": {
                    "indices": p.sparse_vector.indices,
                    "values": p.sparse_vector.values,
                },
            })

        return {
            "project_id": entry.project_id,
            "file_path": entry.file_path,
            "blob_hash": entry.blob_hash,
            "file_version_id": entry.file_version_id,
            "points": points_list,
            "created_at": entry.created_at,
        }

    @staticmethod
    def _deserialize_entry(data: dict) -> PendingKnowledgeEntry:
        """Reconstruct entry from JSON dict."""
        points = []
        for p in data["points"]:
            sv = p["sparse_vector"]
            points.append(PointData(
                file_path=p["file_path"],
                entity_type=p["entity_type"],
                entity_name=p["entity_name"],
                business_description=p["business_description"],
                blob_hash=p["blob_hash"],
                dense_vector=p["dense_vector"],
                sparse_vector=SparseVector(
                    indices=sv["indices"],
                    values=sv["values"],
                ),
            ))

        return PendingKnowledgeEntry(
            project_id=data["project_id"],
            file_path=data["file_path"],
            blob_hash=data["blob_hash"],
            file_version_id=data["file_version_id"],
            points=points,
            created_at=data["created_at"],
        )
