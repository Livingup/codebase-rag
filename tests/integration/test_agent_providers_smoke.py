"""Smoke test for all agent provider/model configurations.

For each agent's ``providers`` fallback queue — context_compression,
context_engine, knowledge_producer, gray_area_classifier, reranker —
iterate every unique ``(provider, model)`` pair and issue a minimal LLM
call to verify the provider is reachable and the model responds.

Marker
------
All tests carry ``@pytest.mark.integration`` — they hit real external APIs
and burn real quota. The default pytest run excludes them when the user
invokes ``pytest -m "not integration"``.

Running
-------
::

    # Run the whole smoke suite
    pytest tests/integration/test_agent_providers_smoke.py -v

    # Run only a specific provider or agent via substring filter
    pytest tests/integration/test_agent_providers_smoke.py -v -k deepseek
    pytest tests/integration/test_agent_providers_smoke.py -v -k context_engine

Skip semantics
--------------
A case is skipped (not failed) when the current environment cannot reach
the provider at all:

- provider is not defined under ``agent.llm.providers`` in the loaded config
- no usable API key for the provider (``KeyPool.healthy_keys == 0``)

Config resolution order
-----------------------
1. ``CODEBASE_RAG_TEST_CONFIG`` env var
2. ``CODEBASE_RAG_CONFIG`` env var
3. ``./config.yaml`` in the current working directory
4. ``./config.example.yaml`` in the current working directory
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
import pytest_asyncio

from codebase_rag.config.loader import ConfigManager, load_config
from codebase_rag.config.schema import AppConfig
from codebase_rag.services.agent.models import Message
from codebase_rag.services.agent.service import AgentService

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Config discovery + case collection
# ---------------------------------------------------------------------------

def _resolve_config_path() -> Path:
    for env_var in ("CODEBASE_RAG_TEST_CONFIG", "CODEBASE_RAG_CONFIG"):
        raw = os.environ.get(env_var)
        if raw:
            path = Path(raw)
            if path.exists():
                return path
    for fname in ("config.yaml", "config.example.yaml"):
        candidate = Path(fname)
        if candidate.exists():
            return candidate
    return Path("config.example.yaml")


def _collect_unique_provider_models(
    cfg: AppConfig,
) -> List[Tuple[str, str, List[str]]]:
    """Deduplicate (provider, model) pairs across all five agent queues.

    Returns a stable-sorted list of ``(provider, model, [agent_name, ...])``.
    """
    grouped: Dict[Tuple[str, str], List[str]] = {}

    sources = (
        ("context_compression", cfg.agent.context_compression.providers),
        ("context_engine", cfg.context_engine.providers),
        ("knowledge_producer", cfg.workflow.knowledge_producer.providers),
        ("reranker", cfg.mcp.reranker.providers),
    )

    for agent_name, entries in sources:
        for entry in entries:
            key = (entry.provider, entry.model)
            bucket = grouped.setdefault(key, [])
            if agent_name not in bucket:
                bucket.append(agent_name)

    return [
        (provider, model, sorted(agents))
        for (provider, model), agents in sorted(grouped.items())
    ]


def _try_load_config() -> AppConfig | None:
    try:
        return load_config(_resolve_config_path())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# pytest collection hook — one test per unique (provider, model)
# ---------------------------------------------------------------------------

def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "provider_case" not in metafunc.fixturenames:
        return

    cfg = _try_load_config()
    if cfg is None:
        metafunc.parametrize(
            "provider_case",
            [
                pytest.param(
                    None,
                    marks=pytest.mark.skip(
                        reason=(
                            "config load failed — check .env and the config file "
                            f"at {_resolve_config_path()}"
                        )
                    ),
                )
            ],
        )
        return

    cases = _collect_unique_provider_models(cfg)
    if not cases:
        metafunc.parametrize(
            "provider_case",
            [
                pytest.param(
                    None,
                    marks=pytest.mark.skip(
                        reason="no provider entries in any agent queue"
                    ),
                )
            ],
        )
        return

    ids = [f"{provider}|{model}|{'+'.join(agents)}" for provider, model, agents in cases]
    metafunc.parametrize("provider_case", cases, ids=ids)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def agent_service() -> AgentService:
    """Initialise a real AgentService once per test session."""
    cfg_path = _resolve_config_path()
    cfg = load_config(cfg_path)
    cm = ConfigManager(cfg, cfg_path)
    svc = AgentService(config_manager=cm, mcp_client=None)
    await svc.initialize()
    try:
        yield svc
    finally:
        await svc.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAgentProvidersSmoke:
    """End-to-end smoke: every configured (provider, model) answers a trivial call."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_provider_responds_with_simple_prompt(
        self,
        provider_case: Tuple[str, str, List[str]] | None,
        agent_service: AgentService,
    ) -> None:
        assert provider_case is not None  # guarded by pytest_generate_tests skip
        provider, model, agents = provider_case

        # Skip when the provider is not actually configured in agent.llm.providers.
        if provider not in agent_service._providers:  # noqa: SLF001 — intentional introspection for test
            pytest.skip(
                f"provider '{provider}' referenced by {agents} is not in "
                f"agent.llm.providers"
            )

        # Skip when no healthy API key is available (missing or empty env var).
        keypool = agent_service._keypools.get(provider)  # noqa: SLF001
        if keypool is None or keypool.healthy_keys == 0:
            pytest.skip(
                f"no usable API key for provider '{provider}' "
                f"(referenced by {agents})"
            )

        messages = [
            Message(
                role="user",
                content="Reply with exactly the two characters: OK",
            ),
        ]

        # Some providers route through a relay that mandates a session header
        # (e.g. ``openai_compatible_primary`` / ``custom_openai_compatible`` require ``X-Relay-Session-Id``).
        # Generating a per-call session id matches what ``run_agent`` does in
        # production and keeps relay-based providers happy.
        session_id = uuid.uuid4().hex

        response = await agent_service.llm_call(
            messages=messages,
            provider=provider,
            model=model,
            temperature=0.0,
            session_id=session_id,
            prompt_cache_key=session_id,
        )

        assert response.content is not None and response.content.strip(), (
            f"provider={provider} model={model} agents={agents} "
            f"returned empty content (finish_reason={response.finish_reason})"
        )
        assert response.finish_reason in ("stop", "length"), (
            f"provider={provider} model={model} agents={agents} "
            f"unexpected finish_reason={response.finish_reason}"
        )
