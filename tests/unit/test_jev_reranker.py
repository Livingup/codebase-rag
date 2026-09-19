from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from codebase_rag.config.schema import (
    ApiKeyConfig,
    LLMProviderConfig,
    MCPRerankerProviderEntry,
    ProviderModelRef,
    RetryConfig,
)
from codebase_rag.services.agent.errors import AgentTaskComplete
from codebase_rag.services.mcp_service.jev_client import (
    JevRerankerClient,
    _build_jev_endpoint,
    _build_jev_payload,
)
from codebase_rag.services.mcp_service.models import (
    CandidatePoint,
    RerankerError,
    RerankerScoreEntry,
)
from codebase_rag.services.mcp_service.service import McpService


def _candidate(cid: str, vector_score: float = 0.5) -> CandidatePoint:
    return CandidatePoint(
        id=cid,
        file_path=f"src/{cid}.py",
        business_description=f"Business description for {cid}",
        vector_score=vector_score,
    )


class JevClientTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = JevRerankerClient()

    def test_build_jev_endpoint(self) -> None:
        # Base URLs without path get /v1/systemone appended
        self.assertEqual(
            _build_jev_endpoint("https://api.typesafe.ai"),
            "https://api.typesafe.ai/v1/systemone",
        )
        self.assertEqual(
            _build_jev_endpoint("http://localhost:8317/"),
            "http://localhost:8317/v1/systemone",
        )
        # Explicit endpoints remain as-is
        self.assertEqual(
            _build_jev_endpoint("https://openrouter.ai/api/alpha/decisions"),
            "https://openrouter.ai/api/alpha/decisions",
        )
        self.assertEqual(
            _build_jev_endpoint("https://api.typesafe.ai/v1/systemone"),
            "https://api.typesafe.ai/v1/systemone",
        )

    def test_build_jev_payload(self) -> None:
        c = _candidate("auth", 0.7)
        payload = _build_jev_payload("jev-latest", "用户认证机制", c)

        self.assertEqual(payload["model"], "jev-latest")
        self.assertEqual(payload["state"]["file_path"], "src/auth.py")
        self.assertEqual(payload["state"]["target_context"], "Business description for auth")

        q = payload["questions"]["relevance"]
        self.assertEqual(q["type"], "noul")
        self.assertIn("用户认证机制", q["instructions"])
        self.assertIn("true", q["criteria"])
        self.assertIn("false", q["criteria"])
        # Ensure no non-standard fields exist in question
        self.assertEqual(set(q.keys()), {"type", "instructions", "criteria"})

    async def test_evaluate_candidates_success_and_dual_key_sort(self) -> None:
        pcfg = LLMProviderConfig(
            api_format="jev",
            api_base_url="https://api.typesafe.ai/v1/systemone",
            api_keys=[ApiKeyConfig(key="sk-test-key")],
            retry=RetryConfig(max_retries_per_key=2, retry_base_delay=0.01),
        )
        entry = MCPRerankerProviderEntry(provider="jev_p", model="jev-latest", priority=5)

        c1 = _candidate("doc1", vector_score=0.4)
        c2 = _candidate("doc2", vector_score=0.9)  # Higher vector score
        c3 = _candidate("doc3", vector_score=0.8)

        mock_responses = {
            "src/doc1.py": 0.95,
            "src/doc2.py": 0.95,  # Same noul score as doc1, but higher vector score (0.9 > 0.4)
            "src/doc3.py": 0.70,
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            import json
            body = json.loads(request.content.decode("utf-8"))
            file_path = body["state"]["file_path"]
            noul = mock_responses[file_path]
            data = {"answers": {"relevance": {"type": "noul", "noul": noul}}}
            return httpx.Response(200, json=data)

        transport = httpx.MockTransport(handler)
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            scores = await self.client.evaluate_candidates(
                query="test query",
                candidates=[c1, c2, c3],
                entry=entry,
                provider_cfg=pcfg,
                top_k=3,
            )

        self.assertEqual(len(scores), 3)
        # doc2 should be first because noul is 0.95 and vector_score is 0.9
        self.assertEqual(scores[0].id, "doc2")
        self.assertEqual(scores[0].score, 0.95)
        # doc1 second (noul 0.95, vector 0.4)
        self.assertEqual(scores[1].id, "doc1")
        self.assertEqual(scores[1].score, 0.95)
        # doc3 third (noul 0.70)
        self.assertEqual(scores[2].id, "doc3")
        self.assertEqual(scores[2].score, 0.70)

    async def test_keypool_failover_on_401(self) -> None:
        pcfg = LLMProviderConfig(
            api_format="jev",
            api_base_url="https://api.typesafe.ai/v1/systemone",
            api_keys=[
                ApiKeyConfig(key="sk-bad-key"),
                ApiKeyConfig(key="sk-good-key"),
            ],
            retry=RetryConfig(max_retries_per_key=2, retry_base_delay=0.01),
        )
        entry = MCPRerankerProviderEntry(provider="jev_multi_key", model="jev-latest", priority=5)
        c = _candidate("test_c")

        async def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("Authorization", "")
            if "sk-bad-key" in auth:
                return httpx.Response(401, json={"error": "Unauthorized"})
            return httpx.Response(200, json={"answers": {"relevance": {"type": "noul", "noul": 0.88}}})

        transport = httpx.MockTransport(handler)
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            scores = await self.client.evaluate_candidates(
                query="query",
                candidates=[c],
                entry=entry,
                provider_cfg=pcfg,
                top_k=1,
            )

        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0].id, "test_c")
        self.assertEqual(scores[0].score, 0.88)

    async def test_failure_rate_threshold_raises_reranker_error(self) -> None:
        pcfg = LLMProviderConfig(
            api_format="jev",
            api_base_url="https://api.typesafe.ai/v1/systemone",
            api_keys=[ApiKeyConfig(key="sk-test-key")],
            retry=RetryConfig(max_retries_per_key=1, retry_base_delay=0.01),
        )
        entry = MCPRerankerProviderEntry(provider="jev_failing", model="jev-latest", priority=5)
        candidates = [_candidate(f"c_{i}") for i in range(5)]

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "Internal Server Error"})

        transport = httpx.MockTransport(handler)
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            with self.assertRaises(RerankerError):
                await self.client.evaluate_candidates(
                    query="query",
                    candidates=candidates,
                    entry=entry,
                    provider_cfg=pcfg,
                    top_k=5,
                )


class McpServiceJevIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def _create_service(self, providers_cfg, reranker_providers, agent) -> McpService:
        config = SimpleNamespace(
            agent=SimpleNamespace(
                llm=SimpleNamespace(
                    providers=providers_cfg,
                )
            ),
            mcp=SimpleNamespace(
                reranker=SimpleNamespace(
                    recall_limit=20,
                    timeout=600,
                    max_rate_limit_retries=1,
                    providers=reranker_providers,
                ),
                knowledge_submission=SimpleNamespace(point_token_limit=8000),
            ),
        )
        return McpService(
            config_manager=SimpleNamespace(config=config),
            embedding=SimpleNamespace(),
            memory_qdrant=SimpleNamespace(),
            agent=agent,
        )

    async def test_jev_success_does_not_call_agent(self) -> None:
        jev_cfg = LLMProviderConfig(
            api_format="jev",
            api_base_url="https://api.typesafe.ai/v1/systemone",
            api_keys=[ApiKeyConfig(key="sk-key")],
        )
        providers_cfg = {"jev_p": jev_cfg}
        reranker_providers = [
            MCPRerankerProviderEntry(provider="jev_p", model="jev-latest", priority=5)
        ]

        agent_mock = AsyncMock()
        service = self._create_service(providers_cfg, reranker_providers, agent_mock)
        await service._register_reranker_agent()

        candidates = [_candidate("c1", 0.5)]

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"answers": {"relevance": {"type": "noul", "noul": 0.92}}})

        transport = httpx.MockTransport(handler)
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            scores = await service._run_reranker(query="q", candidates=candidates, top_k=1)

        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0].id, "c1")
        self.assertEqual(scores[0].score, 0.92)
        # Agent was never invoked!
        agent_mock.run_agent.assert_not_called()

    async def test_jev_failing_cascades_to_agent_fallback(self) -> None:
        jev_cfg = LLMProviderConfig(
            api_format="jev",
            api_base_url="https://api.typesafe.ai/v1/systemone",
            api_keys=[ApiKeyConfig(key="sk-key")],
            retry=RetryConfig(max_retries_per_key=1, retry_base_delay=0.01),
        )
        agent_pcfg = LLMProviderConfig(
            api_format="deepseek",
            api_base_url="https://api.deepseek.com",
            api_keys=[ApiKeyConfig(key="sk-deepseek")],
        )
        providers_cfg = {
            "jev_failing": jev_cfg,
            "deepseek_agent": agent_pcfg,
        }
        reranker_providers = [
            MCPRerankerProviderEntry(provider="jev_failing", model="jev-latest", priority=5),
            MCPRerankerProviderEntry(provider="deepseek_agent", model="deepseek-flash", priority=10),
        ]

        async def agent_plan(**kwargs):
            handlers = kwargs["tool_handlers"]
            with self.assertRaises(AgentTaskComplete):
                await handlers["submit_scores"]([{"id": "c1", "score": 0.85}])

        agent_mock = SimpleNamespace(
            run_agent=AsyncMock(side_effect=agent_plan),
            register_agent=AsyncMock(),
        )

        service = self._create_service(providers_cfg, reranker_providers, agent_mock)
        await service._register_reranker_agent()

        candidates = [_candidate("c1", 0.5)]

        # Mock Jev failing with 500
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "Down"})

        transport = httpx.MockTransport(handler)
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            scores = await service._run_reranker(query="q", candidates=candidates, top_k=1)

        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0].id, "c1")
        self.assertEqual(scores[0].score, 0.85)
        # Agent was successfully called as fallback!
        self.assertEqual(agent_mock.run_agent.call_count, 1)


if __name__ == "__main__":
    unittest.main()
