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
import time
from pathlib import Path
import base64
import json
from typing import Any, Dict, List, Optional, Sequence

import httpx

# Allow ``python scripts/rewrite_dense_vectors.py`` from a source checkout.
_PROJECT_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src",
)
if os.path.isdir(_PROJECT_SRC) and _PROJECT_SRC not in sys.path:
    sys.path.insert(0, _PROJECT_SRC)

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointVectors,
)

from codebase_rag.common.logging import get_logger, setup_logging
from codebase_rag.common.types import EmbedInputItem, FileType, MultimodalEmbedInput
from codebase_rag.config.loader import ConfigManager, load_config
from codebase_rag.services.embedding.service import EmbeddingService
from codebase_rag.services.git_operations.file_filter import classify_file_modality
from codebase_rag.services.git_operations.service import GitOperationsService

logger = get_logger(__name__)


def _build_gemini_content_parts(item: EmbedInputItem) -> List[dict]:
    """Format an EmbedInputItem into Gemini Content parts."""
    if isinstance(item, MultimodalEmbedInput):
        parts: List[dict] = [{"text": item.text}]
        if item.image_bytes:
            b64_data = base64.b64encode(item.image_bytes).decode("ascii")
            parts.append(
                {
                    "inlineData": {
                        "mimeType": item.mime_type or "image/png",
                        "data": b64_data,
                    }
                }
            )
        return parts
    return [{"text": str(item)}]


class GeminiKeyPoolEmbedder:
    """Gemini official API embedder with round-robin key pool and rate-limit auto-recovery.

    - Round-robin across keys for every batch.
    - On 429 quota exhaustion: switch immediately to the next key.
    - If all keys in the pool hit 429: sleep 60s on 1st round, then 30s in a loop until available.
    - Minimal, clean debug logs output to stdout.
    """

    def __init__(
        self,
        keys: Sequence[str],
        api_base_url: str = "https://generativelanguage.googleapis.com",
        model_name: str = "gemini-embedding-2",
        dimension: Optional[int] = None,
        timeout: float = 30.0,
    ) -> None:
        self._keys = [k.strip() for k in keys if k and k.strip()]
        if not self._keys:
            raise ValueError("No API keys provided for GeminiKeyPoolEmbedder")
        self._base_url = api_base_url.rstrip("/")
        self._model_name = model_name
        self._dimension = dimension
        self._timeout = timeout
        self._current_index = 0
        self._all_exhausted_rounds = 0
        self._client: Optional[httpx.AsyncClient] = None

    async def initialize(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def embed_dense(
        self, inputs: Sequence[EmbedInputItem], is_query: bool = False
    ) -> List[List[float]]:
        if not inputs:
            return []
        if self._client is None:
            await self.initialize()
        assert self._client is not None

        task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        wire_requests: List[dict] = []
        for item in inputs:
            req: dict = {
                "model": f"models/{self._model_name}",
                "content": {"parts": _build_gemini_content_parts(item)},
                "taskType": task_type,
            }
            if self._dimension is not None:
                req["outputDimensionality"] = self._dimension
            wire_requests.append(req)
        payload = {"requests": wire_requests}

        num_keys = len(self._keys)
        while True:
            for _ in range(num_keys):
                key = self._keys[self._current_index]
                key_suffix = key[-6:]
                url = f"{self._base_url}/v1beta/models/{self._model_name}:batchEmbedContents?key={key}"

                try:
                    resp = await self._client.post(url, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        embeddings = data.get("embeddings", [])
                        if len(embeddings) != len(inputs):
                            raise ValueError(
                                f"Gemini returned {len(embeddings)} embeddings for {len(inputs)} inputs"
                            )
                        self._all_exhausted_rounds = 0
                        # Round-robin cursor advance for the next call
                        self._current_index = (self._current_index + 1) % num_keys
                        return [emb["values"] for emb in embeddings]

                    body_text = resp.text
                    is_rate_limit = (
                        resp.status_code == 429
                        or "429" in body_text
                        or "RESOURCE_EXHAUSTED" in body_text
                        or "quota" in body_text.lower()
                        or "too many requests" in body_text.lower()
                    )

                    if is_rate_limit:
                        print(
                            f"[debug][key-rotate] key ...{key_suffix} hit 429 (quota), switching to next key...",
                            flush=True,
                        )
                        self._current_index = (self._current_index + 1) % num_keys
                        continue
                    else:
                        print(
                            f"[debug][error] key ...{key_suffix} HTTP {resp.status_code}: {body_text[:100]}, switching key...",
                            flush=True,
                        )
                        self._current_index = (self._current_index + 1) % num_keys
                        continue

                except (httpx.TimeoutException, httpx.NetworkError) as net_err:
                    print(
                        f"[debug][network-retry] key ...{key_suffix} timeout/network error ({type(net_err).__name__}), switching key...",
                        flush=True,
                    )
                    self._current_index = (self._current_index + 1) % num_keys
                    continue
                except Exception as exc:
                    exc_str = str(exc).lower()
                    if any(m in exc_str for m in ("429", "resource_exhausted", "quota")):
                        print(
                            f"[debug][key-rotate] key ...{key_suffix} hit 429, switching key...",
                            flush=True,
                        )
                        self._current_index = (self._current_index + 1) % num_keys
                        continue
                    print(
                        f"[debug][exception] key ...{key_suffix}: {str(exc)[:100]}, switching key...",
                        flush=True,
                    )
                    self._current_index = (self._current_index + 1) % num_keys
                    continue

            # Full rotation over all keys resulted in rate limits / busy
            self._all_exhausted_rounds += 1
            wait_seconds = 60 if self._all_exhausted_rounds == 1 else 30
            print(
                f"[debug][all-exhausted] all {num_keys} keys busy/429, round={self._all_exhausted_rounds}, "
                f"sleeping {wait_seconds}s before retry...",
                flush=True,
            )
            await asyncio.sleep(wait_seconds)


def _infer_file_type_from_path(file_path: str) -> FileType:
    ext = os.path.splitext(file_path or "")[1].lower()
    if ext in {".png", ".jpg", ".jpeg"}:
        return "image"
    return "text"


async def _try_load_repo_image(
    git_ops: GitOperationsService,
    repo_roots: Dict[str, List[str]],
    project_id: str,
    rel_path: str,
    blob_hash: str,
) -> Optional[tuple[bytes, str]]:
    """Load the exact image blob from any registered local repository copy."""
    if not rel_path or not blob_hash:
        return None
    for repo_root in repo_roots.get(project_id, []):
        try:
            raw_bytes = await git_ops.get_file_bytes(
                repo_root, blob_hash, file_path=rel_path,
            )
            modality, mime_type = classify_file_modality(rel_path, raw_bytes)
            if modality == "image" and mime_type:
                return raw_bytes, mime_type
            logger.warning(
                "rewrite_dense_vectors_blob_not_supported_image",
                repo_path=repo_root,
                file_path=rel_path,
                blob_hash=blob_hash,
            )
        except Exception as e:
            logger.warning(
                "rewrite_dense_vectors_image_blob_read_failed",
                repo_path=repo_root,
                file_path=rel_path,
                blob_hash=blob_hash,
                error=str(e)[:200],
            )
    return None


def _load_registered_repo_roots(registry_file: str) -> Dict[str, List[str]]:
    """Load project_id -> local repository paths from project_registry.json."""
    import json

    if not os.path.isfile(registry_file):
        return {}
    try:
        with open(registry_file, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Project registry root must be a JSON array")
        mapping: Dict[str, List[str]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            pid = item.get("project_id")
            rpath = item.get("git_repo_path")
            if pid and rpath and os.path.isdir(rpath):
                normalized = os.path.normpath(rpath)
                bucket = mapping.setdefault(pid, [])
                if normalized not in bucket:
                    bucket.append(normalized)
        return mapping
    except (OSError, ValueError, TypeError) as e:
        logger.warning(
            "rewrite_dense_vectors_registry_read_failed",
            registry_file=registry_file,
            error=str(e)[:200],
        )
        return {}


def _create_qdrant_client(config_manager: ConfigManager) -> AsyncQdrantClient:
    """Create a client with exactly the fields declared by QdrantConnectionConfig."""
    conn = config_manager.config.qdrant_storage.connection
    return AsyncQdrantClient(
        host=conn.host,
        port=conn.port,
        grpc_port=conn.grpc_port,
        prefer_grpc=conn.prefer_grpc,
        https=conn.https,
        api_key=conn.api_key,
        timeout=conn.timeout,
    )


async def _rewrite_batch(
    client: AsyncQdrantClient,
    records: Sequence[Any],
    *,
    collection_name: str,
    dense_vector_name: str,
    embedding: Optional[EmbeddingService],
    git_ops: Optional[GitOperationsService],
    repo_roots: Dict[str, List[str]],
    dry_run: bool,
) -> int:
    """Prepare, embed and update one scrolled batch without private service access."""
    embed_inputs: List[EmbedInputItem] = []
    valid_records: List[Any] = []
    resolved_types: List[FileType] = []

    for rec in records:
        payload = rec.payload or {}
        # Points that already carry file_type were migrated by a previous run, so
        # restarting the script resumes instead of re-embedding everything.
        if payload.get("file_type"):
            continue
        desc = (payload.get("business_description") or "").strip()
        if not desc:
            continue
        rel_path = payload.get("file_path") or ""
        pid = payload.get("project_id") or ""
        blob_hash = payload.get("blob_hash") or ""
        raw_type = payload.get("file_type")
        legacy_image = not raw_type and _infer_file_type_from_path(rel_path) == "image"
        ftype: FileType = "image" if raw_type == "image" or legacy_image else "text"

        if ftype == "image":
            loaded = (
                await _try_load_repo_image(
                    git_ops, repo_roots, pid, rel_path, blob_hash,
                )
                if git_ops is not None
                else None
            )
            if loaded is not None:
                img_bytes, mime_type = loaded
                embed_inputs.append(
                    MultimodalEmbedInput(
                        text=desc, image_bytes=img_bytes, mime_type=mime_type,
                    )
                )
            else:
                logger.warning(
                    "rewrite_dense_vectors_image_fallback_text",
                    project_id=pid,
                    file_path=rel_path,
                    blob_hash=blob_hash,
                )
                embed_inputs.append(desc)
        else:
            embed_inputs.append(desc)

        valid_records.append(rec)
        resolved_types.append(ftype)

    if not valid_records or dry_run:
        return len(valid_records)
    assert embedding is not None
    if hasattr(embedding, "embed_dense"):
        vectors = await embedding.embed_dense(embed_inputs, is_query=False)
    else:
        results = await embedding.embed(embed_inputs, is_query=False)
        vectors = [result.dense_vector for result in results]
    point_vectors = [
        PointVectors(id=rec.id, vector={dense_vector_name: vec})
        for rec, vec in zip(valid_records, vectors, strict=True)
    ]
    await client.update_vectors(
        collection_name=collection_name, points=point_vectors, wait=True,
    )

    for file_type in ("text", "image"):
        point_ids = [
            rec.id
            for rec, old_type, new_type in zip(
                valid_records,
                [(rec.payload or {}).get("file_type") for rec in valid_records],
                resolved_types,
                strict=True,
            )
            if new_type == file_type and old_type != new_type
        ]
        if point_ids:
            await client.set_payload(
                collection_name=collection_name,
                payload={"file_type": file_type},
                points=point_ids,
                wait=True,
            )
    return len(valid_records)


async def _migrate_batch(
    *,
    embedding: Optional[EmbeddingService],
    qdrant: AsyncQdrantClient,
    repo_roots: Dict[str, List[str]],
    records: Sequence[Any],
    dry_run: bool,
    dense_name: Optional[str] = None,
    collection_name: str = "codebase",
    dense_vector_name: Optional[str] = None,
    git_ops: Optional[GitOperationsService] = None,
) -> int:
    """Migrate one batch through explicit dependencies (also used by unit tests)."""
    resolved_dense_name = dense_vector_name or dense_name
    if not resolved_dense_name:
        raise ValueError("dense_vector_name is required")
    return await _rewrite_batch(
        qdrant,
        records,
        collection_name=collection_name,
        dense_vector_name=resolved_dense_name,
        embedding=embedding,
        git_ops=git_ops,
        repo_roots=repo_roots,
        dry_run=dry_run,
    )


_RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "rate_limit", "quota", "too many requests")


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True when the failure is a provider rate-limit/quota rejection."""
    message = str(exc).lower()
    return any(marker in message for marker in _RATE_LIMIT_MARKERS)


async def _rewrite_batch_with_retry(
    client: AsyncQdrantClient,
    records: Sequence[Any],
    *,
    collection_name: str,
    dense_vector_name: str,
    embedding: Optional[EmbeddingService],
    git_ops: Optional[GitOperationsService],
    repo_roots: Dict[str, List[str]],
    dry_run: bool,
) -> int:
    """Run one batch; keep retrying while every key is rate limited.

    First wait is 60s (provider minute window), afterwards 30s, looping until the
    batch succeeds so a long migration never aborts on quota alone.
    """
    rate_limited_rounds = 0
    while True:
        try:
            return await _rewrite_batch(
                client,
                records,
                collection_name=collection_name,
                dense_vector_name=dense_vector_name,
                embedding=embedding,
                git_ops=git_ops,
                repo_roots=repo_roots,
                dry_run=dry_run,
            )
        except Exception as exc:
            if not _is_rate_limit_error(exc):
                raise
            rate_limited_rounds += 1
            wait_seconds = 60 if rate_limited_rounds == 1 else 30
            print(
                f"[rate-limit] all keys busy, round={rate_limited_rounds}, "
                f"sleep={wait_seconds}s, batch_points={len(records)}",
                flush=True,
            )
            await asyncio.sleep(wait_seconds)


async def rewrite_all_points(
    config_path: str,
    project_id: Optional[str] = None,
    batch_size: int = 64,
    dry_run: bool = False,
    max_points: Optional[int] = None,
) -> int:
    if batch_size < 1:
        raise ValueError("batch_size must be greater than zero")
    resolved_config_path = Path(config_path)
    config_manager = ConfigManager(load_config(resolved_config_path), resolved_config_path)
    config = config_manager.config
    log_config = config.system.log
    setup_logging(
        level=log_config.level,
        format=log_config.format,
        file=log_config.file,
        error_file=log_config.error_file,
        retention_days=log_config.retention_days,
        max_bytes=log_config.max_bytes,
        backup_count=log_config.backup_count,
    )

    collection_config = config.qdrant_storage.collection
    collection_name = collection_config.name
    dense_vector_name = collection_config.dense_vector.name
    repo_roots = _load_registered_repo_roots(config.management_api.registry_path)
    git_ops = GitOperationsService(config_manager)
    client = _create_qdrant_client(config_manager)
    embedding: Optional[EmbeddingService] = None

    active_provider = config.embedding.dense.active_provider
    provider_config = config.embedding.dense.providers.get(active_provider)
    provider_batch_size = provider_config.batch_size if provider_config else 100
    scroll_batch_size = min(batch_size, provider_batch_size)
    scroll_filter = None
    if project_id:
        scroll_filter = Filter(
            must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
        )

    total_rewritten = 0
    offset = None
    started_at = time.monotonic()
    try:
        if not dry_run:
            active_provider = config.embedding.dense.active_provider
            provider_config = config.embedding.dense.providers.get(active_provider)
            raw_keys = [k.key for k in (provider_config.api_keys if provider_config else []) if k.key]
            if raw_keys and active_provider == "gemini":
                embedding = GeminiKeyPoolEmbedder(
                    keys=raw_keys,
                    api_base_url=provider_config.api_base_url if provider_config else "https://generativelanguage.googleapis.com",
                    model_name=provider_config.model_name if provider_config else "gemini-embedding-2",
                    dimension=provider_config.dimension if provider_config else 3072,
                    timeout=provider_config.request_timeout if provider_config else 30.0,
                )
                await embedding.initialize()
            else:
                embedding = EmbeddingService(config_manager)
                await embedding.initialize()
        while True:
            limit = scroll_batch_size
            if max_points is not None:
                remaining = max_points - total_rewritten
                if remaining <= 0:
                    break
                limit = min(limit, remaining)
            records, next_offset = await client.scroll(
                collection_name=collection_name,
                scroll_filter=scroll_filter,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if not records:
                break
            total_rewritten += await _rewrite_batch_with_retry(
                client,
                records,
                collection_name=collection_name,
                dense_vector_name=dense_vector_name,
                embedding=embedding,
                git_ops=git_ops,
                repo_roots=repo_roots,
                dry_run=dry_run,
            )
            print(
                f"[progress] migrated={total_rewritten} batch_scanned={len(records)} "
                f"elapsed={time.monotonic() - started_at:.0f}s",
                flush=True,
            )
            logger.info(
                "rewrite_dense_vectors_batch_done",
                total_rewritten=total_rewritten,
                dry_run=dry_run,
            )
            if max_points is not None and total_rewritten >= max_points:
                logger.info(
                    "rewrite_dense_vectors_max_points_reached",
                    total_rewritten=total_rewritten,
                )
                break
            if next_offset is None:
                break
            offset = next_offset
    finally:
        await client.close()
        if embedding is not None:
            if hasattr(embedding, "close"):
                await embedding.close()
            elif hasattr(embedding, "shutdown"):
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
    parser.add_argument(
        "--max-points",
        type=int,
        default=None,
        help="Stop after processing this many points (staged migration)",
    )
    args = parser.parse_args()

    total = asyncio.run(
        rewrite_all_points(
            config_path=args.config,
            project_id=args.project_id,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            max_points=args.max_points,
        )
    )
    print(f"Completed: {total} point(s) processed (dry_run={args.dry_run}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
