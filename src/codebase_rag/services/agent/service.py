"""Agent Service — generic Agent runtime platform.

Manages LLM providers with KeyPool scheduling, Agent definition registry,
and execution loop with tool calling support. No business logic — only
"how to run an Agent".
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Callable, Coroutine, Dict, List, Optional, Union

from codebase_rag.common.keypool import KeyPool
from codebase_rag.common.logging import get_logger
from codebase_rag.common.token_counter import TokenCounter
from codebase_rag.config.loader import ConfigManager
from codebase_rag.config.schema import AgentConfig, LLMProviderConfig, ProviderModelRef
from codebase_rag.services.agent.errors import (
    AgentNotFoundError,
    AgentTaskComplete,
    AgentTimeoutError,
    LLMAllKeysExhaustedError,
    LLMAPIError,
    LLMAuthError,
    LLMRateLimitError,
    StructuredOutputError,
    ToolHandlerError,
)
from codebase_rag.services.agent.models import (
    AgentDefinition,
    AgentInfo,
    AgentInput,
    AgentResult,
    LLMResponse,
    Message,
    TokenEstimate,
    TokenUsage,
    ToolCallRecord,
    ToolSchema,
)
from codebase_rag.services.agent.context_compression import (
    COMPRESSION_SYSTEM_PROMPT,
    COMPRESSION_TOOL,
)
from codebase_rag.services.agent.providers.base import LLMProvider
from codebase_rag.services.agent.trace_logger import AgentTraceLogger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Provider factory — lazy imports to avoid loading heavy SDK deps at startup
# ---------------------------------------------------------------------------

_SUPPORTED_FORMATS = ("gemini", "openai_chat", "openai_responses", "anthropic", "zhipu", "deepseek")

# Mutable dict for test monkey-patching (e.g. registering a mock format)
_FORMAT_FACTORIES: Dict[str, type] = {}


def _resolve_provider_class(api_format: str) -> type:
    """Lazy-import provider class by api_format."""
    if api_format == "gemini":
        from codebase_rag.services.agent.providers.gemini import GeminiLLMProvider
        return GeminiLLMProvider
    if api_format == "openai_chat":
        from codebase_rag.services.agent.providers.openai_chat import OpenAIChatProvider
        return OpenAIChatProvider
    if api_format == "openai_responses":
        from codebase_rag.services.agent.providers.openai_responses import OpenAIResponsesProvider
        return OpenAIResponsesProvider
    if api_format == "anthropic":
        from codebase_rag.services.agent.providers.anthropic import AnthropicProvider
        return AnthropicProvider
    if api_format == "zhipu":
        from codebase_rag.services.agent.providers.zhipu import ZhipuProvider
        return ZhipuProvider
    if api_format == "deepseek":
        from codebase_rag.services.agent.providers.deepseek import DeepSeekProvider
        return DeepSeekProvider
    raise ValueError(f"Unknown api_format: '{api_format}'. Supported: {_SUPPORTED_FORMATS}")


def _create_provider(name: str, cfg: LLMProviderConfig) -> LLMProvider:
    api_format = cfg.api_format
    # Check monkey-patched factories first (for tests)
    if api_format in _FORMAT_FACTORIES:
        factory = _FORMAT_FACTORIES[api_format]
        return factory(api_base_url=cfg.api_base_url)
    factory = _resolve_provider_class(api_format)
    return factory(api_base_url=cfg.api_base_url, request_timeout=cfg.request_timeout)


class AgentService:
    """Generic Agent runtime platform.

    - Multi-provider LLM management with KeyPool
    - Agent definition registry
    - Execution loop with tool calling (local + MCP)
    - Batch execution with concurrency control
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        mcp_client: Optional["McpClientService"] = None,
        token_counter: Optional[TokenCounter] = None,
    ) -> None:
        self._config_manager = config_manager
        self._mcp_client = mcp_client
        self._token_counter = token_counter or TokenCounter()
        self._providers: Dict[str, LLMProvider] = {}
        self._keypools: Dict[str, KeyPool] = {}
        self._agents: Dict[str, AgentDefinition] = {}
        self._agent_lock = asyncio.Lock()
        # 每个 LLM 服务商的并发请求信号量（key=provider名称）
        self._provider_sems: Dict[str, asyncio.Semaphore] = {}
        # 当任意服务商释放并发槽位时触发，用于唤醒等待分配服务商的请求
        self._slot_freed: asyncio.Event = asyncio.Event()
        self._initialized = False

    @property
    def _config(self) -> AgentConfig:
        return self._config_manager.config.agent

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self) -> None:
        logger.info("agent_service_initializing")

        llm_cfg = self._config.llm

        # Initialize providers and keypools
        for pname, pcfg in llm_cfg.providers.items():
            self._providers[pname] = _create_provider(pname, pcfg)

            keys = [k.key for k in pcfg.api_keys if k.key]
            if keys:
                self._keypools[pname] = KeyPool(
                    keys=keys,
                    cooldown_seconds=pcfg.retry.key_cooldown_seconds,
                    max_consecutive_failures=pcfg.retry.max_retries_per_key,
                )

            self._provider_sems[pname] = asyncio.Semaphore(pcfg.max_concurrency)
            logger.info(
                "provider_concurrency_configured",
                provider=pname,
                max_concurrency=pcfg.max_concurrency,
            )

        self._initialized = True
        logger.info(
            "agent_service_initialized",
            providers=list(self._providers.keys()),
            default_provider=llm_cfg.default_provider,
            default_model=llm_cfg.default_model,
        )

    async def shutdown(self) -> None:
        self._providers.clear()
        self._keypools.clear()
        self._agents.clear()
        self._initialized = False
        logger.info("agent_service_shutdown")

    def is_healthy(self) -> bool:
        if not self._initialized:
            return False
        return any(
            kp.healthy_keys > 0 for kp in self._keypools.values()
        )

    def get_llm_provider_status_snapshot(self) -> List[Dict[str, object]]:
        """Return configured LLM provider availability without external calls."""
        llm_cfg = self._config.llm
        items: List[Dict[str, object]] = []
        for provider_name, provider_cfg in llm_cfg.providers.items():
            keypool = self._keypools.get(provider_name)
            total_keys = keypool.total_keys if keypool else len([k for k in provider_cfg.api_keys if k.key])
            healthy_keys = keypool.healthy_keys if keypool else 0
            provider_loaded = provider_name in self._providers
            status = self._provider_status_from_counts(
                initialized=self._initialized and provider_loaded,
                total_keys=total_keys,
                healthy_keys=healthy_keys,
            )

            model_refs = [
                {
                    "id": ref.id,
                    "alias": ref.alias,
                    "api_model": ref.alias or ref.id,
                    "tool_choice": ref.tool_choice,
                    "thinking_configured": bool(ref.thinking),
                    "custom_headers_configured": bool(ref.custom_headers),
                    "is_default": provider_name == llm_cfg.default_provider and ref.id == llm_cfg.default_model,
                }
                for ref in provider_cfg.models
            ]
            items.append(
                {
                    "id": provider_name,
                    "name": provider_name,
                    "status": status,
                    "api_format": provider_cfg.api_format,
                    "endpoint": provider_cfg.api_base_url,
                    "models": model_refs,
                    "default_provider": llm_cfg.default_provider,
                    "default_model": llm_cfg.default_model,
                    "is_default_provider": provider_name == llm_cfg.default_provider,
                    "total_keys": total_keys,
                    "healthy_keys": healthy_keys,
                    "key_status": keypool.get_status() if keypool else [],
                    "request_timeout": provider_cfg.request_timeout,
                    "max_concurrency": provider_cfg.max_concurrency,
                    "initialized": self._initialized,
                    "provider_loaded": provider_loaded,
                }
            )
        return items

    async def probe_llm_provider(self, provider_name: str, timeout: float) -> Dict[str, object]:
        """Probe one LLM provider by sending a minimal chat request to a configured model."""
        provider = self._providers.get(provider_name)
        provider_cfg = self._config.llm.providers.get(provider_name)
        keypool = self._keypools.get(provider_name)
        if not self._initialized or provider is None or provider_cfg is None or keypool is None:
            return {"ok": False, "status": "not_configured", "message": "LLM provider is not configured or initialized"}
        if not provider_cfg.models:
            return {"ok": False, "status": "not_configured", "message": "LLM provider has no models configured"}

        model_ref = self._select_probe_model_ref(provider_name, provider_cfg)
        model_id = model_ref.id
        api_model = model_ref.alias or model_ref.id
        api_key = await keypool.get_key()
        if not api_key:
            return {"ok": False, "status": "unavailable", "message": "No healthy API key is currently available"}

        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                provider.chat_completion(
                    api_key=api_key,
                    messages=[Message(role="user", content="ping")],
                    model=api_model,
                    temperature=0.0,
                    max_output_tokens=1,
                    tool_choice="none",
                    thinking_config=None,
                    custom_headers=model_ref.custom_headers,
                ),
                timeout=timeout,
            )
            await keypool.report_success(api_key)
            return {
                "ok": True,
                "status": "available",
                "message": "Minimal LLM chat request succeeded",
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "model": model_id,
                "api_model": api_model,
                "finish_reason": response.finish_reason,
                "response_received": bool(response.content or response.thinking_content or response.tool_calls),
            }
        except asyncio.TimeoutError:
            await keypool.report_failure(api_key)
            return {
                "ok": False,
                "status": "timeout",
                "message": f"Minimal LLM chat request timed out after {timeout:g}s",
                "model": model_id,
                "api_model": api_model,
            }
        except LLMAuthError as exc:
            await keypool.mark_unhealthy(api_key)
            return {
                "ok": False,
                "status": "unavailable",
                "message": f"LLM authentication failed: {str(exc)[:160]}",
                "model": model_id,
                "api_model": api_model,
            }
        except LLMRateLimitError as exc:
            return {
                "ok": False,
                "status": "degraded",
                "message": f"LLM rate limit or quota response: {str(exc)[:160]}",
                "model": model_id,
                "api_model": api_model,
            }
        except Exception as exc:
            await keypool.report_failure(api_key)
            return {
                "ok": False,
                "status": "error",
                "message": f"Minimal LLM chat request failed: {str(exc)[:160]}",
                "model": model_id,
                "api_model": api_model,
            }

    async def probe_llm_providers(self, timeout: float) -> Dict[str, Dict[str, object]]:
        """Probe all configured LLM providers with one minimal model request each."""
        provider_names = list(self._config.llm.providers.keys())
        results = await asyncio.gather(
            *(self.probe_llm_provider(provider_name, timeout) for provider_name in provider_names),
        )
        return dict(zip(provider_names, results))

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

    def _select_probe_model_ref(self, provider_name: str, provider_cfg: LLMProviderConfig) -> ProviderModelRef:
        default_model = self._config.llm.default_model
        if provider_name == self._config.llm.default_provider:
            for model_ref in provider_cfg.models:
                if model_ref.id == default_model:
                    return model_ref
        return provider_cfg.models[0]

    # =========================================================================
    # Provider 并发容量选择
    # =========================================================================

    async def select_provider(self, provider_names: List[str]) -> str:
        """按优先级选择有并发容量的 LLM 服务商，全满时等待直到有空闲。

        用于 agent 启动前选定服务商，确保整个 agent 多轮对话生命周期
        绑定同一服务商。优先选择高优先级（列表靠前）的服务商；当所有
        服务商并发槽位都满时，阻塞等待直到任意服务商释放槽位后重试。

        Args:
            provider_names: 按优先级排序的服务商名称列表（索引越小优先级越高）

        Returns:
            选中的服务商名称
        """
        self._check_initialized()

        while True:
            for name in provider_names:
                sem = self._provider_sems.get(name)
                if sem and not sem.locked():
                    logger.debug(
                        "provider_selected",
                        provider=name,
                    )
                    return name
            # 所有服务商并发槽位都满，等待任意服务商释放
            logger.debug(
                "all_providers_at_capacity",
                providers=provider_names,
            )
            self._slot_freed.clear()
            await self._slot_freed.wait()

    # =========================================================================
    # 2.1 Agent definition registration
    # =========================================================================

    async def register_agent(self, agent_def: AgentDefinition) -> bool:
        """Register an Agent definition. Overwrites if same name exists."""
        self._check_initialized()

        # Resolve provider + model
        provider = agent_def.provider or self._config.llm.default_provider
        model = agent_def.model or self._config.llm.default_model

        # Validate provider exists
        if provider not in self._providers:
            raise ValueError(
                f"Provider '{provider}' not registered. "
                f"Available: {list(self._providers.keys())}"
            )

        # Validate model exists in provider's model list
        pcfg = self._config.llm.providers.get(provider)
        if pcfg and pcfg.models:
            model_ids = {ref.id for ref in pcfg.models}
            if model not in model_ids:
                raise ValueError(
                    f"Model '{model}' not found in provider '{provider}'. "
                    f"Available: {model_ids}"
                )

        async with self._agent_lock:
            self._agents[agent_def.name] = agent_def

        logger.info(
            "agent_registered",
            name=agent_def.name,
            provider=provider,
            model=model,
        )
        return True

    # =========================================================================
    # 2.2 Query registered agents
    # =========================================================================

    async def list_agents(self) -> List[AgentInfo]:
        return [
            AgentInfo(
                name=ad.name,
                description=ad.description,
                provider=ad.provider or self._config.llm.default_provider,
                model=ad.model or self._config.llm.default_model,
            )
            for ad in self._agents.values()
        ]

    async def get_agent(self, name: str) -> Optional[AgentDefinition]:
        return self._agents.get(name)

    # =========================================================================
    # 2.3 Run Agent (core)
    # =========================================================================

    async def run_agent(
        self,
        agent_name: str,
        input_variables: Dict[str, str],
        tool_handlers: Optional[Dict[str, Callable[..., Coroutine]]] = None,
        user_message: Optional[str] = None,
    ) -> AgentResult:
        """Run a registered Agent through the execution loop."""
        self._check_initialized()

        agent_def = self._agents.get(agent_name)
        if not agent_def:
            raise AgentNotFoundError(f"Agent '{agent_name}' not registered")

        provider_name = agent_def.provider or self._config.llm.default_provider
        model = agent_def.model or self._config.llm.default_model
        temperature = agent_def.temperature if agent_def.temperature is not None else self._config.execution.default_temperature
        timeout = agent_def.timeout or self._config.execution.default_timeout

        # Render system prompt
        system_prompt = agent_def.system_prompt
        for key, value in input_variables.items():
            system_prompt = system_prompt.replace(f"{{{key}}}", value)

        # Build initial messages
        first_user_msg = user_message if user_message else "Please proceed with the analysis."
        messages: List[Message] = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=first_user_msg),
        ]

        # Merge local tools + MCP tools
        all_tools = list(agent_def.tools or [])
        mcp_tool_map: Dict[str, str] = {}  # tool_name → server_name

        if agent_def.mcp_servers and self._mcp_client:
            for server_name, include_tools in agent_def.mcp_servers.items():
                try:
                    mcp_tools = await self._mcp_client.list_tools(server_name)
                except Exception as e:
                    logger.warning(
                        "mcp_tool_discovery_failed",
                        agent=agent_name,
                        server=server_name,
                        error=str(e)[:200],
                    )
                    continue
                whitelist = set(include_tools) if include_tools else None
                local_names = {t.name for t in all_tools}
                for tool_info in mcp_tools:
                    if whitelist and tool_info.name not in whitelist:
                        continue
                    if tool_info.name in local_names:
                        logger.debug(
                            "mcp_tool_shadowed_by_local",
                            tool=tool_info.name,
                            server=server_name,
                        )
                        continue
                    all_tools.append(ToolSchema(
                        name=tool_info.name,
                        description=tool_info.description,
                        parameters=tool_info.parameters,
                    ))
                    mcp_tool_map[tool_info.name] = server_name

        result = AgentResult()
        start = time.monotonic()
        session_id = self._uuid7()

        # --- Trace logger (file-level execution log) ---
        trace: Optional[AgentTraceLogger] = None
        trace_dir = self._config.execution.trace_log_dir
        if trace_dir:
            try:
                trace = AgentTraceLogger(trace_dir, agent_name, session_id)
                trace.log_header(
                    agent_def, provider_name, model,
                    temperature, timeout, all_tools,
                )
                for msg in messages:
                    trace.log_message(msg, 0)
                logger.info("agent_trace_started", path=trace.path)
            except Exception as e:
                logger.warning("agent_trace_init_failed", error=str(e)[:200])
                trace = None

        last_prompt_tokens = 0
        round_num = 0
        while True:
            round_num += 1
            # Timeout check
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise AgentTimeoutError(
                    f"Agent '{agent_name}' timed out after {elapsed:.1f}s "
                    f"(limit: {timeout}s)"
                )

            # Context compression check (skip first round — no token data yet)
            if (
                agent_def.enable_context_compression
                and last_prompt_tokens > 0
            ):
                model_meta = self._config.llm.models.get(model)
                threshold = model_meta.compression_threshold if model_meta else None
                if threshold and last_prompt_tokens >= threshold:
                    logger.info(
                        "context_compression_triggered",
                        agent=agent_name,
                        prompt_tokens=last_prompt_tokens,
                        threshold=threshold,
                        round=round_num,
                    )
                    await self._compress_context(
                        messages, agent_name, trace, round_num,
                    )

            # LLM call with KeyPool + retry
            llm_resp = await self._llm_call_with_retry(
                provider_name, model, messages,
                tools=all_tools if all_tools else None,
                output_schema=agent_def.output_schema,
                temperature=temperature,
                session_id=session_id,
                prompt_cache_key=session_id,
                agent_name=agent_name,
                max_rate_limit_retries=agent_def.max_rate_limit_retries,
            )

            last_prompt_tokens = llm_resp.usage.prompt_tokens
            result.total_tokens = result.total_tokens + llm_resp.usage
            result.rounds = round_num

            if llm_resp.finish_reason == "stop" or llm_resp.finish_reason == "length":
                result.content = llm_resp.content
                result.structured_output = llm_resp.structured_output
                # Log final assistant message
                if trace:
                    trace.log_message(
                        Message(role="assistant", content=llm_resp.content),
                        round_num,
                    )
                if llm_resp.finish_reason == "length":
                    logger.warning(
                        "agent_output_truncated",
                        agent=agent_name,
                        round=round_num,
                    )
                break

            if llm_resp.finish_reason == "tool_calls" and llm_resp.tool_calls:
                # Execute tool calls — preserve thinking content for multi-turn transparency
                assistant_msg = Message(
                    role="assistant",
                    content=llm_resp.content,
                    thinking_content=llm_resp.thinking_content,
                    tool_calls=llm_resp.tool_calls,
                )
                messages.append(assistant_msg)
                if trace:
                    trace.log_message(assistant_msg, round_num)

                early_exit = False
                for tc in llm_resp.tool_calls:
                    try:
                        tool_result = await self._execute_tool(
                            tc.name, tc.arguments, tool_handlers,
                            round_num, result, mcp_tool_map,
                        )
                    except AgentTaskComplete:
                        logger.info(
                            "agent_task_complete_signal",
                            agent=agent_name,
                            round=round_num,
                            tool=tc.name,
                        )
                        early_exit = True
                        break
                    tool_msg = Message(
                        role="tool",
                        content=tool_result,
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                    messages.append(tool_msg)
                    if trace:
                        trace.log_message(tool_msg, round_num)
                if early_exit:
                    break
            else:
                # Unexpected finish reason → treat as final
                result.content = llm_resp.content
                result.structured_output = llm_resp.structured_output
                break
        if trace:
            trace.log_finish(
                reason="completed",
                rounds=result.rounds,
                total_tokens=result.total_tokens,
            )
        logger.info(
            "agent_run_completed",
            agent=agent_name,
            rounds=result.rounds,
            total_tokens=result.total_tokens.total_tokens,
            elapsed_ms=round((time.monotonic() - start) * 1000, 1),
        )
        return result

    # =========================================================================
    # 2.4 Batch run
    # =========================================================================

    async def batch_run_agent(
        self,
        agent_name: str,
        batch_inputs: List[AgentInput],
    ) -> List[Union[AgentResult, Exception]]:
        """Run agent on multiple inputs concurrently. Partial failures safe."""
        self._check_initialized()
        results: List[Union[AgentResult, Exception]] = [None] * len(batch_inputs)  # type: ignore

        async def _run_one(idx: int, inp: AgentInput) -> None:
            try:
                r = await self.run_agent(
                    agent_name, inp.input_variables, inp.tool_handlers
                )
                results[idx] = r
            except Exception as e:
                results[idx] = e

        tasks = [_run_one(i, inp) for i, inp in enumerate(batch_inputs)]
        await asyncio.gather(*tasks)

        succeeded = sum(1 for r in results if isinstance(r, AgentResult))
        logger.info(
            "batch_run_agent",
            agent=agent_name,
            total=len(batch_inputs),
            succeeded=succeeded,
            failed=len(batch_inputs) - succeeded,
        )
        return results

    # =========================================================================
    # 2.5 Simple LLM call (no agent definition)
    # =========================================================================

    async def llm_call(
        self,
        messages: List[Message],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[List[ToolSchema]] = None,
        output_schema: Optional[Dict] = None,
        temperature: float = 0.0,
        session_id: Optional[str] = None,
        prompt_cache_key: Optional[str] = None,
    ) -> LLMResponse:
        """Direct LLM call without Agent definition or execution loop."""
        self._check_initialized()

        provider_name = provider or self._config.llm.default_provider
        model_name = model or self._config.llm.default_model

        return await self._llm_call_with_retry(
            provider_name, model_name, messages,
            tools=tools,
            output_schema=output_schema,
            temperature=temperature,
            session_id=session_id,
            prompt_cache_key=prompt_cache_key,
            agent_name="llm_call_direct",
        )

    # =========================================================================
    # 2.6 Token counting
    # =========================================================================

    async def count_tokens(
        self,
        text: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> int:
        """Count tokens for text.

        Fallback chain:
          1. LLM Provider SDK API (Gemini/Anthropic remote, OpenAI/ZhiPu tiktoken local)
          2. Embedding Service count_tokens (Gemini countTokens API)
          3. Local estimate (len // 4)
        """
        provider_name = provider or self._config.llm.default_provider
        model_name = model or self._config.llm.default_model

        # Tier 1: Provider SDK
        prov = self._providers.get(provider_name)
        if prov:
            api_key = ""
            kp = self._keypools.get(provider_name)
            if kp:
                api_key = kp.peek()
            try:
                return await prov.count_tokens(text, model_name, api_key)
            except Exception:
                logger.debug(
                    "count_tokens_provider_fallback",
                    provider=provider_name,
                )

        # Tier 2/3: TokenCounter (Embedding API → local estimate)
        return await self._token_counter.count(text)

    # =========================================================================
    # 2.7 Estimate prompt tokens
    # =========================================================================

    async def estimate_prompt_tokens(
        self,
        agent_name: str,
        input_variables: Dict[str, str],
    ) -> TokenEstimate:
        """Estimate token consumption before running an Agent."""
        agent_def = self._agents.get(agent_name)
        if not agent_def:
            raise AgentNotFoundError(f"Agent '{agent_name}' not registered")

        provider_name = agent_def.provider or self._config.llm.default_provider
        model = agent_def.model or self._config.llm.default_model

        # Render system prompt
        system_prompt = agent_def.system_prompt
        for key, value in input_variables.items():
            system_prompt = system_prompt.replace(f"{{{key}}}", value)

        sys_tokens = await self.count_tokens(system_prompt, provider_name, model)

        # User input tokens (template variables combined)
        user_text = " ".join(input_variables.values())
        user_tokens = await self.count_tokens(user_text, provider_name, model)

        # Tool schema tokens
        tool_tokens = 0
        if agent_def.tools:
            tool_text = json.dumps([
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in agent_def.tools
            ])
            tool_tokens = await self.count_tokens(tool_text, provider_name, model)

        total = sys_tokens + tool_tokens

        # Get context window from model metadata registry
        context_window = 128000  # default
        model_meta = self._config.llm.models.get(model)
        if model_meta:
            context_window = model_meta.context_window

        return TokenEstimate(
            system_prompt_tokens=sys_tokens,
            user_input_tokens=user_tokens,
            tool_schema_tokens=tool_tokens,
            total_tokens=total,
            remaining_for_output=context_window - total,
        )

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _compress_context(
        self,
        messages: List[Message],
        agent_name: str,
        trace: Optional[AgentTraceLogger] = None,
        round_num: int = 0,
    ) -> bool:
        """Compress conversation history in-place when token threshold is exceeded.

        Finds the last assistant message, sends everything before it to the
        compression agent, and rebuilds the messages list as:
          [system_prompt, compressed_user_msg, last_assistant_msg, ...]

        Returns True if compression was performed successfully.
        """
        # Find last assistant message index
        last_asst_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "assistant":
                last_asst_idx = i
                break

        if last_asst_idx is None or last_asst_idx <= 1:
            # No assistant message yet or too few messages — nothing to compress
            return False

        # Build compression agent messages:
        # [compression system prompt] + original messages[1:last_asst_idx]
        base_compression_messages: List[Message] = [
            Message(role="system", content=COMPRESSION_SYSTEM_PROMPT),
        ] + list(messages[1:last_asst_idx])

        # Resolve compression agent LLM config
        comp_cfg = self._config.context_compression
        compression_candidates = (
            [
                (entry.provider, entry.model)
                for entry in sorted(comp_cfg.providers, key=lambda e: e.priority)
            ]
            if comp_cfg.providers else [
                (
                    comp_cfg.provider or self._config.llm.default_provider,
                    comp_cfg.model or self._config.llm.default_model,
                )
            ]
        )

        # Simplified execution loop for compression agent (max 3 rounds)
        max_comp_rounds = 3
        last_error: Optional[Exception] = None
        for provider_idx, (comp_provider, comp_model) in enumerate(compression_candidates):
            compression_messages = list(base_compression_messages)
            logger.info(
                "context_compression_started",
                agent=agent_name,
                provider=comp_provider,
                model=comp_model,
                provider_index=provider_idx,
                messages_to_compress=last_asst_idx - 1,
                messages_to_keep=len(messages) - last_asst_idx,
            )
            for comp_round in range(max_comp_rounds):
                try:
                    llm_resp = await self._llm_call_with_retry(
                        comp_provider,
                        comp_model,
                        compression_messages,
                        tools=[COMPRESSION_TOOL],
                        temperature=comp_cfg.temperature,
                        agent_name=f"{agent_name}__context_compression",
                    )
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "context_compression_provider_failed",
                        agent=agent_name,
                        provider=comp_provider,
                        model=comp_model,
                        round=comp_round,
                        error=str(e)[:200],
                    )
                    break

                # Case 1: LLM called the compression tool — extract result
                if llm_resp.finish_reason == "tool_calls" and llm_resp.tool_calls:
                    for tc in llm_resp.tool_calls:
                        if tc.name == "compress_and_replace_history":
                            compressed = tc.arguments.get("compressed_context", "")
                            if compressed:
                                # Rebuild messages in-place
                                system_msg = messages[0]
                                remaining = list(messages[last_asst_idx:])
                                messages.clear()
                                messages.append(system_msg)
                                messages.append(Message(role="user", content=compressed))
                                messages.extend(remaining)

                                if trace:
                                    trace.log_message(
                                        Message(
                                            role="system",
                                            content=f"[context_compressed] compressed_len={len(compressed)}, new_message_count={len(messages)}",
                                        ),
                                        round_num,
                                    )

                                logger.info(
                                    "context_compression_completed",
                                    agent=agent_name,
                                    provider=comp_provider,
                                    model=comp_model,
                                    compressed_len=len(compressed),
                                    new_message_count=len(messages),
                                )
                                return True

                    # Tool called but wrong name or empty result — prompt retry
                    compression_messages.append(Message(
                        role="assistant",
                        content=llm_resp.content,
                        thinking_content=llm_resp.thinking_content,
                        tool_calls=llm_resp.tool_calls,
                    ))
                    compression_messages.append(Message(
                        role="user",
                        content=(
                            "请调用 compress_and_replace_history 工具，"
                            "并在 compressed_context 中给出压缩后的上下文。"
                        ),
                    ))
                    continue

                # Case 2: LLM responded with text (no tool call)
                elif llm_resp.finish_reason == "stop" and llm_resp.content:
                    # Some models may put the compressed content in text instead of tool call.
                    # If content is substantial, use it as fallback.
                    if len(llm_resp.content) > 200:
                        system_msg = messages[0]
                        remaining = list(messages[last_asst_idx:])
                        messages.clear()
                        messages.append(system_msg)
                        messages.append(Message(role="user", content=llm_resp.content))
                        messages.extend(remaining)

                        logger.warning(
                            "context_compression_fallback_text",
                            agent=agent_name,
                            provider=comp_provider,
                            model=comp_model,
                            compressed_len=len(llm_resp.content),
                        )
                        return True

                    logger.warning(
                        "context_compression_no_tool_call",
                        agent=agent_name,
                        provider=comp_provider,
                        model=comp_model,
                        content_len=len(llm_resp.content),
                    )
                    break
                else:
                    logger.warning(
                        "context_compression_unexpected_finish",
                        agent=agent_name,
                        provider=comp_provider,
                        model=comp_model,
                        finish_reason=llm_resp.finish_reason,
                    )
                    break

            logger.warning(
                "context_compression_provider_exhausted",
                agent=agent_name,
                provider=comp_provider,
                model=comp_model,
            )

        logger.error(
            "context_compression_all_providers_exhausted",
            agent=agent_name,
            last_error=str(last_error)[:200] if last_error else None,
        )
        return False

    def _check_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "AgentService is not initialized. Call initialize() first."
            )

    @staticmethod
    def _uuid7() -> str:
        """Generate a UUID v7 (RFC 9562) — time-ordered, suitable for prompt_cache_key."""
        timestamp_ms = int(time.time() * 1000)
        rand_bytes = os.urandom(10)
        uuid_int = (timestamp_ms & 0xFFFFFFFFFFFF) << 80
        uuid_int |= 0x7000 << 64  # version 7
        uuid_int |= (rand_bytes[0] & 0x3F) << 72 | rand_bytes[1] << 64
        uuid_int |= 0x80 << 56  # variant 10xx
        uuid_int |= (rand_bytes[2] & 0x3F) << 48
        for i in range(3, 10):
            uuid_int |= rand_bytes[i] << ((9 - i) * 8)
        return str(uuid.UUID(int=uuid_int))

    async def _llm_call_with_retry(
        self,
        provider_name: str,
        model: str,
        messages: List[Message],
        tools: Optional[List[ToolSchema]] = None,
        output_schema: Optional[Dict] = None,
        temperature: float = 0.0,
        session_id: Optional[str] = None,
        prompt_cache_key: Optional[str] = None,
        agent_name: Optional[str] = None,
        max_rate_limit_retries: int = -1,
    ) -> LLMResponse:
        """LLM call with KeyPool rotation + retry."""
        provider = self._providers.get(provider_name)
        if not provider:
            raise ValueError(f"Provider '{provider_name}' not available")

        keypool = self._keypools.get(provider_name)
        if not keypool:
            raise LLMAllKeysExhaustedError(
                f"No KeyPool for provider '{provider_name}'"
            )

        pcfg = self._config.llm.providers.get(provider_name)
        retry_cfg = pcfg.retry if pcfg else None
        max_retries = retry_cfg.max_retries_per_key if retry_cfg else 3
        base_delay = retry_cfg.retry_base_delay if retry_cfg else 1.0
        backoff = retry_cfg.retry_backoff_multiplier if retry_cfg else 2.0

        # Get max_output_tokens from model metadata registry
        max_output_tokens = None
        model_meta = self._config.llm.models.get(model)
        if model_meta:
            max_output_tokens = model_meta.max_output_tokens

        # Resolve model ref: alias, tool_choice, thinking, custom_headers
        api_model = model
        tool_choice = None
        thinking_config = None
        custom_headers = None
        if pcfg and pcfg.models:
            for ref in pcfg.models:
                if ref.id == model:
                    api_model = ref.alias or ref.id
                    tool_choice = ref.tool_choice
                    if ref.thinking:
                        thinking_config = json.loads(ref.thinking)
                    custom_headers = ref.custom_headers
                    break

        provider_sem = self._provider_sems.get(provider_name)

        # Try all keys via rotation
        last_error: Optional[Exception] = None
        for _ in range(keypool.total_keys):
            api_key = await keypool.get_key()
            if not api_key:
                break

            # Level 1: single-key retry
            # Rate limit errors do NOT consume attempts. Bounded by
            # max_rate_limit_retries (-1 = unlimited, capped by task timeout).
            attempt = 0
            rate_limit_count = 0
            while attempt < max_retries:
                # 获取该服务商的并发槽位（阻塞等待直到有空闲）
                if provider_sem:
                    await provider_sem.acquire()
                try:
                    resp = await provider.chat_completion(
                        api_key=api_key,
                        messages=messages,
                        model=api_model,
                        tools=tools,
                        output_schema=output_schema,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                        tool_choice=tool_choice,
                        thinking_config=thinking_config,
                        custom_headers=custom_headers,
                        session_id=session_id,
                        prompt_cache_key=prompt_cache_key,
                    )

                    # 请求成功
                    await keypool.report_success(api_key)
                    logger.info(
                        "llm_call",
                        agent=agent_name or "unknown",
                        provider=provider_name,
                        model=model,
                        key_suffix=api_key[-6:],
                        prompt_tokens=resp.usage.prompt_tokens,
                        completion_tokens=resp.usage.completion_tokens,
                        cache_creation_tokens=resp.usage.cache_creation_tokens,
                        cache_read_tokens=resp.usage.cache_read_tokens,
                        has_thinking=bool(resp.thinking_content),
                    )
                    return resp

                except LLMAuthError:
                    await keypool.mark_unhealthy(api_key)
                    logger.warning(
                        "llm_auth_error",
                        provider=provider_name,
                        key_suffix=api_key[-6:],
                    )
                    break  # Don't retry auth errors, rotate key

                except LLMRateLimitError as e:
                    # Rate limit is temporary — do NOT mark key as failed
                    # and do NOT consume an attempt.
                    rate_limit_count += 1
                    last_error = e
                    if max_rate_limit_retries >= 0 and rate_limit_count > max_rate_limit_retries:
                        logger.warning(
                            "llm_rate_limit_exceeded",
                            provider=provider_name,
                            key_suffix=api_key[-6:],
                            rate_limit_count=rate_limit_count,
                            max_rate_limit_retries=max_rate_limit_retries,
                        )
                        break  # Rotate to next key
                    delay = min(base_delay * (backoff ** min(rate_limit_count, 6)), 60)
                    logger.warning(
                        "llm_rate_limit",
                        provider=provider_name,
                        key_suffix=api_key[-6:],
                        rate_limit_count=rate_limit_count,
                        delay=round(delay, 1),
                    )
                    await asyncio.sleep(delay)
                    continue  # Do NOT increment attempt

                except LLMAPIError as e:
                    await keypool.report_failure(api_key)
                    last_error = e
                    delay = base_delay * (backoff ** attempt)
                    logger.warning(
                        "llm_api_error",
                        provider=provider_name,
                        key_suffix=api_key[-6:],
                        attempt=attempt + 1,
                        error=str(e)[:100],
                    )
                    await asyncio.sleep(delay)

                except Exception as e:
                    await keypool.report_failure(api_key)
                    last_error = e
                    break  # Unknown error, rotate key

                finally:
                    # 无论成功或失败，释放并发槽位并通知等待分配服务商的请求
                    if provider_sem:
                        provider_sem.release()
                        self._slot_freed.set()

                attempt += 1

        raise LLMAllKeysExhaustedError(
            f"All keys exhausted for provider '{provider_name}'. "
            f"Last error: {last_error}"
        )

    async def _execute_tool(
        self,
        tool_name: str,
        arguments: Dict,
        tool_handlers: Optional[Dict[str, Callable[..., Coroutine]]],
        round_num: int,
        result: AgentResult,
        mcp_tool_map: Optional[Dict[str, str]] = None,
    ) -> str:
        """Execute a tool handler and record result.

        Routing priority:
          1. Local tool_handlers (provided by caller)
          2. MCP Tool (via McpClientService, if tool_name in mcp_tool_map)
          3. Not found → return error message to LLM
        """
        start = time.monotonic()

        # Priority 1: Local tool handler
        if tool_handlers and tool_name in tool_handlers:
            handler = tool_handlers[tool_name]
            try:
                tool_result = await asyncio.wait_for(
                    handler(**arguments),
                    timeout=300,
                )
                tool_result_str = str(tool_result)
            except AgentTaskComplete:
                raise
            except asyncio.TimeoutError:
                tool_result_str = f"Tool '{tool_name}' timed out after 300s"
                logger.warning("tool_timeout", tool_name=tool_name)
            except Exception as e:
                tool_result_str = f"Tool '{tool_name}' error: {e}"
                logger.error("tool_handler_error", tool_name=tool_name, error=str(e))

        # Priority 2: MCP Tool
        elif mcp_tool_map and tool_name in mcp_tool_map and self._mcp_client:
            server_name = mcp_tool_map[tool_name]
            try:
                tool_result_str = await self._mcp_client.call_tool(
                    server_name, tool_name, arguments,
                )
            except Exception as e:
                tool_result_str = f"MCP Tool '{tool_name}' error: {e}"
                logger.error(
                    "mcp_tool_error",
                    tool_name=tool_name,
                    server=server_name,
                    error=str(e),
                )

        # Not found
        else:
            error_msg = f"Tool '{tool_name}' not found in local handlers or MCP servers"
            logger.warning("tool_not_found", tool_name=tool_name)
            result.tool_call_history.append(ToolCallRecord(
                round=round_num,
                tool_name=tool_name,
                arguments=arguments,
                result=error_msg,
                elapsed_ms=0,
            ))
            return error_msg

        elapsed_ms = (time.monotonic() - start) * 1000
        result.tool_call_history.append(ToolCallRecord(
            round=round_num,
            tool_name=tool_name,
            arguments=arguments,
            result=tool_result_str,
            elapsed_ms=round(elapsed_ms, 1),
        ))

        logger.debug(
            "tool_executed",
            tool_name=tool_name,
            elapsed_ms=round(elapsed_ms, 1),
            result_len=len(tool_result_str),
        )
        return tool_result_str
