"""Qdrant Storage Service — persistent KV storage for file_version_id → Points.

Pure storage layer wrapping qdrant-client AsyncQdrantClient.
No semantic search — only project-scoped persistence by file_version_id.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
import time
from typing import Dict, List, Optional, Set

from qdrant_client import AsyncQdrantClient, models

from codebase_rag.common.logging import get_logger
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import QdrantStorageConfig
from codebase_rag.services.qdrant_storage.errors import (
    QdrantConnectionError,
    QdrantOperationError,
    QdrantSchemaError,
)
from codebase_rag.services.qdrant_storage.models import PointData

logger = get_logger(__name__)


class QdrantStorageService:
    """Qdrant Server persistent storage for file knowledge points.

    Manages connection lifecycle, collection initialization, and
    file-version persistence scoped by project_id + file_version_id.

    Args:
        config: Qdrant storage configuration section from AppConfig.
    """

    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        self._client: Optional[AsyncQdrantClient] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._initialized = False

    @property
    def _config(self) -> QdrantStorageConfig:
        return self._config_manager.config.qdrant_storage

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Connect to Qdrant Server and ensure collection exists."""
        logger.info("qdrant_storage_service_initializing")

        conn = self._config.connection
        try:
            self._client = AsyncQdrantClient(
                host=conn.host,
                port=conn.port,
                grpc_port=conn.grpc_port,
                api_key=conn.api_key,
                prefer_grpc=conn.prefer_grpc,
                https=conn.https,
                timeout=conn.timeout,
                grpc_options={
                    "grpc.max_receive_message_length": -1,
                },
            )
        except Exception as e:
            raise QdrantConnectionError(f"Failed to create Qdrant client: {e}") from e

        self._semaphore = asyncio.Semaphore(self._config.max_concurrency)

        # Verify connectivity
        try:
            await self._client.get_collections()
        except Exception as e:
            raise QdrantConnectionError(
                f"Cannot connect to Qdrant Server at {conn.host}:{conn.port}: {e}"
            ) from e

        # Ensure collection
        await self._ensure_collection()

        self._initialized = True
        logger.info(
            "qdrant_storage_service_initialized",
            host=conn.host,
            port=conn.port,
            collection=self._config.collection.name,
        )

    async def shutdown(self) -> None:
        """Close Qdrant connection."""
        if self._client:
            await self._client.close()
            self._client = None
        self._initialized = False
        logger.info("qdrant_storage_service_shutdown")

    def is_healthy(self) -> bool:
        return self._initialized and self._client is not None

    def get_client_status_snapshot(self) -> dict:
        """Return current Qdrant client availability without a network call."""
        conn = self._config.connection
        collection = self._config.collection
        available = self.is_healthy()
        api_key = conn.api_key or ""
        api_key_configured = bool(api_key)
        key_status = [
            {
                "label": "Qdrant API Key",
                "key_suffix": api_key[-6:] if api_key_configured else None,
                "configured": api_key_configured,
                "healthy": available if api_key_configured else False,
                "status_label": "已配置" if api_key_configured else "未配置",
            }
        ]
        return {
            "status": "available" if available else "unavailable",
            "initialized": self._initialized,
            "client_created": self._client is not None,
            "endpoint": f"{'https' if conn.https else 'http'}://{conn.host}:{conn.port}",
            "host": conn.host,
            "port": conn.port,
            "grpc_port": conn.grpc_port,
            "https": conn.https,
            "prefer_grpc": conn.prefer_grpc,
            "timeout": conn.timeout,
            "collection": collection.name,
            "dense_vector": collection.dense_vector.name,
            "sparse_vector": collection.sparse_vector.name,
            "api_key_configured": api_key_configured,
            "key_status": key_status,
        }

    async def probe_client(self, timeout: float) -> dict:
        """Run a real Qdrant heartbeat against the configured server."""
        if not self._initialized or self._client is None:
            return {"ok": False, "status": "unavailable", "message": "client is not initialized"}
        started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(self._client.get_collections(), timeout=timeout)
        except TimeoutError:
            elapsed = asyncio.get_running_loop().time() - started
            return {
                "ok": False,
                "status": "timeout",
                "message": f"heartbeat timed out after {timeout:g}s",
                "latency_ms": round(elapsed * 1000, 1),
            }
        except Exception as e:
            elapsed = asyncio.get_running_loop().time() - started
            return {
                "ok": False,
                "status": "error",
                "message": str(e),
                "latency_ms": round(elapsed * 1000, 1),
            }
        elapsed = asyncio.get_running_loop().time() - started
        return {
            "ok": True,
            "status": "available",
            "message": "heartbeat passed",
            "latency_ms": round(elapsed * 1000, 1),
        }

    # =========================================================================
    # 2.1 Upsert file points (single file_version_id)
    # =========================================================================

    async def upsert_file_points(
        self,
        project_id: str,
        file_version_id: str,
        points_data: List[PointData],
    ) -> bool:
        """Write Points for a file_version_id with deterministic point ids.

        Args:
            project_id: Project UUID for multi-tenant isolation.
            file_version_id: Content-addressed file version hash.
            points_data: List of chunks to store.

        Returns:
            True if successful.
        """
        self._check_initialized()
        assert self._client is not None
        assert self._semaphore is not None

        collection = self._config.collection.name

        try:
            async with self._semaphore:
                base_payloads = [
                    {
                        "file_version_id": file_version_id,
                        "file_path": pd.file_path,
                        "entity_type": pd.entity_type,
                        "entity_name": pd.entity_name,
                        "business_description": pd.business_description,
                        "project_id": project_id,
                        "blob_hash": pd.blob_hash,
                    }
                    for pd in points_data
                ]
                knowledge_set_id = self._compute_knowledge_set_id(
                    project_id, file_version_id, base_payloads,
                )
                chunk_count = len(base_payloads)

                points = [
                    models.PointStruct(
                        id=self._point_id(
                            project_id, file_version_id, knowledge_set_id, idx,
                        ),
                        vector={
                            self._config.collection.dense_vector.name: pd.dense_vector,
                            self._config.collection.sparse_vector.name: models.SparseVector(
                                indices=pd.sparse_vector.indices,
                                values=pd.sparse_vector.values,
                            ),
                        },
                        payload={
                            **base_payloads[idx],
                            "knowledge_set_id": knowledge_set_id,
                            "chunk_index": idx,
                            "chunk_count": chunk_count,
                        },
                    )
                    for idx, pd in enumerate(points_data)
                ]

                # Upsert in batches
                batch_size = self._config.upsert_batch_size
                for i in range(0, len(points), batch_size):
                    batch = points[i : i + batch_size]
                    await self._client.upsert(
                        collection_name=collection,
                        points=batch,
                    )

            logger.info(
                "upsert_file_points",
                project_id=project_id,
                file_version_id=file_version_id[:16],
                points_count=len(points_data),
                knowledge_set_id=knowledge_set_id[:16],
            )
            return True

        except Exception as e:
            logger.error(
                "upsert_file_points_failed",
                project_id=project_id,
                file_version_id=file_version_id[:16],
                error=str(e),
            )
            raise QdrantOperationError(
                f"Failed to upsert points for {file_version_id}: {e}"
            ) from e

    async def get_file_points(
        self, project_id: str, file_version_id: str
    ) -> List[models.Record]:
        """Get all Points for a file_version_id."""
        self._check_initialized()
        assert self._client is not None
        assert self._semaphore is not None

        collection = self._config.collection.name

        records = []
        offset = None
        while True:
            async with self._semaphore:
                batch, next_offset = await self._client.scroll(
                    collection_name=collection,
                    scroll_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="file_version_id",
                                match=models.MatchValue(value=file_version_id),
                            ),
                            models.FieldCondition(
                                key="project_id",
                                match=models.MatchValue(value=project_id),
                            ),
                        ]
                    ),
                    limit=self._config.scroll_batch_size,
                    offset=offset,
                    with_vectors=True,
                    with_payload=True,
                )
            records.extend(batch)
            if next_offset is None:
                break
            offset = next_offset

        selected_records = self._select_readable_knowledge_set(records)

        logger.debug(
            "get_file_points",
            project_id=project_id,
            file_version_id=file_version_id[:16],
            records_count=len(records),
            selected_count=len(selected_records),
        )
        return selected_records

    # =========================================================================
    # 2.4 Check existence (content-addressing dedup)
    # =========================================================================

    async def check_existence(
        self, project_id: str, file_version_ids: List[str]
    ) -> Dict[str, bool]:
        """Check which file_version_ids already have Points stored.

        Core interface for content-addressed deduplication.
        """
        self._check_initialized()
        assert self._client is not None
        assert self._semaphore is not None

        collection = self._config.collection.name
        result: Dict[str, bool] = {}

        async def _check_one(fvid: str) -> None:
            records = []
            offset = None
            while True:
                async with self._semaphore:
                    batch, next_offset = await self._client.scroll(
                        collection_name=collection,
                        scroll_filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key="file_version_id",
                                    match=models.MatchValue(value=fvid),
                                ),
                                models.FieldCondition(
                                    key="project_id",
                                    match=models.MatchValue(value=project_id),
                                ),
                            ]
                        ),
                        limit=self._config.scroll_batch_size,
                        offset=offset,
                        with_vectors=False,
                        with_payload=[
                            "knowledge_set_id",
                            "chunk_index",
                            "chunk_count",
                        ],
                    )
                records.extend(batch)
                if next_offset is None:
                    break
                offset = next_offset
            result[fvid] = self._has_complete_knowledge_set(records)

        start = time.monotonic()
        batch_size = self._config.scroll_batch_size
        for i in range(0, len(file_version_ids), batch_size):
            batch = file_version_ids[i : i + batch_size]
            tasks = [_check_one(fvid) for fvid in batch]
            await asyncio.gather(*tasks)

        existing_count = sum(1 for v in result.values() if v)
        elapsed = time.monotonic() - start
        logger.info(
            "check_existence",
            project_id=project_id,
            total_checked=len(file_version_ids),
            existing=existing_count,
            elapsed_ms=round(elapsed * 1000, 1),
        )
        return result

    async def get_all_file_version_ids(self, project_id: str) -> Set[str]:
        """Scroll through all Points for a project, extracting file_version_ids.

        Used for project-scoped storage inspection.
        """
        self._check_initialized()
        assert self._client is not None
        assert self._semaphore is not None

        collection = self._config.collection.name
        fvids: Set[str] = set()
        offset = None

        while True:
            async with self._semaphore:
                records, next_offset = await self._client.scroll(
                    collection_name=collection,
                    scroll_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="project_id",
                                match=models.MatchValue(value=project_id),
                            ),
                        ]
                    ),
                    limit=self._config.scroll_batch_size,
                    offset=offset,
                    with_vectors=False,
                    with_payload=["file_version_id"],
                )

            for record in records:
                fvid = record.payload.get("file_version_id")
                if fvid:
                    fvids.add(fvid)

            if next_offset is None:
                break
            offset = next_offset

        logger.debug(
            "get_all_file_version_ids",
            project_id=project_id,
            count=len(fvids),
        )
        return fvids

    # =========================================================================
    # 2.8 Rebrand project_id (migration for project_id derivation upgrade)
    # =========================================================================

    async def rebrand_project_id(
        self, old_project_id: str, new_project_id: str,
    ) -> int:
        """Rewrite all stored points from ``old_project_id`` to ``new_project_id``.

        Used when the project_id derivation rule changes (e.g. random uuid4
        → deterministic uuid5(root_commit)) and existing knowledge points
        must be re-attributed to the new project identity.  Idempotent: a
        no-op when both ids are equal or no points reference the old id.

        Args:
            old_project_id: The project_id currently stored in payloads.
            new_project_id: The target project_id to rewrite payloads to.

        Returns:
            Number of points whose payload was rewritten.
        """
        self._check_initialized()
        assert self._client is not None
        assert self._semaphore is not None

        if old_project_id == new_project_id:
            return 0

        collection = self._config.collection.name
        old_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="project_id",
                    match=models.MatchValue(value=old_project_id),
                ),
            ]
        )

        # Step 1: count points to rebrand (for observability only)
        try:
            async with self._semaphore:
                count_result = await self._client.count(
                    collection_name=collection,
                    count_filter=old_filter,
                    exact=True,
                )
        except Exception as e:
            raise QdrantOperationError(
                f"Failed to count points for rebrand "
                f"({old_project_id} -> {new_project_id}): {e}"
            ) from e

        affected = count_result.count
        if affected == 0:
            logger.info(
                "rebrand_project_id_noop",
                old_project_id=old_project_id,
                new_project_id=new_project_id,
            )
            return 0

        # Step 2: rewrite payload field via set_payload + filter selector
        try:
            async with self._semaphore:
                await self._client.set_payload(
                    collection_name=collection,
                    payload={"project_id": new_project_id},
                    points=models.FilterSelector(filter=old_filter),
                    wait=True,
                )
        except Exception as e:
            raise QdrantOperationError(
                f"Failed to rebrand project_id "
                f"({old_project_id} -> {new_project_id}): {e}"
            ) from e

        logger.info(
            "rebrand_project_id_done",
            old_project_id=old_project_id,
            new_project_id=new_project_id,
            affected=affected,
        )
        return affected

    # =========================================================================
    # 2.9 Collection management
    # =========================================================================

    async def ensure_collection(self) -> None:
        """Public wrapper for collection initialization."""
        await self._ensure_collection()

    async def get_collection_info(self) -> Optional[models.CollectionInfo]:
        """Return collection metadata (point count, index status, etc.)."""
        self._check_initialized()
        assert self._client is not None

        try:
            return await self._client.get_collection(self._config.collection.name)
        except Exception as e:
            logger.error("get_collection_info_failed", error=str(e))
            return None

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _check_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "QdrantStorageService is not initialized. Call initialize() first."
            )

    @staticmethod
    def _point_id(
        project_id: str,
        file_version_id: str,
        knowledge_set_id: str,
        chunk_index: int,
    ) -> str:
        return str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"codebase-rag:point:{project_id}:{file_version_id}:"
            f"{knowledge_set_id}:{chunk_index}",
        ))

    @staticmethod
    def _compute_knowledge_set_id(
        project_id: str,
        file_version_id: str,
        payloads: List[dict],
    ) -> str:
        canonical = json.dumps(
            {
                "project_id": project_id,
                "file_version_id": file_version_id,
                "payloads": payloads,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _select_readable_knowledge_set(
        records: List[models.Record],
    ) -> List[models.Record]:
        complete_sets = QdrantStorageService._complete_knowledge_sets(records)
        if complete_sets:
            selected_id = sorted(complete_sets.keys())[0]
            return complete_sets[selected_id]
        return [
            rec for rec in records
            if not rec.payload or "knowledge_set_id" not in rec.payload
        ]

    @staticmethod
    def _has_complete_knowledge_set(records: List[models.Record]) -> bool:
        if QdrantStorageService._complete_knowledge_sets(records):
            return True
        return any(
            not rec.payload or "knowledge_set_id" not in rec.payload
            for rec in records
        )

    @staticmethod
    def _complete_knowledge_sets(
        records: List[models.Record],
    ) -> Dict[str, List[models.Record]]:
        grouped: Dict[str, Dict[int, models.Record]] = {}
        expected_counts: Dict[str, int] = {}

        for rec in records:
            payload = rec.payload or {}
            knowledge_set_id = payload.get("knowledge_set_id")
            chunk_index = payload.get("chunk_index")
            chunk_count = payload.get("chunk_count")
            if not isinstance(knowledge_set_id, str):
                continue
            if not isinstance(chunk_index, int):
                continue
            if not isinstance(chunk_count, int) or chunk_count <= 0:
                continue
            grouped.setdefault(knowledge_set_id, {})[chunk_index] = rec
            expected_counts[knowledge_set_id] = chunk_count

        complete: Dict[str, List[models.Record]] = {}
        for knowledge_set_id, by_index in grouped.items():
            chunk_count = expected_counts.get(knowledge_set_id, 0)
            expected_indexes = set(range(chunk_count))
            if set(by_index.keys()) == expected_indexes:
                complete[knowledge_set_id] = [
                    by_index[idx] for idx in range(chunk_count)
                ]

        return complete

    async def _ensure_collection(self) -> None:
        """Check/create collection and build payload indexes."""
        assert self._client is not None
        coll_cfg = self._config.collection
        collection_name = coll_cfg.name

        collections = await self._client.get_collections()
        existing_names = {c.name for c in collections.collections}

        if collection_name in existing_names:
            # Validate schema
            info = await self._client.get_collection(collection_name)
            await self._validate_collection_schema(info)

            # Log schema details for debugging
            vectors = info.config.params.vectors
            dense_names = list(vectors.keys()) if isinstance(vectors, dict) else ["<single>"]
            sparse_cfg = info.config.params.sparse_vectors
            sparse_names = list(sparse_cfg.keys()) if sparse_cfg else []
            logger.info(
                "collection_exists_validated",
                collection=collection_name,
                points_count=info.points_count,
                dense_vectors=dense_names,
                sparse_vectors=sparse_names,
            )
        else:
            # Create collection
            dense_cfg = coll_cfg.dense_vector
            distance_map = {
                "cosine": models.Distance.COSINE,
                "euclid": models.Distance.EUCLID,
                "dot": models.Distance.DOT,
            }
            distance = distance_map.get(
                dense_cfg.distance.lower(), models.Distance.COSINE
            )

            # When HNSW is disabled (KV-only mode), use Qdrant defaults
            # (no custom hnsw_config). When enabled, apply user config.
            hnsw = None
            if coll_cfg.hnsw_config.enabled:
                hnsw = models.HnswConfigDiff(
                    m=coll_cfg.hnsw_config.m,
                    ef_construct=coll_cfg.hnsw_config.ef_construct,
                )

            vectors_config = {
                dense_cfg.name: models.VectorParams(
                    size=dense_cfg.size,
                    distance=distance,
                    hnsw_config=hnsw,
                ),
            }

            sparse_vectors_config = {
                coll_cfg.sparse_vector.name: models.SparseVectorParams(),
            }

            await self._client.create_collection(
                collection_name=collection_name,
                vectors_config=vectors_config,
                sparse_vectors_config=sparse_vectors_config,
            )

            logger.info(
                "collection_created",
                collection=collection_name,
                dense_dim=dense_cfg.size,
                dense_distance=dense_cfg.distance,
            )

        # Ensure payload indexes
        for idx_cfg in coll_cfg.payload_indexes:
            schema_type_map = {
                "keyword": models.PayloadSchemaType.KEYWORD,
                "integer": models.PayloadSchemaType.INTEGER,
                "float": models.PayloadSchemaType.FLOAT,
                "text": models.PayloadSchemaType.TEXT,
            }
            schema_type = schema_type_map.get(
                idx_cfg.type.lower(), models.PayloadSchemaType.KEYWORD
            )
            try:
                await self._client.create_payload_index(
                    collection_name=collection_name,
                    field_name=idx_cfg.field,
                    field_schema=schema_type,
                )
            except Exception:
                # Index may already exist, that's fine
                pass

        logger.info(
            "payload_indexes_ensured",
            collection=collection_name,
            indexes=[idx.field for idx in coll_cfg.payload_indexes],
        )

    async def _validate_collection_schema(
        self, info: models.CollectionInfo
    ) -> None:
        """Validate that existing collection matches our expected config."""
        coll_cfg = self._config.collection
        dense_name = coll_cfg.dense_vector.name

        # Check dense vector dimension
        vectors_config = info.config.params.vectors
        if isinstance(vectors_config, dict) and dense_name in vectors_config:
            actual_size = vectors_config[dense_name].size
            expected_size = coll_cfg.dense_vector.size
            if actual_size != expected_size:
                raise QdrantSchemaError(
                    f"Dense vector dimension mismatch: "
                    f"collection has {actual_size}, config expects {expected_size}. "
                    f"This must match the Embedding service's dense dimension."
                )

        # Check sparse vector exists
        sparse_name = coll_cfg.sparse_vector.name
        sparse_config = info.config.params.sparse_vectors
        if sparse_config and sparse_name not in sparse_config:
            raise QdrantSchemaError(
                f"Sparse vector '{sparse_name}' not found in collection. "
                f"Available: {list(sparse_config.keys())}"
            )
