from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import BaseModel, Field

from codebase_rag.config.schema import ManagementApiConfig
from codebase_rag.services.management_api.admin_ui import require_local_request
from codebase_rag.services.management_api.service import ManagementApiService


class _Config(BaseModel):
    management_api: ManagementApiConfig = Field(default_factory=ManagementApiConfig)


class _ConfigManager:
    def __init__(self) -> None:
        self.config = _Config()


class _Embedding:
    def __init__(self) -> None:
        self._initialized = True

    def get_client_status_snapshot(self):
        return {
            "dense": {
                "provider": "gemini",
                "status": "available",
                "initialized": True,
                "endpoint": "https://generativelanguage.googleapis.com",
                "model": "gemini-embedding-001",
                "dimension": 3072,
                "batch_size": 100,
                "request_timeout": 30,
                "total_keys": 1,
                "healthy_keys": 1,
                "key_status": [{"key_suffix": "EMINI1", "healthy": True}],
            },
            "sparse": {
                "provider": "dashscope",
                "status": "available",
                "initialized": True,
                "endpoint": "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
                "model": "text-embedding-v4",
                "dimension": 2048,
                "batch_size": 10,
                "request_timeout": 30,
                "total_keys": 1,
                "healthy_keys": 1,
                "key_status": [{"key_suffix": "SCOPE1", "healthy": True}],
            },
        }

    async def probe_dense_provider(self, timeout: float):
        return {"ok": True, "status": "available", "message": "dense ok", "latency_ms": 1.0}

    async def probe_sparse_provider(self, timeout: float):
        return {"ok": True, "status": "available", "message": "sparse ok", "latency_ms": 1.0}


class _Qdrant:
    _initialized = True

    def get_client_status_snapshot(self):
        return {
            "status": "available",
            "initialized": True,
            "client_created": True,
            "endpoint": "https://qdrant.example:6333",
            "host": "qdrant.example",
            "port": 6333,
            "grpc_port": 6334,
            "https": True,
            "prefer_grpc": False,
            "timeout": 60,
            "collection": "codebase",
            "dense_vector": "dense_vectors",
            "sparse_vector": "sparse_vectors",
            "api_key_configured": True,
            "key_status": [
                {
                    "label": "Qdrant API Key",
                    "key_suffix": "DRANT1",
                    "configured": True,
                    "healthy": True,
                    "status_label": "已配置",
                }
            ],
        }

    async def probe_client(self, timeout: float):
        return {"ok": True, "status": "available", "message": "qdrant ok", "latency_ms": 1.0}


class ManagementAdminStatusTest(unittest.IsolatedAsyncioTestCase):
    def _service(self) -> ManagementApiService:
        return ManagementApiService(
            config_manager=_ConfigManager(),
            git_ops=SimpleNamespace(is_healthy=lambda: True),
            event_detection=SimpleNamespace(_initialized=True),
            workflow=SimpleNamespace(_initialized=True),
            memory_qdrant=SimpleNamespace(_initialized=True),
            qdrant_storage=_Qdrant(),
            embedding=_Embedding(),
            knowledge_sync=SimpleNamespace(),
        )

    async def test_client_status_snapshot_does_not_run_live_probe(self):
        service = self._service()
        response = await service.get_client_status(live=False)

        self.assertEqual(response.status, "available")
        self.assertFalse(response.live_probe)
        self.assertEqual([client.id for client in response.clients], [
            "dashscope_embedding",
            "gemini_embedding",
            "qdrant",
        ])
        self.assertTrue(all(client.live_probe is None for client in response.clients))
        self.assertFalse(response.proxy_environment["required"])
        qdrant = next(client for client in response.clients if client.id == "qdrant")
        self.assertTrue(qdrant.details["api_key_configured"])
        self.assertEqual(qdrant.key_status[0]["key_suffix"], "DRANT1")
        self.assertEqual(qdrant.key_status[0]["status_label"], "已配置")

    async def test_client_status_live_probe_attaches_probe_results(self):
        service = self._service()
        response = await service.get_client_status(live=True)

        self.assertTrue(response.live_probe)
        self.assertEqual(response.status, "available")
        self.assertTrue(all(client.live_probe for client in response.clients))

    def test_admin_ui_requires_local_request(self):
        require_local_request(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))
        with self.assertRaises(HTTPException) as ctx:
            require_local_request(SimpleNamespace(client=SimpleNamespace(host="192.168.1.10")))
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
