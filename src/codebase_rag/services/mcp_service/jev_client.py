"""Jev System One decision client for RAG reranking.

Handles Jev protocol execution, schema validation, KeyPool rotation,
exponential backoff retry on network failures, and dual-key tie-breaking.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

import httpx

from codebase_rag.common.keypool import KeyPool
from codebase_rag.common.logging import get_logger
from codebase_rag.config.schema import LLMProviderConfig, MCPRerankerProviderEntry
from codebase_rag.services.mcp_service.models import (
    CandidatePoint,
    RerankerError,
    RerankerScoreEntry,
)

logger = get_logger(__name__)


def _build_jev_endpoint(base_url: str) -> str:
    """Resolve full Jev POST endpoint from base_url.

    Supports:
      - Vercel AI Gateway: https://ai-gateway.vercel.sh/v4/ai/evaluation-model
      - OpenRouter: https://openrouter.ai/api/alpha/decisions
      - TypeSafe Official: https://api.typesafe.ai/v1/systemone
    """
    url = base_url.strip().rstrip("/")
    if url.endswith("/systemone") or url.endswith("/decisions") or url.endswith("/evaluation-model"):
        return url
    if "vercel" in url:
        return f"{url}/v4/ai/evaluation-model"
    if "openrouter" in url:
        return f"{url}/api/alpha/decisions"
    return f"{url}/v1/systemone"


def _is_vercel_gateway(endpoint: str, headers: Optional[Dict[str, str]] = None) -> bool:
    """Determine whether target endpoint or headers indicate Vercel AI Gateway."""
    if headers and "ai-gateway-protocol-version" in headers:
        return True
    return "vercel" in endpoint or endpoint.endswith("/evaluation-model")


def _build_jev_payload(
    model: str,
    query: str,
    candidate: CandidatePoint,
    is_vercel: bool = False,
) -> dict:
    """Build request payload strictly conforming to Jev API specifications.
    
    - state: structured JSON containing target candidate context (file_path & content).
    - questions: relevance question with type=noul.
    - instructions: natural language guidance indicating context recall reranking.
    - criteria: true/false judgment rubrics.

    For Vercel AI Gateway, the model ID is declared via the 'ai-model-id' header,
    so 'model' is omitted from the request body.
    For OpenRouter and TypeSafe Direct API, 'model' is included in the request body.
    """
    payload = {
        "state": {
            "file_path": candidate.file_path,
            "target_context": candidate.business_description,
        },
        "questions": {
            "relevance": {
                "type": "noul",
                "instructions": (
                    "你现在正在做上下文召回打分判断。上面是待检的目标上下文，"
                    f"用户的检索是：\"{query}\"。"
                    "请判断上面的目标上下文是否与用户的检索高度相关，能够为用户提供直接答案、关键实现逻辑或核心依据。"
                ),
                "criteria": {
                    "true": "目标上下文与用户检索高度契合，直接包含检索所需的核心业务逻辑、接口规则、关键实现细节或直接解答",
                    "false": "目标上下文与用户检索不相关、仅弱相关（如同名变量/偶发字面重合），或属于无关的辅助代码/配置",
                },
            },
        },
    }
    if not is_vercel:
        payload["model"] = model
    return payload


class JevRerankerClient:
    """Asynchronous client for evaluating candidates via Jev decision models."""

    def __init__(self) -> None:
        self._keypools: Dict[str, KeyPool] = {}
        self._keypool_lock = asyncio.Lock()
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        self._sem_lock = asyncio.Lock()
        self._clients: Dict[str, httpx.AsyncClient] = {}
        self._client_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """Close all persistent HTTP clients safely."""
        async with self._client_lock:
            for client in self._clients.values():
                if not client.is_closed:
                    try:
                        await asyncio.wait_for(client.aclose(), timeout=5.0)
                    except Exception:
                        pass
            self._clients.clear()

    async def _get_or_create_semaphore(
        self,
        provider_name: str,
        max_concurrency: int,
    ) -> asyncio.Semaphore:
        async with self._sem_lock:
            if provider_name not in self._semaphores:
                self._semaphores[provider_name] = asyncio.Semaphore(max_concurrency)
            return self._semaphores[provider_name]

    async def _get_or_create_client(
        self,
        provider_name: str,
        max_concurrency: int,
        timeout_seconds: float,
    ) -> httpx.AsyncClient:
        async with self._client_lock:
            if provider_name not in self._clients or self._clients[provider_name].is_closed:
                limits = httpx.Limits(
                    max_connections=max(10, max_concurrency * 2),
                    max_keepalive_connections=max(5, max_concurrency),
                    keepalive_expiry=60.0,
                )
                self._clients[provider_name] = httpx.AsyncClient(
                    limits=limits,
                    timeout=httpx.Timeout(timeout_seconds),
                )
            return self._clients[provider_name]

    async def _get_or_create_keypool(
        self,
        provider_name: str,
        provider_cfg: LLMProviderConfig,
    ) -> KeyPool:
        async with self._keypool_lock:
            keys = [k.key for k in provider_cfg.api_keys if k.key]
            if not keys:
                self._keypools.pop(provider_name, None)
                raise RerankerError(f"Jev provider '{provider_name}' has no valid API keys configured.")
            if provider_name not in self._keypools:
                self._keypools[provider_name] = KeyPool(
                    keys=keys,
                    cooldown_seconds=provider_cfg.retry.key_cooldown_seconds,
                    max_consecutive_failures=provider_cfg.retry.max_retries_per_key,
                )
            else:
                await self._keypools[provider_name].update_keys(
                    keys=keys,
                    cooldown_seconds=provider_cfg.retry.key_cooldown_seconds,
                    max_consecutive_failures=provider_cfg.retry.max_retries_per_key,
                )
            return self._keypools[provider_name]

    async def evaluate_candidates(
        self,
        query: str,
        candidates: List[CandidatePoint],
        entry: MCPRerankerProviderEntry,
        provider_cfg: LLMProviderConfig,
        top_k: int,
    ) -> List[RerankerScoreEntry]:
        """Evaluate and score all candidate points using Jev noul primitive in parallel.
        
        Args:
            query: User's search query string.
            candidates: List of candidate points from Stage 1 recall.
            entry: Reranker provider entry in the priority queue.
            provider_cfg: Configuration of the Jev provider.
            top_k: Target number of top items to return.

        Returns:
            List of RerankerScoreEntry sorted descending by score and vector_score.
        """
        if not candidates:
            return []

        keypool = await self._get_or_create_keypool(entry.provider, provider_cfg)
        endpoint = _build_jev_endpoint(provider_cfg.api_base_url)
        timeout_seconds = float(provider_cfg.request_timeout or 10.0)
        max_concurrency = max(1, provider_cfg.max_concurrency or 15)
        sem = await self._get_or_create_semaphore(entry.provider, max_concurrency)
        client = await self._get_or_create_client(entry.provider, max_concurrency, timeout_seconds)

        # Merge custom headers from model or provider config
        custom_headers: Dict[str, str] = {}
        for m in provider_cfg.models:
            if m.id == entry.model and m.custom_headers:
                custom_headers.update(m.custom_headers)

        is_vercel = _is_vercel_gateway(endpoint, custom_headers)

        # Base headers
        base_headers = {"Content-Type": "application/json"}
        if is_vercel:
            base_headers.update({
                "ai-gateway-protocol-version": "0.0.1",
                "ai-model-id": entry.model,
                "ai-evaluation-model-specification-version": "4",
            })
        base_headers.update(custom_headers)

        scores: List[RerankerScoreEntry] = []
        failures: List[CandidatePoint] = []
        last_error: Optional[Exception] = None

        async def _eval_one(c: CandidatePoint) -> None:
            nonlocal last_error
            payload = _build_jev_payload(entry.model, query, c, is_vercel=is_vercel)
            max_retries = max(1, provider_cfg.retry.max_retries_per_key)
            base_delay = max(0.1, provider_cfg.retry.retry_base_delay)
            multiplier = max(1.0, provider_cfg.retry.retry_backoff_multiplier)

            async with sem:
                for attempt in range(max_retries):
                    key = await keypool.get_key()
                    if not key:
                        err = RerankerError(f"All API keys for Jev provider '{entry.provider}' are exhausted or in cooldown.")
                        last_error = err
                        failures.append(c)
                        return

                    headers = dict(base_headers)
                    headers["Authorization"] = f"Bearer {key}"

                    try:
                        resp = await asyncio.wait_for(
                            client.post(endpoint, json=payload, headers=headers),
                            timeout=timeout_seconds,
                        )
                        status = resp.status_code

                        if status == 200:
                            data = resp.json()
                            answers = data.get("answers", {})
                            rel_entry = answers.get("relevance", {})
                            raw_noul = None
                            if isinstance(rel_entry, dict):
                                raw_noul = rel_entry.get("noul")
                            elif isinstance(rel_entry, (int, float)):
                                raw_noul = rel_entry

                            if raw_noul is not None and isinstance(raw_noul, (int, float)):
                                noul_score = max(0.0, min(1.0, float(raw_noul)))
                                await keypool.report_success(key)
                                scores.append(RerankerScoreEntry(id=c.id, score=noul_score))
                                return
                            else:
                                logger.warning("jev_invalid_noul_response", point_id=c.id, response=data)
                                await keypool.report_failure(key)

                        elif status in (401, 403):
                            logger.warning("jev_key_auth_error", provider=entry.provider, key_suffix=key[-6:], status=status)
                            await keypool.mark_unhealthy(key)
                            continue  # Try next available key immediately

                        elif status == 429:
                            logger.warning("jev_rate_limited", provider=entry.provider, key_suffix=key[-6:], attempt=attempt)
                            retry_after = resp.headers.get("Retry-After")
                            if retry_after and retry_after.isdigit():
                                delay = float(retry_after)
                            else:
                                delay = base_delay * (multiplier ** attempt) * (0.8 + 0.4 * ((attempt + 1) % 3))
                            await asyncio.sleep(delay)
                            continue

                        elif status >= 500:
                            logger.warning("jev_server_error", provider=entry.provider, status=status, attempt=attempt)
                            await keypool.report_failure(key)
                            delay = base_delay * (multiplier ** attempt)
                            await asyncio.sleep(delay)
                            continue

                        else:
                            err_msg = f"Jev HTTP {status}: {resp.text[:200]}"
                            logger.warning("jev_unexpected_status", provider=entry.provider, status=status, error=err_msg)
                            await keypool.report_failure(key)
                            delay = base_delay * (multiplier ** attempt)
                            await asyncio.sleep(delay)
                            continue

                    except (httpx.RequestError, asyncio.TimeoutError, TimeoutError) as e:
                        last_error = e
                        logger.warning(
                            "jev_network_transient_error",
                            provider=entry.provider,
                            key_suffix=key[-6:],
                            error=str(e)[:150],
                            attempt=attempt,
                        )
                        await keypool.report_failure(key)
                        delay = base_delay * (multiplier ** attempt)
                        await asyncio.sleep(delay)
                        continue

                # If all attempts fail for this candidate:
                failures.append(c)

        # Batch hard timeout: allow up to 3x single call timeout, max 45s
        batch_timeout = min(45.0, max(15.0, timeout_seconds * 3.0))
        tasks = [_eval_one(c) for c in candidates]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=False), timeout=batch_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "jev_batch_evaluation_timeout",
                provider=entry.provider,
                candidates=len(candidates),
                completed_scores=len(scores),
                batch_timeout=batch_timeout,
            )

        # Check overall failure rate
        total = len(candidates)
        scored_ids = {s.id for s in scores}
        unscored = [c for c in candidates if c.id not in scored_ids]
        if total > 0 and (len(unscored) / total) > 0.30:
            raise RerankerError(
                f"Jev provider '{entry.provider}' failure rate ({len(unscored)}/{total}, {len(unscored)/total:.1%}) "
                f"exceeded threshold (30%). Last error: {last_error}"
            )

        # For minor transient failures, assign minimum fallback score 0.0
        for fc in unscored:
            scores.append(RerankerScoreEntry(id=fc.id, score=0.0))

        # Dual-key sort: Primary by score descending; Secondary by vector_score descending
        vec_score_map = {c.id: c.vector_score for c in candidates}
        scores.sort(
            key=lambda s: (s.score, vec_score_map.get(s.id, 0.0)),
            reverse=True,
        )

        return scores[:top_k]
