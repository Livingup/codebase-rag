"""Embedding service — dense + sparse vector generation with KeyPool scheduling.

Orchestrates dense (Gemini) and sparse (DashScope) providers with:
- KeyPool round-robin + health tracking per provider
- Two-level retry: L1 single-key retry → L2 key rotation
- Strict validation: texts exceeding batch_size or token limit → error
- Concurrency control via asyncio.Semaphore

This service is a pure infrastructure layer. It does NOT perform any automatic
text splitting or batching. Text chunking is the responsibility of the Agent
and Workflow layers. If input violates provider constraints, an error is raised.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from codebase_rag.common.keypool import KeyPool
from codebase_rag.common.logging import get_logger
from codebase_rag.common.types import EmbeddingResult, SparseVector
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import EmbeddingConfig, EmbeddingProviderConfig
from codebase_rag.services.embedding.providers.base import (
    DenseEmbeddingProvider,
    SparseEmbeddingProvider,
)
from codebase_rag.services.embedding.providers.dashscope import DashScopeSparseProvider
from codebase_rag.services.embedding.providers.gemini import GeminiDenseProvider

logger = get_logger(__name__)


# =============================================================================
# Provider factory
# =============================================================================

_DENSE_PROVIDER_FACTORIES = {
    "gemini": lambda cfg: GeminiDenseProvider(
        api_base_url=cfg.api_base_url or "https://generativelanguage.googleapis.com",
        model_name=cfg.model_name,
        dimension=cfg.dimension,
        batch_size=cfg.batch_size,
        request_timeout=cfg.request_timeout,
    ),
}

_SPARSE_PROVIDER_FACTORIES = {
    "dashscope": lambda cfg: DashScopeSparseProvider(
        api_base_url=cfg.api_base_url or "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
        model_name=cfg.model_name,
        dimension=cfg.dimension or 2048,
        batch_size=cfg.batch_size,
        request_timeout=cfg.request_timeout,
    ),
}


class EmbeddingService:
    """Embedding service providing dense + sparse vector generation.

    Manages provider lifecycle, KeyPool scheduling, retry, and concurrency.

    Args:
        config: Embedding configuration section from AppConfig.
    """

    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        self._dense_provider: Optional[DenseEmbeddingProvider] = None
        self._sparse_provider: Optional[SparseEmbeddingProvider] = None
        self._dense_keypool: Optional[KeyPool] = None
        self._sparse_keypool: Optional[KeyPool] = None
        self._dense_cfg: Optional[EmbeddingProviderConfig] = None
        self._sparse_cfg: Optional[EmbeddingProviderConfig] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._initialized = False

    @property
    def _config(self) -> EmbeddingConfig:
        return self._config_manager.config.embedding

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Initialize providers, KeyPools, and validate connectivity."""
        logger.info("embedding_service_initializing")

        # Concurrency limiter
        self._semaphore = asyncio.Semaphore(self._config.max_concurrency)

        # --- Dense provider ---
        dense_name = self._config.dense.active_provider
        dense_cfg = self._config.dense.providers.get(dense_name)
        if dense_cfg is None:
            raise ValueError(
                f"Dense active_provider '{dense_name}' not found in providers: "
                f"{list(self._config.dense.providers.keys())}"
            )

        factory = _DENSE_PROVIDER_FACTORIES.get(dense_name)
        if factory is None:
            raise ValueError(
                f"No dense provider implementation for '{dense_name}'. "
                f"Available: {list(_DENSE_PROVIDER_FACTORIES.keys())}"
            )
        self._dense_provider = factory(dense_cfg)

        dense_keys = [k.key for k in dense_cfg.api_keys]
        if not dense_keys:
            raise ValueError(f"Dense provider '{dense_name}' has no API keys configured")
        self._dense_cfg = dense_cfg
        self._dense_keypool = KeyPool(
            keys=dense_keys,
            cooldown_seconds=dense_cfg.retry.key_cooldown_seconds,
            max_consecutive_failures=dense_cfg.retry.max_retries_per_key,
        )

        # Validate dense keys
        await self._validate_keys(
            provider_name=f"dense/{dense_name}",
            provider=self._dense_provider,
            keypool=self._dense_keypool,
            keys=dense_keys,
        )

        # --- Sparse provider ---
        sparse_name = self._config.sparse.active_provider
        sparse_cfg = self._config.sparse.providers.get(sparse_name)
        if sparse_cfg is None:
            raise ValueError(
                f"Sparse active_provider '{sparse_name}' not found in providers: "
                f"{list(self._config.sparse.providers.keys())}"
            )

        factory_s = _SPARSE_PROVIDER_FACTORIES.get(sparse_name)
        if factory_s is None:
            raise ValueError(
                f"No sparse provider implementation for '{sparse_name}'. "
                f"Available: {list(_SPARSE_PROVIDER_FACTORIES.keys())}"
            )
        self._sparse_provider = factory_s(sparse_cfg)

        sparse_keys = [k.key for k in sparse_cfg.api_keys]
        if not sparse_keys:
            raise ValueError(f"Sparse provider '{sparse_name}' has no API keys configured")
        self._sparse_cfg = sparse_cfg
        self._sparse_keypool = KeyPool(
            keys=sparse_keys,
            cooldown_seconds=sparse_cfg.retry.key_cooldown_seconds,
            max_consecutive_failures=sparse_cfg.retry.max_retries_per_key,
        )

        # Validate sparse keys
        await self._validate_keys(
            provider_name=f"sparse/{sparse_name}",
            provider=self._sparse_provider,
            keypool=self._sparse_keypool,
            keys=sparse_keys,
        )

        self._initialized = True
        self._config_manager.register_reconciler("embedding", self.reconcile)
        logger.info(
            "embedding_service_initialized",
            dense_provider=dense_name,
            sparse_provider=sparse_name,
            dense_keys=self._dense_keypool.healthy_keys,
            sparse_keys=self._sparse_keypool.healthy_keys,
        )

    async def reconcile(self, old_config: AppConfig, new_config: AppConfig) -> None:
        """Reconcile embedding providers, KeyPools, and concurrency limits."""
        logger.info("embedding_service_reconciling")

        new_emb = new_config.embedding
        old_emb = old_config.embedding

        # 1. Concurrency limit
        if new_emb.max_concurrency != old_emb.max_concurrency or self._semaphore is None:
            self._semaphore = asyncio.Semaphore(new_emb.max_concurrency)

        # 2. Dense provider reconciliation
        dense_name = new_emb.dense.active_provider
        dense_cfg = new_emb.dense.providers.get(dense_name)
        if dense_cfg is not None:
            dense_keys = [k.key for k in dense_cfg.api_keys if k.key]
            if dense_name != old_emb.dense.active_provider or self._dense_provider is None:
                factory = _DENSE_PROVIDER_FACTORIES.get(dense_name)
                if factory:
                    if self._dense_provider and hasattr(self._dense_provider, "close"):
                        await self._dense_provider.close()
                    self._dense_provider = factory(dense_cfg)
                    self._dense_cfg = dense_cfg
                    if dense_keys:
                        self._dense_keypool = KeyPool(
                            keys=dense_keys,
                            cooldown_seconds=dense_cfg.retry.key_cooldown_seconds,
                            max_consecutive_failures=dense_cfg.retry.max_retries_per_key,
                        )
            elif dense_keys and self._dense_keypool:
                await self._dense_keypool.update_keys(
                    keys=dense_keys,
                    cooldown_seconds=dense_cfg.retry.key_cooldown_seconds,
                    max_consecutive_failures=dense_cfg.retry.max_retries_per_key,
                )

        # 3. Sparse provider reconciliation
        sparse_name = new_emb.sparse.active_provider
        sparse_cfg = new_emb.sparse.providers.get(sparse_name)
        if sparse_cfg is not None:
            sparse_keys = [k.key for k in sparse_cfg.api_keys if k.key]
            if sparse_name != old_emb.sparse.active_provider or self._sparse_provider is None:
                factory_s = _SPARSE_PROVIDER_FACTORIES.get(sparse_name)
                if factory_s:
                    if self._sparse_provider and hasattr(self._sparse_provider, "close"):
                        await self._sparse_provider.close()
                    self._sparse_provider = factory_s(sparse_cfg)
                    self._sparse_cfg = sparse_cfg
                    if sparse_keys:
                        self._sparse_keypool = KeyPool(
                            keys=sparse_keys,
                            cooldown_seconds=sparse_cfg.retry.key_cooldown_seconds,
                            max_consecutive_failures=sparse_cfg.retry.max_retries_per_key,
                        )
            elif sparse_keys and self._sparse_keypool:
                await self._sparse_keypool.update_keys(
                    keys=sparse_keys,
                    cooldown_seconds=sparse_cfg.retry.key_cooldown_seconds,
                    max_consecutive_failures=sparse_cfg.retry.max_retries_per_key,
                )

        logger.info(
            "embedding_service_reconciled",
            dense_provider=dense_name,
            sparse_provider=sparse_name,
            dense_healthy_keys=self._dense_keypool.healthy_keys if self._dense_keypool else 0,
            sparse_healthy_keys=self._sparse_keypool.healthy_keys if self._sparse_keypool else 0,
        )

    async def shutdown(self) -> None:
        """Close HTTP clients and release resources."""
        self._config_manager.unregister_reconciler("embedding")
        if self._dense_provider and hasattr(self._dense_provider, "close"):
            await self._dense_provider.close()
        if self._sparse_provider and hasattr(self._sparse_provider, "close"):
            await self._sparse_provider.close()
        self._initialized = False
        logger.info("embedding_service_shutdown")

    def is_healthy(self) -> bool:
        """Check if service is operational."""
        if not self._initialized:
            return False
        if self._dense_keypool and self._dense_keypool.healthy_keys == 0:
            return False
        if self._sparse_keypool and self._sparse_keypool.healthy_keys == 0:
            return False
        return True

    def get_client_status_snapshot(self) -> dict:
        """Return current dense/sparse provider availability without external calls."""
        return {
            "dense": self._build_provider_status(
                kind="dense",
                active_provider=self._config.dense.active_provider,
                cfg=self._dense_cfg,
                keypool=self._dense_keypool,
                initialized=self._initialized and self._dense_provider is not None,
            ),
            "sparse": self._build_provider_status(
                kind="sparse",
                active_provider=self._config.sparse.active_provider,
                cfg=self._sparse_cfg,
                keypool=self._sparse_keypool,
                initialized=self._initialized and self._sparse_provider is not None,
            ),
        }

    async def probe_dense_provider(self, timeout: float) -> dict:
        """Run a real dense embedding provider heartbeat with one available key."""
        return await self._probe_provider(
            provider=self._dense_provider,
            keypool=self._dense_keypool,
            timeout=timeout,
        )

    async def probe_sparse_provider(self, timeout: float) -> dict:
        """Run a real sparse embedding provider heartbeat with one available key."""
        return await self._probe_provider(
            provider=self._sparse_provider,
            keypool=self._sparse_keypool,
            timeout=timeout,
        )

    # =========================================================================
    # Public API
    # =========================================================================

    async def embed_dense(self, texts: List[str]) -> List[List[float]]:
        """Generate dense vectors for a list of texts.

        Raises:
            ValueError: If len(texts) exceeds batch_size or any text exceeds max_tokens_per_text.
        """
        self._check_initialized()
        assert self._dense_provider is not None
        assert self._dense_keypool is not None
        assert self._dense_cfg is not None

        self._validate_batch_size(texts, self._dense_cfg, "dense")
        await self._validate_tokens(texts, self._dense_cfg, "dense")

        return await self._call_with_retry(
            keypool=self._dense_keypool,
            call_fn=lambda key: self._dense_provider.embed_batch(key, texts),
            provider_name="dense",
        )

    async def embed_sparse(
        self, texts: List[str], is_query: bool = False
    ) -> List[SparseVector]:
        """Generate sparse vectors for a list of texts.

        Args:
            texts: Texts to embed.
            is_query: If True, use text_type=query (for search queries).
                     If False, use text_type=document (for indexing).

        Raises:
            ValueError: If len(texts) exceeds batch_size or any text exceeds max_tokens_per_text.
        """
        self._check_initialized()
        assert self._sparse_provider is not None
        assert self._sparse_keypool is not None
        assert self._sparse_cfg is not None

        self._validate_batch_size(texts, self._sparse_cfg, "sparse")

        if is_query and isinstance(self._sparse_provider, DashScopeSparseProvider):
            return await self._call_with_retry(
                keypool=self._sparse_keypool,
                call_fn=lambda key: self._sparse_provider.embed_batch_for_query(key, texts),
                provider_name="sparse",
            )
        return await self._call_with_retry(
            keypool=self._sparse_keypool,
            call_fn=lambda key: self._sparse_provider.embed_batch(key, texts),
            provider_name="sparse",
        )

    async def embed(
        self, texts: List[str], is_query: bool = False
    ) -> List[EmbeddingResult]:
        """Generate both dense and sparse vectors for texts (parallel).

        Args:
            texts: Texts to embed.
            is_query: If True, sparse uses text_type=query.
        """
        dense_task = self.embed_dense(texts)
        sparse_task = self.embed_sparse(texts, is_query=is_query)

        dense_vectors, sparse_vectors = await asyncio.gather(dense_task, sparse_task)

        logger.debug(
            "embed_complete",
            text_count=len(texts),
            is_query=is_query,
            dense_dim=len(dense_vectors[0]) if dense_vectors else 0,
            sparse_nnz=len(sparse_vectors[0].indices) if sparse_vectors else 0,
        )

        return [
            EmbeddingResult(dense_vector=d, sparse_vector=s)
            for d, s in zip(dense_vectors, sparse_vectors)
        ]

    async def count_tokens(self, text: str) -> int:
        """Count tokens using the dense provider's tokenizer.

        Used by upper-layer services to decide chunking strategy.
        """
        self._check_initialized()
        assert self._dense_provider is not None
        assert self._dense_keypool is not None

        return await self._call_with_retry(
            keypool=self._dense_keypool,
            call_fn=lambda key: self._dense_provider.count_tokens(key, text),
            provider_name="dense/count_tokens",
        )

    # =========================================================================
    # Internal helpers
    # =========================================================================

    @staticmethod
    def _provider_status_from_counts(initialized: bool, total_keys: int, healthy_keys: int) -> str:
        if not initialized:
            return "unavailable"
        if total_keys <= 0:
            return "not_configured"
        if healthy_keys <= 0:
            return "unavailable"
        if healthy_keys < total_keys:
            return "degraded"
        return "available"

    def _build_provider_status(
        self,
        kind: str,
        active_provider: str,
        cfg: Optional[EmbeddingProviderConfig],
        keypool: Optional[KeyPool],
        initialized: bool,
    ) -> dict:
        total_keys = keypool.total_keys if keypool else 0
        healthy_keys = keypool.healthy_keys if keypool else 0
        key_status = keypool.get_status() if keypool else []
        return {
            "kind": kind,
            "provider": active_provider,
            "initialized": initialized,
            "status": self._provider_status_from_counts(
                initialized=initialized,
                total_keys=total_keys,
                healthy_keys=healthy_keys,
            ),
            "endpoint": cfg.api_base_url if cfg else None,
            "model": cfg.model_name if cfg else None,
            "dimension": cfg.dimension if cfg else None,
            "batch_size": cfg.batch_size if cfg else None,
            "request_timeout": cfg.request_timeout if cfg else None,
            "total_keys": total_keys,
            "healthy_keys": healthy_keys,
            "key_status": key_status,
        }

    async def _probe_provider(
        self,
        provider: DenseEmbeddingProvider | SparseEmbeddingProvider | None,
        keypool: KeyPool | None,
        timeout: float,
    ) -> dict:
        if not self._initialized or provider is None or keypool is None:
            return {"ok": False, "status": "unavailable", "message": "client is not initialized"}
        api_key = keypool.peek()
        if not api_key:
            return {"ok": False, "status": "not_configured", "message": "no api key configured"}
        started = asyncio.get_running_loop().time()
        try:
            ok = await asyncio.wait_for(provider.health_check(api_key), timeout=timeout)
        except TimeoutError:
            await keypool.report_failure(api_key)
            elapsed = asyncio.get_running_loop().time() - started
            return {
                "ok": False,
                "status": "timeout",
                "message": f"heartbeat timed out after {timeout:g}s",
                "latency_ms": round(elapsed * 1000, 1),
                "key_suffix": api_key[-6:],
            }
        except Exception as e:
            await keypool.report_failure(api_key)
            elapsed = asyncio.get_running_loop().time() - started
            return {
                "ok": False,
                "status": "error",
                "message": str(e),
                "latency_ms": round(elapsed * 1000, 1),
                "key_suffix": api_key[-6:],
            }
        elapsed = asyncio.get_running_loop().time() - started
        
        if ok:
            await keypool.report_success(api_key)
        else:
            await keypool.report_failure(api_key)
            
        return {
            "ok": bool(ok),
            "status": "available" if ok else "unavailable",
            "message": "heartbeat passed" if ok else "heartbeat failed",
            "latency_ms": round(elapsed * 1000, 1),
            "key_suffix": api_key[-6:],
        }

    def _check_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("EmbeddingService is not initialized. Call initialize() first.")

    def _validate_batch_size(
        self, texts: List[str], cfg: EmbeddingProviderConfig, provider_name: str
    ) -> None:
        """Raise ValueError if texts exceed the configured batch_size."""
        if len(texts) > cfg.batch_size:
            raise ValueError(
                f"{provider_name} embedding batch size exceeded: "
                f"got {len(texts)} texts, max {cfg.batch_size}. "
                f"Text chunking is the caller's responsibility."
            )

    async def _validate_tokens(
        self, texts: List[str], cfg: EmbeddingProviderConfig, provider_name: str
    ) -> None:
        """Raise ValueError if any text exceeds max_tokens_per_text.

        Uses the dense provider's countTokens API for validation.
        """
        assert self._dense_provider is not None
        assert self._dense_keypool is not None

        for i, text in enumerate(texts):
            token_count = await self._call_with_retry(
                keypool=self._dense_keypool,
                call_fn=lambda key, t=text: self._dense_provider.count_tokens(key, t),
                provider_name="dense/count_tokens",
            )
            if token_count > cfg.max_tokens_per_text:
                raise ValueError(
                    f"Text[{i}] exceeds max_tokens_per_text: "
                    f"{token_count} tokens > {cfg.max_tokens_per_text} limit. "
                    f"Text must be chunked by the Agent/Workflow layer before embedding."
                )

    async def _validate_keys(
        self,
        provider_name: str,
        provider: DenseEmbeddingProvider | SparseEmbeddingProvider,
        keypool: KeyPool,
        keys: List[str],
    ) -> None:
        """Validate all API keys at startup. Mark unhealthy keys."""
        healthy_count = 0
        for key in keys:
            try:
                ok = await provider.health_check(key)
                if ok:
                    healthy_count += 1
                    logger.info(
                        "key_validated",
                        provider=provider_name,
                        key_suffix=key[-6:],
                        status="ok",
                    )
                else:
                    await keypool.mark_unhealthy(key)
                    logger.warning(
                        "key_validated",
                        provider=provider_name,
                        key_suffix=key[-6:],
                        status="failed",
                    )
            except Exception as e:
                await keypool.mark_unhealthy(key)
                logger.warning(
                    "key_validation_error",
                    provider=provider_name,
                    key_suffix=key[-6:],
                    error=str(e),
                )

        if healthy_count == 0:
            raise RuntimeError(
                f"All API keys for {provider_name} are unhealthy. "
                f"Cannot start embedding service."
            )

        logger.info(
            "provider_keys_validated",
            provider=provider_name,
            healthy=healthy_count,
            total=len(keys),
        )

    async def _call_with_retry(self, keypool: KeyPool, call_fn, provider_name: str):
        """Execute an API call with two-level retry:

        Level 1: Retry on same key (with backoff) — handled by retry loop.
        Level 2: Rotate to next key — handled by KeyPool.

        Uses the global concurrency semaphore.
        """
        assert self._semaphore is not None

        retry_cfg = self._get_retry_config(provider_name)
        max_retries = retry_cfg.max_retries_per_key
        base_delay = retry_cfg.retry_base_delay
        backoff = retry_cfg.retry_backoff_multiplier

        total_attempts = keypool.total_keys * max_retries
        last_error: Optional[Exception] = None

        for attempt in range(total_attempts):
            key = await keypool.get_key()
            if key is None:
                raise RuntimeError(
                    f"No healthy API keys available for {provider_name}"
                )

            try:
                async with self._semaphore:
                    result = await call_fn(key)
                await keypool.report_success(key)
                return result
            except Exception as e:
                last_error = e
                await keypool.report_failure(key)

                # L1: Retry delay with exponential backoff
                delay = base_delay * (backoff ** (attempt % max_retries))
                logger.warning(
                    "embedding_call_retry",
                    provider=provider_name,
                    attempt=attempt + 1,
                    max_attempts=total_attempts,
                    key_suffix=key[-6:],
                    error=str(e),
                    retry_delay=delay,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(
            f"All retry attempts exhausted for {provider_name}: {last_error}"
        )

    def _get_retry_config(self, provider_name: str):
        """Get retry config for the specified provider."""
        if "dense" in provider_name:
            active = self._config.dense.active_provider
            cfg = self._config.dense.providers.get(active)
        else:
            active = self._config.sparse.active_provider
            cfg = self._config.sparse.providers.get(active)

        if cfg:
            return cfg.retry

        # Fallback defaults
        from codebase_rag.config.schema import RetryConfig
        return RetryConfig()
