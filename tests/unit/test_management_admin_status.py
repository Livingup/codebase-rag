from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import BaseModel, Field

from codebase_rag.common.keypool import KeyPool
from codebase_rag.config.schema import (
    AgentConfig,
    ApiKeyConfig,
    LLMConfig,
    LLMProviderConfig,
    ManagementApiConfig,
    ProviderModelRef,
)
from codebase_rag.services.agent.service import AgentService
from codebase_rag.services.management_api.admin_ui import require_local_request
from codebase_rag.services.management_api.models import InvalidRequestError
from codebase_rag.services.management_api.service import ManagementApiService


class _Config(BaseModel):
    management_api: ManagementApiConfig = Field(default_factory=ManagementApiConfig)


class _ConfigManager:
    def __init__(self) -> None:
        self.config = _Config()


class _Embedding:
    def __init__(self) -> None:
        self._initialized = True
        self.dense_probe_calls = 0
        self.sparse_probe_calls = 0

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
        self.dense_probe_calls += 1
        return {"ok": True, "status": "available", "message": "dense ok", "latency_ms": 1.0}

    async def probe_sparse_provider(self, timeout: float):
        self.sparse_probe_calls += 1
        return {"ok": True, "status": "available", "message": "sparse ok", "latency_ms": 1.0}


class _Agent:
    def __init__(self) -> None:
        self.all_probe_calls = 0
        self.single_probe_calls = []

    def get_llm_provider_status_snapshot(self):
        return [
            {
                "id": "zhipu",
                "name": "zhipu",
                "status": "available",
                "api_format": "zhipu",
                "endpoint": "https://open.bigmodel.cn",
                "models": [
                    {
                        "id": "glm-5.1",
                        "alias": None,
                        "api_model": "glm-5.1",
                        "tool_choice": "auto",
                        "thinking_configured": False,
                        "custom_headers_configured": False,
                        "is_default": True,
                    }
                ],
                "default_provider": "zhipu",
                "default_model": "glm-5.1",
                "is_default_provider": True,
                "total_keys": 1,
                "healthy_keys": 1,
                "key_status": [{"key_suffix": "ZHIPU1", "healthy": True}],
                "request_timeout": 600,
                "max_concurrency": 3,
                "initialized": True,
                "provider_loaded": True,
            }
        ]

    async def probe_llm_providers(self, timeout: float):
        self.all_probe_calls += 1
        return {
            "zhipu": {
                "ok": True,
                "status": "available",
                "message": "Minimal LLM chat request succeeded",
                "latency_ms": 1.0,
                "model": "glm-5.1",
                "api_model": "glm-5.1",
                "finish_reason": "stop",
                "response_received": True,
            }
        }

    async def probe_llm_provider(self, provider_name: str, timeout: float):
        self.single_probe_calls.append(provider_name)
        return {
            "ok": True,
            "status": "available",
            "message": f"single {provider_name} ok",
            "latency_ms": 1.0,
            "model": "glm-5.1",
            "api_model": "glm-5.1",
            "response_received": True,
        }


class _Qdrant:
    _initialized = True

    def __init__(self) -> None:
        self.probe_calls = 0

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
        self.probe_calls += 1
        return {"ok": True, "status": "available", "message": "qdrant ok", "latency_ms": 1.0}


class _ProbeProvider:
    def __init__(self) -> None:
        self.probe_calls = []
        self.chat_completion_called = False

    async def probe_stream(self, api_key: str, model: str, custom_headers=None) -> bool:
        self.probe_calls.append({
            "api_key": api_key,
            "model": model,
            "custom_headers": custom_headers,
        })
        return True

    async def chat_completion(self, *args, **kwargs):
        self.chat_completion_called = True
        raise AssertionError("probe_llm_provider must not use full chat_completion")


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
            agent=_Agent(),
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
            "llm_provider:zhipu",
        ])
        self.assertTrue(all(client.live_probe is None for client in response.clients))
        self.assertFalse(response.proxy_environment["required"])
        qdrant = next(client for client in response.clients if client.id == "qdrant")
        self.assertTrue(qdrant.details["api_key_configured"])
        self.assertEqual(qdrant.key_status[0]["key_suffix"], "DRANT1")
        self.assertEqual(qdrant.key_status[0]["status_label"], "已配置")
        provider = next(client for client in response.clients if client.id == "llm_provider:zhipu")
        self.assertEqual(provider.kind, "model_provider")
        self.assertEqual(provider.details["api_format"], "zhipu")
        self.assertEqual(provider.details["models"], "glm-5.1")
        self.assertEqual(provider.key_status[0]["key_suffix"], "ZHIPU1")

    async def test_client_status_live_probe_attaches_probe_results(self):
        service = self._service()
        response = await service.get_client_status(live=True)

        self.assertTrue(response.live_probe)
        self.assertEqual(response.status, "available")
        self.assertTrue(all(client.live_probe for client in response.clients))
        provider = next(client for client in response.clients if client.id == "llm_provider:zhipu")
        self.assertEqual(provider.live_probe["message"], "Minimal LLM chat request succeeded")
        self.assertEqual(provider.live_probe["api_model"], "glm-5.1")
        self.assertTrue(provider.live_probe["response_received"])

    async def test_single_client_probe_only_attaches_requested_llm_result(self):
        service = self._service()
        response = await service.probe_client_status("llm_provider:zhipu")

        self.assertTrue(response.live_probe)
        self.assertEqual(response.status, "available")
        provider = next(client for client in response.clients if client.id == "llm_provider:zhipu")
        self.assertEqual(provider.live_probe["message"], "single zhipu ok")
        self.assertTrue(all(
            client.live_probe is None
            for client in response.clients
            if client.id != "llm_provider:zhipu"
        ))
        self.assertEqual(service._agent.single_probe_calls, ["zhipu"])
        self.assertEqual(service._agent.all_probe_calls, 0)
        self.assertEqual(service._embedding.dense_probe_calls, 0)
        self.assertEqual(service._embedding.sparse_probe_calls, 0)
        self.assertEqual(service._qdrant.probe_calls, 0)

    async def test_single_client_probe_rejects_unknown_item(self):
        service = self._service()

        with self.assertRaises(InvalidRequestError):
            await service.probe_client_status("llm_provider:missing")

    def test_admin_ui_requires_local_request(self):
        require_local_request(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))
        with self.assertRaises(HTTPException) as ctx:
            require_local_request(SimpleNamespace(client=SimpleNamespace(host="192.168.1.10")))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_llm_provider_status_uses_provider_probe_model_as_default_display(self):
        agent_config = AgentConfig(
            llm=LLMConfig(
                default_provider="openai_compatible_primary",
                default_model="gpt-5.4",
                providers={
                    "deepseek": LLMProviderConfig(
                        api_format="deepseek",
                        api_base_url="https://api.deepseek.com",
                        models=[
                            ProviderModelRef(id="deepseek-v4-flash"),
                            ProviderModelRef(id="deepseek-v4-pro"),
                        ],
                        api_keys=[ApiKeyConfig(key="deepseek-key")],
                    ),
                    "zhipu": LLMProviderConfig(
                        api_format="zhipu",
                        api_base_url="https://open.bigmodel.cn",
                        models=[
                            ProviderModelRef(id="glm-5.1"),
                            ProviderModelRef(id="glm-4.7"),
                        ],
                        api_keys=[ApiKeyConfig(key="zhipu-key")],
                    ),
                    "openai_compatible_secondary": LLMProviderConfig(
                        api_format="gemini",
                        api_base_url="http://localhost:8317",
                        models=[
                            ProviderModelRef(id="gemini-3.1-pro", alias="gemini-3.1-pro-preview"),
                            ProviderModelRef(id="gemini-3-flash"),
                        ],
                        api_keys=[ApiKeyConfig(key="gemini-key")],
                    ),
                    "openai_compatible_primary": LLMProviderConfig(
                        api_format="openai_responses",
                        api_base_url="${OPENAI_COMPAT_SECONDARY_BASE_URL}",
                        models=[ProviderModelRef(id="gpt-5.4")],
                        api_keys=[ApiKeyConfig(key="openai_compatible_primary-key")],
                    ),
                },
            )
        )
        service = AgentService(config_manager=SimpleNamespace(config=SimpleNamespace(agent=agent_config)))
        service._initialized = True
        service._providers = {"deepseek": object(), "zhipu": object(), "openai_compatible_secondary": object(), "openai_compatible_primary": object()}

        statuses = {item["id"]: item for item in service.get_llm_provider_status_snapshot()}

        self.assertEqual(statuses["deepseek"]["default_model"], "deepseek-v4-flash")
        self.assertEqual(statuses["deepseek"]["default_model_id"], "deepseek-v4-flash")
        self.assertEqual(statuses["zhipu"]["default_model"], "glm-5.1")
        self.assertEqual(statuses["openai_compatible_secondary"]["default_model"], "gemini-3.1-pro-preview")
        self.assertEqual(statuses["openai_compatible_secondary"]["default_model_id"], "gemini-3.1-pro")
        self.assertEqual(statuses["openai_compatible_primary"]["default_model"], "gpt-5.4")
        self.assertEqual(statuses["openai_compatible_primary"]["global_default_model"], "gpt-5.4")
        self.assertTrue(statuses["deepseek"]["models"][0]["is_default"])
        self.assertTrue(statuses["openai_compatible_primary"]["models"][0]["is_default"])

    async def test_llm_live_probe_uses_first_configured_model_and_stream_probe(self):
        agent_config = AgentConfig(
            llm=LLMConfig(
                default_provider="openai_compatible_primary",
                default_model="gpt-5.4",
                providers={
                    "openai_compatible_primary": LLMProviderConfig(
                        api_format="openai_responses",
                        api_base_url="${OPENAI_COMPAT_SECONDARY_BASE_URL}",
                        models=[
                            ProviderModelRef(
                                id="first-model",
                                alias="api-first-model",
                                custom_headers={"X-Test": "probe"},
                            ),
                            ProviderModelRef(id="gpt-5.4"),
                        ],
                        api_keys=[ApiKeyConfig(key="probe-key")],
                    ),
                },
            )
        )
        service = AgentService(config_manager=SimpleNamespace(config=SimpleNamespace(agent=agent_config)))
        provider = _ProbeProvider()
        service._initialized = True
        service._providers = {"openai_compatible_primary": provider}
        service._keypools = {"openai_compatible_primary": KeyPool(["probe-key"])}

        result = await service.probe_llm_provider("openai_compatible_primary", timeout=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["model"], "first-model")
        self.assertEqual(result["api_model"], "api-first-model")
        self.assertTrue(result["response_received"])
        self.assertEqual(len(provider.probe_calls), 1)
        self.assertEqual(provider.probe_calls[0]["api_key"], "probe-key")
        self.assertEqual(provider.probe_calls[0]["model"], "api-first-model")
        self.assertEqual(provider.probe_calls[0]["custom_headers"]["X-Test"], "probe")
        self.assertTrue(provider.probe_calls[0]["custom_headers"]["X-Relay-Session-Id"].startswith(
            "codebase-rag-probe-openai_compatible_primary-"
        ))
        self.assertFalse(provider.chat_completion_called)


if __name__ == "__main__":
    unittest.main()
