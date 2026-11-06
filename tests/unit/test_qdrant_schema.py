from __future__ import annotations

import unittest
from types import SimpleNamespace

from codebase_rag.services.qdrant_storage.errors import QdrantSchemaError
from codebase_rag.services.qdrant_storage.service import QdrantStorageService


class QdrantSchemaValidationTest(unittest.IsolatedAsyncioTestCase):
    def make_service(self) -> QdrantStorageService:
        service = object.__new__(QdrantStorageService)
        service._config_manager = SimpleNamespace(
            config=SimpleNamespace(
                qdrant_storage=SimpleNamespace(
                    collection=SimpleNamespace(
                        name="codebase",
                        dense_vector=SimpleNamespace(name="dense_vectors", size=3072),
                    )
                )
            )
        )
        return service

    @staticmethod
    def info(vectors, sparse_vectors=None):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=vectors,
                    sparse_vectors=sparse_vectors,
                )
            )
        )

    async def test_accepts_exact_dense_only_schema(self):
        info = self.info({
            "dense_vectors": SimpleNamespace(size=3072),
        })
        await self.make_service()._validate_collection_schema(info)

    async def test_rejects_any_sparse_vector_schema(self):
        info = self.info(
            {"dense_vectors": SimpleNamespace(size=3072)},
            sparse_vectors={"sparse_vectors": SimpleNamespace()},
        )
        with self.assertRaisesRegex(QdrantSchemaError, "sparse vector"):
            await self.make_service()._validate_collection_schema(info)

    async def test_rejects_additional_or_missing_dense_vectors(self):
        cases = [
            {},
            {
                "dense_vectors": SimpleNamespace(size=3072),
                "other_dense": SimpleNamespace(size=3072),
            },
        ]
        for vectors in cases:
            with self.subTest(vectors=list(vectors)):
                with self.assertRaisesRegex(QdrantSchemaError, "exactly one dense vector"):
                    await self.make_service()._validate_collection_schema(self.info(vectors))

    async def test_rejects_dense_dimension_mismatch(self):
        info = self.info({"dense_vectors": SimpleNamespace(size=1024)})
        with self.assertRaisesRegex(QdrantSchemaError, "dimension mismatch"):
            await self.make_service()._validate_collection_schema(info)


class QdrantCollectionExistenceTest(unittest.IsolatedAsyncioTestCase):
    """Alias-aware existence: configured name may be an alias to the dense-only collection."""

    def make_service(self, client) -> QdrantStorageService:
        service = object.__new__(QdrantStorageService)
        service._config_manager = SimpleNamespace(
            config=SimpleNamespace(
                qdrant_storage=SimpleNamespace(
                    collection=SimpleNamespace(
                        name="codebase",
                        dense_vector=SimpleNamespace(
                            name="dense_vectors", size=3072, distance="cosine"
                        ),
                        hnsw_config=SimpleNamespace(enabled=False),
                        payload_indexes=[],
                    )
                )
            )
        )
        service._client = client
        return service

    @staticmethod
    def dense_only_info():
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense_vectors": SimpleNamespace(size=3072)},
                    sparse_vectors=None,
                )
            ),
            points_count=8887,
        )

    async def test_alias_name_is_treated_as_existing_collection(self):
        class AliasClient:
            def __init__(self):
                self.created = []

            async def collection_exists(self, name):
                return name == "codebase"

            async def get_collection(self, name):
                return QdrantCollectionExistenceTest.dense_only_info()

            async def create_collection(self, **kwargs):
                self.created.append(kwargs)

        client = AliasClient()
        await self.make_service(client)._ensure_collection()
        self.assertEqual(client.created, [])

    async def test_missing_collection_is_created_dense_only(self):
        class EmptyClient:
            def __init__(self):
                self.created = []

            async def collection_exists(self, name):
                return False

            async def create_collection(self, **kwargs):
                self.created.append(kwargs)

        client = EmptyClient()
        await self.make_service(client)._ensure_collection()
        self.assertEqual(len(client.created), 1)
        kwargs = client.created[0]
        self.assertEqual(kwargs["collection_name"], "codebase")
        self.assertEqual(set(kwargs["vectors_config"]), {"dense_vectors"})
        self.assertNotIn("sparse_vectors_config", kwargs)


if __name__ == "__main__":
    unittest.main()
