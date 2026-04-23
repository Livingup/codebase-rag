"""Qdrant Storage Service — persistent KV storage for file_version_id → Points.

Pure storage layer wrapping qdrant-client AsyncQdrantClient.
No semantic search — only CRUD by file_version_id with project_id isolation.
"""

from __future__ import annotations

import asyncio
import uuid
import time
from typing import Dict, List, Optional, Set

from qdrant_client import AsyncQdrantClient, models

from codebase_rag.common.logging import get_logger
from codebase_rag.common.types import SparseVector
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import QdrantStorageConfig
from codebase_rag.services.qdrant_storage.errors import (
    QdrantConnectionError,
    QdrantOperationError,
    QdrantSchemaError,
)
from codebase_rag.services.qdrant_storage.models import BatchResult, PointData

logger = get_logger(__name__)


class QdrantStorageService:
    """Qdrant Server persistent storage for file knowledge points.

    Manages connection lifecycle, collection initialization, and
    CRUD operations scoped by project_id + file_version_id.

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

    # =========================================================================
    # 2.1 Upsert file points (single file_version_id)
    # =========================================================================

    async def upsert_file_points(
        self,
        project_id: str,
        file_version_id: str,
        points_data: List[PointData],
    ) -> bool:
        """Write Points for a file_version_id. Idempotent: delete-then-write.

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
                # Delete existing points for this file_version_id (idempotent)
                await self._client.delete(
                    collection_name=collection,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
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
                        )
                    ),
                )

                # Build Qdrant points
                points = [
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector={
                            self._config.collection.dense_vector.name: pd.dense_vector,
                            self._config.collection.sparse_vector.name: models.SparseVector(
                                indices=pd.sparse_vector.indices,
                                values=pd.sparse_vector.values,
                            ),
                        },
                        payload={
                            "file_version_id": file_version_id,
                            "file_path": pd.file_path,
                            "entity_type": pd.entity_type,
                            "entity_name": pd.entity_name,
                            "business_description": pd.business_description,
                            "project_id": project_id,
                            "blob_hash": pd.blob_hash,
                        },
                    )
                    for pd in points_data
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

    # =========================================================================
    # 2.2 Batch upsert multiple files
    # =========================================================================

    async def batch_upsert_files(
        self,
        project_id: str,
        files_points: Dict[str, List[PointData]],
    ) -> BatchResult:
        """Batch upsert points for multiple file_version_ids.

        Partial failures don't affect already succeeded writes.
        """
        self._check_initialized()
        result = BatchResult()

        async def _upsert_one(fvid: str, pdata: List[PointData]) -> None:
            try:
                await self.upsert_file_points(project_id, fvid, pdata)
                result.succeeded.append(fvid)
            except Exception as e:
                result.failed[fvid] = str(e)

        # Run concurrently (semaphore controls actual parallelism)
        tasks = [_upsert_one(fvid, pdata) for fvid, pdata in files_points.items()]
        await asyncio.gather(*tasks)

        logger.info(
            "batch_upsert_files",
            project_id=project_id,
            total=len(files_points),
            succeeded=len(result.succeeded),
            failed=len(result.failed),
        )
        return result

    # =========================================================================
    # 2.3 Get file points
    # =========================================================================

    async def get_file_points(
        self, project_id: str, file_version_id: str
    ) -> List[models.Record]:
        """Get all Points for a file_version_id."""
        self._check_initialized()
        assert self._client is not None
        assert self._semaphore is not None

        collection = self._config.collection.name

        async with self._semaphore:
            records, _ = await self._client.scroll(
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
                with_vectors=True,
                with_payload=True,
            )

        logger.debug(
            "get_file_points",
            project_id=project_id,
            file_version_id=file_version_id[:16],
            records_count=len(records),
        )
        return records

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
            async with self._semaphore:
                count = await self._client.count(
                    collection_name=collection,
                    count_filter=models.Filter(
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
                    exact=True,
                )
                result[fvid] = count.count > 0

        # Batch + concurrent checks
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

    # =========================================================================
    # 2.5 Delete file points (single)
    # =========================================================================

    async def delete_file_points(
        self, project_id: str, file_version_id: str
    ) -> bool:
        """Delete all Points for a file_version_id. Idempotent."""
        self._check_initialized()
        assert self._client is not None
        assert self._semaphore is not None

        collection = self._config.collection.name

        try:
            async with self._semaphore:
                await self._client.delete(
                    collection_name=collection,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
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
                        )
                    ),
                )

            logger.info(
                "delete_file_points",
                project_id=project_id,
                file_version_id=file_version_id[:16],
            )
            return True

        except Exception as e:
            logger.error(
                "delete_file_points_failed",
                project_id=project_id,
                file_version_id=file_version_id[:16],
                error=str(e),
            )
            raise QdrantOperationError(
                f"Failed to delete points for {file_version_id}: {e}"
            ) from e

    # =========================================================================
    # 2.6 Batch delete
    # =========================================================================

    async def batch_delete_files(
        self, project_id: str, file_version_ids: List[str]
    ) -> BatchResult:
        """Batch delete Points for multiple file_version_ids."""
        self._check_initialized()
        result = BatchResult()

        async def _delete_one(fvid: str) -> None:
            try:
                await self.delete_file_points(project_id, fvid)
                result.succeeded.append(fvid)
            except Exception as e:
                result.failed[fvid] = str(e)

        tasks = [_delete_one(fvid) for fvid in file_version_ids]
        await asyncio.gather(*tasks)

        logger.info(
            "batch_delete_files",
            project_id=project_id,
            total=len(file_version_ids),
            succeeded=len(result.succeeded),
            failed=len(result.failed),
        )
        return result

    # =========================================================================
    # 2.7 Clone points for rename (no LLM / embedding needed)
    # =========================================================================

    async def clone_points_for_rename(
        self,
        project_id: str,
        blob_hash: str,
        new_file_version_id: str,
        new_file_path: str,
    ) -> int:
        """Clone existing knowledge points to a new file path for a pure rename.

        Queries points by ``blob_hash`` + ``project_id``, creates new points
        with identical vectors and business descriptions but updated
        ``file_version_id`` and ``file_path`` payload fields.

        Args:
            project_id: Project UUID.
            blob_hash: Git blob SHA-1 (unchanged by rename).
            new_file_version_id: hash(new_file_path + blob_hash).
            new_file_path: The renamed file's new repository-relative path.

        Returns:
            Number of points cloned.  0 if no source points found.
        """
        self._check_initialized()
        assert self._client is not None
        assert self._semaphore is not None

        collection = self._config.collection.name

        try:
            # Query source points by blob_hash + project_id
            async with self._semaphore:
                records, _ = await self._client.scroll(
                    collection_name=collection,
                    scroll_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="blob_hash",
                                match=models.MatchValue(value=blob_hash),
                            ),
                            models.FieldCondition(
                                key="project_id",
                                match=models.MatchValue(value=project_id),
                            ),
                        ]
                    ),
                    limit=self._config.scroll_batch_size,
                    with_vectors=True,
                    with_payload=True,
                )

            if not records:
                logger.info(
                    "clone_points_no_source",
                    project_id=project_id,
                    blob_hash=blob_hash[:16],
                    new_file_path=new_file_path,
                )
                return 0

            # Delete any existing points for the new_file_version_id (idempotent)
            async with self._semaphore:
                await self._client.delete(
                    collection_name=collection,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key="file_version_id",
                                    match=models.MatchValue(value=new_file_version_id),
                                ),
                                models.FieldCondition(
                                    key="project_id",
                                    match=models.MatchValue(value=project_id),
                                ),
                            ]
                        )
                    ),
                )

            # Build cloned points with updated payload
            cloned_points = []
            for rec in records:
                payload = dict(rec.payload) if rec.payload else {}
                payload["file_version_id"] = new_file_version_id
                payload["file_path"] = new_file_path

                vectors = {}
                if rec.vector and isinstance(rec.vector, dict):
                    for vec_name, vec_data in rec.vector.items():
                        if isinstance(vec_data, models.SparseVector):
                            vectors[vec_name] = vec_data
                        else:
                            vectors[vec_name] = vec_data

                cloned_points.append(models.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vectors,
                    payload=payload,
                ))

            # Upsert cloned points
            async with self._semaphore:
                batch_size = self._config.upsert_batch_size
                for i in range(0, len(cloned_points), batch_size):
                    batch = cloned_points[i : i + batch_size]
                    await self._client.upsert(
                        collection_name=collection,
                        points=batch,
                    )

            logger.info(
                "clone_points_for_rename",
                project_id=project_id,
                blob_hash=blob_hash[:16],
                new_file_path=new_file_path,
                new_file_version_id=new_file_version_id[:16],
                cloned_count=len(cloned_points),
            )
            return len(cloned_points)

        except Exception as e:
            logger.error(
                "clone_points_for_rename_failed",
                project_id=project_id,
                blob_hash=blob_hash[:16],
                new_file_path=new_file_path,
                error=str(e),
            )
            raise QdrantOperationError(
                f"Failed to clone points for rename {new_file_path}: {e}"
            ) from e

    # =========================================================================
    # 2.8 Get all file_version_ids for a project (GC use)
    # =========================================================================

    async def get_all_file_version_ids(self, project_id: str) -> Set[str]:
        """Scroll through all Points for a project, extracting file_version_ids.

        Used by GC service to find orphan knowledge points.
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
    # 2.8 Collection management
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
