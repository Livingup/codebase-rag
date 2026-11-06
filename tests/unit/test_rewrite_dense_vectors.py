"""Deterministic tests for the dense-vector migration script."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codebase_rag.config.schema import AppConfig


def _load_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "rewrite_dense_vectors.py"
    spec = importlib.util.spec_from_file_location("rewrite_dense_vectors", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rewrite_script = _load_script()


class _Client:
    def __init__(self, records=None):
        self.records = records or []
        self.update_calls = []
        self.payload_calls = []
        self.scroll_calls = 0
        self.closed = False

    async def scroll(self, **kwargs):
        self.scroll_calls += 1
        if self.scroll_calls > 1:
            return [], None
        return self.records, None

    async def update_vectors(self, **kwargs):
        self.update_calls.append(kwargs)

    async def set_payload(self, **kwargs):
        self.payload_calls.append(kwargs)

    async def close(self):
        self.closed = True


class _Embedding:
    def __init__(self):
        self.calls = []

    async def embed_dense(self, inputs, is_query=False):
        self.calls.append((list(inputs), is_query))
        return [[float(index + 1), 0.0, 1.0] for index, _ in enumerate(inputs)]


def _record(point_id, description="old text knowledge"):
    return SimpleNamespace(
        id=point_id,
        payload={
            "business_description": description,
            "file_path": "src/main.py",
            "project_id": "project-1",
            "blob_hash": "old-blob",
        },
    )


def test_registry_uses_real_list_schema_and_keeps_repository_copies(tmp_path):
    registry = tmp_path / "project_registry.json"
    registry.write_text(
        json.dumps([
            {"project_id": "p1", "git_repo_path": str(tmp_path)},
            {"project_id": "p1", "git_repo_path": str(tmp_path)},
            {"project_id": "missing", "git_repo_path": str(tmp_path / 'missing')},
        ]),
        encoding="utf-8",
    )
    roots = rewrite_script._load_registered_repo_roots(str(registry))
    assert list(roots) == ["p1"]
    assert len(roots["p1"]) == 1
    assert Path(roots["p1"][0]).resolve() == tmp_path.resolve()


@pytest.mark.asyncio
async def test_text_batch_rewrites_configured_vector_and_backfills_text_type():
    client = _Client()
    embedding = _Embedding()
    count = await rewrite_script._migrate_batch(
        qdrant=client,
        records=[_record("point-1"), _record("point-2", description="  ")],
        collection_name="custom_collection",
        dense_name="custom_dense",
        embedding=embedding,
        git_ops=SimpleNamespace(),
        repo_roots={},
        dry_run=False,
    )
    assert count == 1
    assert embedding.calls == [(["old text knowledge"], False)]
    vectors = client.update_calls[0]["points"]
    assert vectors[0].vector == {"custom_dense": [1.0, 0.0, 1.0]}
    assert client.payload_calls[0]["payload"] == {"file_type": "text"}
    assert client.payload_calls[0]["points"] == ["point-1"]


@pytest.mark.asyncio
async def test_dry_run_loads_real_config_without_constructing_embedding_or_writing():
    record = _record("point-1")
    client = _Client([record])
    config = AppConfig()
    config.management_api.registry_path = "unused-registry.json"
    with (
        patch.object(rewrite_script, "load_config", return_value=config),
        patch.object(rewrite_script, "setup_logging"),
        patch.object(rewrite_script, "_create_qdrant_client", return_value=client),
        patch.object(
            rewrite_script,
            "EmbeddingService",
            side_effect=AssertionError("dry-run must not construct EmbeddingService"),
        ),
    ):
        count = await rewrite_script.rewrite_all_points(
            "unused-config.yaml", batch_size=32, dry_run=True,
        )
    assert count == 1
    assert client.closed
    assert client.update_calls == []
    assert client.payload_calls == []



@pytest.mark.asyncio
async def test_gemini_keypool_embedder_round_robin_and_429_rotation():
    embedder = rewrite_script.GeminiKeyPoolEmbedder(
        keys=["key_alpha", "key_beta"],
        api_base_url="https://fake-gemini.com",
    )

    class DummyResponse:
        def __init__(self, status_code, json_data, text=""):
            self.status_code = status_code
            self._json = json_data
            self.text = text

        def json(self):
            return self._json

    call_urls = []

    async def mock_post(url, json):
        call_urls.append(url)
        if "key_alpha" in url:
            return DummyResponse(200, {"embeddings": [{"values": [0.1, 0.2]}]})
        elif "key_beta" in url:
            # key_beta returns 429
            return DummyResponse(429, {}, text="Resource exhausted: 429 quota exceeded")
        return DummyResponse(500, {}, text="unexpected")

    embedder._client = SimpleNamespace(post=mock_post)

    # 1st call: uses key_alpha -> success -> advances to key_beta
    res1 = await embedder.embed_dense(["text 1"])
    assert res1 == [[0.1, 0.2]]
    assert "key=key_alpha" in call_urls[0]

    # 2nd call: starts with key_beta -> 429 -> immediate switch to key_alpha -> success
    res2 = await embedder.embed_dense(["text 2"])
    assert res2 == [[0.1, 0.2]]
    assert "key=key_beta" in call_urls[1]
    assert "key=key_alpha" in call_urls[2]


@pytest.mark.asyncio
async def test_gemini_keypool_embedder_all_429_sleep_logic():
    embedder = rewrite_script.GeminiKeyPoolEmbedder(
        keys=["key_only"],
        api_base_url="https://fake-gemini.com",
    )

    class DummyResponse:
        def __init__(self, status_code, json_data, text=""):
            self.status_code = status_code
            self._json = json_data
            self.text = text

        def json(self):
            return self._json

    attempts = 0
    sleeps = []

    async def mock_post(url, json):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return DummyResponse(429, {}, text="429 quota exceeded")
        return DummyResponse(200, {"embeddings": [{"values": [0.9]}]})

    async def mock_sleep(seconds):
        sleeps.append(seconds)

    embedder._client = SimpleNamespace(post=mock_post)

    with patch.object(rewrite_script.asyncio, "sleep", side_effect=mock_sleep):
        res = await embedder.embed_dense(["retry text"])

    assert res == [[0.9]]
    # 1st round all-exhausted slept 60s, 2nd round all-exhausted slept 30s, 3rd attempt succeeded
    assert sleeps == [60, 30]
