"""Embedding service — dense vector generation (gemini-embedding-2) with KeyPool scheduling.

Orchestrates dense (Gemini) provider with:
- KeyPool round-robin + health tracking per provider
- Two-level retry: L1 single-key retry → L2 key rotation
- Strict validation: inputs exceeding batch_size or token limit (8192) → error
- Multimodal support: text + optional inline image (.png / .jpg / .jpeg, 258 tokens/image)
- Concurrency control via asyncio.Semaphore

This service is a pure infrastructure layer. It does NOT perform any automatic
text splitting or batching. Text chunking is the responsibility of the Agent
and Workflow layers. If input violates provider constraints, an error is raised.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Sequence

from pythonsdk import ErrorKind, LLMClientError

from codebase_rag.common.keypool import KeyPool
from codebase_rag.common.logging import get_logger
from codebase_rag.common.types import (
    IMAGE_TOKEN_COST,
    MAX_IMAGE_BYTES,
    EmbedInputItem,
    EmbeddingResult,
    MultimodalEmbedInput,
)
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import AppConfig, EmbeddingConfig, EmbeddingProviderConfig
from codebase_rag.services.embedding.providers.base import DenseEmbeddingProvider
from codebase_rag.services.embedding.providers.gemini import GeminiDenseProvider

logger = get_logger(__name__)


# =============================================================================
# Provider factory
# =============================================================================

_DENSE_PROVIDER_FACTORIES = {
    "gemini": lambda cfg: GeminiDenseProvider(
        api_base_url=cfg.api_base_url or "https://generativelanguage.googleapis.com",
        model_name=cfg.model_name or "gemini-embedding-2",
        dimension=cfg.dimension,
        batch_size=cfg.batch_size,
        request_timeout=cfg.request_timeout,
    ),
}


class EmbeddingService:
    """Embedding service providing dense vector generation (text + multimodal image).

    Manages provider lifecycle, KeyPool scheduling, retry, and concurrency.

    Args:
        config_manager: Runtime configuration manager.
    """

    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        self._dense_provider: Optional[DenseEmbeddingProvider] = None
        self._dense_keypool: Optional[KeyPool] = None
        self._dense_cfg: Optional[EmbeddingProviderConfig] = None
        self._dense_semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._initialized = False

    @property
    def _config(self) -> EmbeddingConfig:
        return self._config_manager.config.embedding

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        """Initialize dense provider, KeyPool, and validate connectivity."""
        logger.info("embedding_service_initializing")

        self._dense_semaphore = asyncio.Semaphore(self._config.max_concurrency)
        self._semaphore = self._dense_semaphore

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

        self._initialized = True
        self._config_manager.register_reconciler("embedding", self.reconcile)
        logger.info(
            "embedding_service_initialized",
            dense_provider=dense_name,
            dense_keys=self._dense_keypool.healthy_keys,
        )

    async def reconcile(self, old_config: AppConfig, new_config: AppConfig) -> None:
        """Reconcile embedding provider, KeyPool, and concurrency limits."""
        logger.info("embedding_service_reconciling")

        new_emb = new_config.embedding
        old_emb = old_config.embedding

        # 1. Concurrency limit
        if new_emb.max_concurrency != old_emb.max_concurrency or self._dense_semaphore is None:
            self._dense_semaphore = asyncio.Semaphore(new_emb.max_concurrency)
            self._semaphore = self._dense_semaphore

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
                self._dense_cfg = dense_cfg
                await self._dense_keypool.update_keys(
                    keys=dense_keys,
                    cooldown_seconds=dense_cfg.retry.key_cooldown_seconds,
                    max_consecutive_failures=dense_cfg.retry.max_retries_per_key,
                )

        logger.info(
            "embedding_service_reconciled",
            dense_provider=dense_name,
            dense_healthy_keys=self._dense_keypool.healthy_keys if self._dense_keypool else 0,
        )

    async def shutdown(self) -> None:
        """Close HTTP clients and release resources."""
        self._config_manager.unregister_reconciler("embedding")
        if self._dense_provider and hasattr(self._dense_provider, "close"):
            await self._dense_provider.close()
        self._initialized = False
        logger.info("embedding_service_shutdown")

    def is_healthy(self) -> bool:
        """Check if service is operational."""
        if not self._initialized:
            return False
        if self._dense_keypool and self._dense_keypool.healthy_keys == 0:
            return False
        return True

    def get_client_status_snapshot(self) -> dict:
        """Return current dense provider availability without external calls."""
        return {
            "dense": self._build_provider_status(
                kind="dense",
                active_provider=self._config.dense.active_provider,
                cfg=self._dense_cfg,
                keypool=self._dense_keypool,
                initialized=self._initialized and self._dense_provider is not None,
            ),
        }

    async def probe_dense_provider(self, timeout: float) -> dict:
        """Run a real dense embedding provider heartbeat with one available key."""
        return await self._probe_provider(
            provider=self._dense_provider,
            keypool=self._dense_keypool,
            timeout=timeout,
        )

    # =========================================================================
    # Public API
    # =========================================================================

    async def embed_dense(
        self,
        inputs: Sequence[EmbedInputItem],
        is_query: bool = False,
    ) -> List[List[float]]:
        """Generate dense vectors for a list of text or multimodal inputs.

        Raises:
            ValueError: If len(inputs) exceeds batch_size or any input exceeds max_tokens_per_text.
        """
        self._check_initialized()
        assert self._dense_provider is not None
        assert self._dense_keypool is not None
        assert self._dense_cfg is not None

        self._validate_batch_size(inputs, self._dense_cfg, "dense")
        await self._validate_tokens(inputs, self._dense_cfg, "dense")

        return await self._call_with_retry(
            keypool=self._dense_keypool,
            call_fn=lambda key: self._dense_provider.embed_batch(key, inputs, is_query=is_query),
            provider_name="dense",
        )

    async def embed(
        self,
        inputs: Sequence[EmbedInputItem],
        is_query: bool = False,
    ) -> List[EmbeddingResult]:
        """Generate dense vectors for text or multimodal inputs.

        Args:
            inputs: Texts or MultimodalEmbedInput items to embed.
            is_query: If True, uses taskType=RETRIEVAL_QUERY; otherwise RETRIEVAL_DOCUMENT.
        """
        dense_vectors = await self.embed_dense(inputs, is_query=is_query)

        logger.debug(
            "embed_complete",
            input_count=len(inputs),
            is_query=is_query,
            dense_dim=len(dense_vectors[0]) if dense_vectors else 0,
        )

        return [
            EmbeddingResult(dense_vector=d, sparse_vector=None)
            for d in dense_vectors
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
        provider: DenseEmbeddingProvider | None,
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
        self, inputs: Sequence[EmbedInputItem], cfg: EmbeddingProviderConfig, provider_name: str
    ) -> None:
        """Raise ValueError if inputs exceed the configured batch_size."""
        if len(inputs) > cfg.batch_size:
            raise ValueError(
                f"{provider_name} embedding batch size exceeded: "
                f"got {len(inputs)} items, max {cfg.batch_size}. "
                f"Text chunking is the caller's responsibility."
            )

    async def _validate_tokens(
        self, inputs: Sequence[EmbedInputItem], cfg: EmbeddingProviderConfig, provider_name: str
    ) -> None:
        """Raise ValueError if any input exceeds max_tokens_per_text (including 258 tokens per image).

        Uses the dense provider's countTokens API for validation.
        """
        assert self._dense_provider is not None
        assert self._dense_keypool is not None

        for i, item in enumerate(inputs):
            if isinstance(item, MultimodalEmbedInput):
                text = item.text
                has_image = bool(item.image_bytes)
                if has_image and len(item.image_bytes or b"") > MAX_IMAGE_BYTES:
                    raise ValueError(
                        f"Input[{i}] image exceeds maximum size of {MAX_IMAGE_BYTES} bytes."
                    )
            else:
                text = str(item)
                has_image = False

            token_count = await self._call_with_retry(
                keypool=self._dense_keypool,
                call_fn=lambda key, t=text: self._dense_provider.count_tokens(key, t),
                provider_name="dense/count_tokens",
            )
            total_tokens = token_count + (IMAGE_TOKEN_COST if has_image else 0)
            if total_tokens > cfg.max_tokens_per_text:
                raise ValueError(
                    f"Text[{i}] exceeds max_tokens_per_text: "
                    f"{total_tokens} tokens > {cfg.max_tokens_per_text} limit. "
                    f"Text must be chunked by the Agent/Workflow layer before embedding."
                )

    async def _validate_keys(
        self,
        provider_name: str,
        provider: DenseEmbeddingProvider,
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
        sem = self._dense_semaphore or self._semaphore
        assert sem is not None

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
                async with sem:
                    result = await call_fn(key)
                await keypool.report_success(key)
                return result
            except Exception as e:
                last_error = e
                err_str = str(e)
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                is_rate_limit = (
                    (
                        isinstance(e, LLMClientError)
                        and e.kind in (ErrorKind.RATE_LIMIT, ErrorKind.OVERLOADED)
                    )
                    or status_code == 429
                    or "429" in err_str
                    or "RESOURCE_EXHAUSTED" in err_str
                    or "Too Many Requests" in err_str
                )

                if not is_rate_limit:
                    await keypool.report_failure(key)
                else:
                    logger.warning(
                        "embedding_rate_limit_encountered",
                        provider=provider_name,
                        key_suffix=key[-6:],
                        error=err_str[:120],
                    )

                delay = base_delay * (backoff ** (attempt % max_retries))
                if is_rate_limit:
                    delay = max(delay, 1.5)
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
        """Get retry config for the dense provider."""
        active = self._config.dense.active_provider
        cfg = self._config.dense.providers.get(active)
        if cfg:
            return cfg.retry

        from codebase_rag.config.schema import RetryConfig
        return RetryConfig()

