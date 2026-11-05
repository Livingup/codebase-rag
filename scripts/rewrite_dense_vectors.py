"""Rewrite all persistent Qdrant points with gemini-embedding-2 dense vectors.

Because gemini-embedding-001 and gemini-embedding-2 do not share a compatible
vector space, upgrading to gemini-embedding-2 requires re-embedding all stored
points in Qdrant.

Usage:
    python scripts/rewrite_dense_vectors.py --config config.yaml [--project-id <id>] [--batch-size 64] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointVectors,
)

from codebase_rag.common.logging import get_logger, setup_logging
from codebase_rag.common.types import EmbedInputItem, FileType, MultimodalEmbedInput
from codebase_rag.config.loader import ConfigManager
from codebase_rag.services.embedding.service import EmbeddingService
from codebase_rag.services.git_operations.file_filter import classify_file_modality
from codebase_rag.services.qdrant_storage.service import QdrantStorageService

logger = get_logger(__name__)


def _infer_file_type_from_path(file_path: str) -> FileType:
    ext = os.path.splitext(file_path or "")[1].lower()
    if ext in {".png", ".jpg", ".jpeg"}:
        return "image"
    return "text"


def _try_load_repo_image(repo_roots: Dict[str, str], project_id: str, rel_path: str) -> Optional[tuple[bytes, str]]:
    """Attempt to load image bytes from a registered local repository if available."""
    repo_root = repo_roots.get(project_id)
    if not repo_root or not rel_path:
        return None
    abs_path = os.path.normpath(os.path.join(repo_root, rel_path))
    if not os.path.isfile(abs_path):
        return None
    try:
        with open(abs_path, "rb") as f:
            raw_bytes = f.read()
        modality, mime_type = classify_file_modality(rel_path, raw_bytes)
        if modality == "image" and mime_type:
            return raw_bytes, mime_type
    except Exception:
        return None
    return None


def _load_registered_repo_roots(data_dir: str) -> Dict[str, str]:
    """Load project_id -> git_repo_path mapping from projects.json if present."""
    import json

    registry_file = os.path.join(data_dir, "projects.json")
    if not os.path.isfile(registry_file):
        return {}
    try:
        with open(registry_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        mapping: Dict[str, str] = {}
        for item in data.get("projects", []):
            pid = item.get("project_id")
            rpath = item.get("git_repo_path")
            if pid and rpath and pid not in mapping and os.path.isdir(rpath):
                mapping[pid] = rpath
        return mapping
    except Exception:
        return {}


async def rewrite_all_points(
    config_path: str,
    project_id: Optional[str] = None,
    batch_size: int = 64,
    dry_run: bool = False,
) -> int:
    config_manager = ConfigManager.load(config_path)
    setup_logging(config_manager.config.logging)

    embedding = EmbeddingService(config_manager)
    qdrant = QdrantStorageService(config_manager)

    await embedding.initialize()
    await qdrant.initialize()

    repo_roots = _load_registered_repo_roots(config_manager.config.Management.data_dir)
    collection_name = config_manager.config.qdrant.collection_name

    scroll_filter = None
    if project_id:
        scroll_filter = Filter(
            must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
        )

    total_rewritten = 0
    offset = None

    try:
        while True:
            assert qdrant._client is not None
            records, next_offset = await qdrant._client.scroll(
                collection_name=collection_name,
                scroll_filter=scroll_filter,
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if not records:
                break

            embed_inputs: List[EmbedInputItem] = []
            valid_records: List[Any] = []
            resolved_types: List[FileType] = []

            for rec in records:
                payload = rec.payload or {}
                desc = (payload.get("business_description") or "").strip()
                if not desc:
                    continue
                rel_path = payload.get("file_path") or ""
                pid = payload.get("project_id") or ""
                ftype: FileType = payload.get("file_type") or _infer_file_type_from_path(rel_path)

                if ftype == "image":
                    loaded = _try_load_repo_image(repo_roots, pid, rel_path)
                    if loaded is not None:
                        img_bytes, mime_type = loaded
                        embed_inputs.append(
                            MultimodalEmbedInput(
                                text=desc,
                                image_bytes=img_bytes,
                                mime_type=mime_type,
                            )
                        )
                    else:
                        embed_inputs.append(desc)
                else:
                    embed_inputs.append(desc)

                valid_records.append(rec)
                resolved_types.append(ftype)

            if valid_records and not dry_run:
                vectors = await embedding.embed_dense(embed_inputs, is_query=False)
                point_vectors = [
                    PointVectors(
                        id=rec.id,
                        vector={"dense_vectors": vec},
                    )
                    for rec, vec in zip(valid_records, vectors)
                ]
                await qdrant._client.update_vectors(
                    collection_name=collection_name,
                    points=point_vectors,
                    wait=True,
                )
                for rec, ftype in zip(valid_records, resolved_types):
                    payload = rec.payload or {}
                    if payload.get("file_type") != ftype:
                        await qdrant._client.set_payload(
                            collection_name=collection_name,
                            payload={"file_type": ftype},
                            points=[rec.id],
                            wait=True,
                        )

            total_rewritten += len(valid_records)
            logger.info(
                "rewrite_dense_vectors_batch_done",
                batch_points=len(valid_records),
                total_rewritten=total_rewritten,
                dry_run=dry_run,
            )

            if next_offset is None:
                break
            offset = next_offset
    finally:
        await qdrant.shutdown()
        await embedding.shutdown()

    return total_rewritten


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-embed all persistent Qdrant points using gemini-embedding-2."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--project-id",
        default=None,
        help="Optional project_id filter to only rewrite a specific project",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Number of points to process per scroll batch (default: 64)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and count points without calling Gemini API or updating Qdrant",
    )
    args = parser.parse_args()

    total = asyncio.run(
        rewrite_all_points(
            config_path=args.config,
            project_id=args.project_id,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    )
    print(f"Completed: {total} point(s) processed (dry_run={args.dry_run}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
