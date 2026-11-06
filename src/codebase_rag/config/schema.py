"""Pydantic configuration models for all services.

Single Source of Truth — all services read config from these models.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


def _default_data_dir() -> str:
    return os.environ.get("CODEBASE_RAG_DATA_DIR", "data")


def _join_config_path(base: str, *parts: str) -> str:
    return "/".join([base.rstrip("/\\"), *parts])


# =============================================================================
# Shared config types
# =============================================================================


class RetryConfig(BaseModel):
    """Retry strategy configuration (shared by Embedding + Agent services)."""

    max_retries_per_key: int = 3
    retry_base_delay: float = 1.0
    retry_backoff_multiplier: float = 2.0
    key_cooldown_seconds: int = 60


class ApiKeyConfig(BaseModel):
    """Single API key entry (supports env var reference like ${ENV_VAR})."""

    key: str


# =============================================================================
# System config
# =============================================================================


class LogConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    file: Optional[str] = None              # all-level log file (None = stdout only)
    error_file: Optional[str] = None        # WARNING + ERROR only log file
    retention_days: int = 1                 # regular log retention in days
    max_bytes: int = 50 * 1024 * 1024       # error log rotation: max file size
    backup_count: int = 5                   # error log rotation: number of backup files


class SystemConfig(BaseModel):
    data_dir: str = Field(default_factory=_default_data_dir)
    log: LogConfig = Field(default_factory=LogConfig)


# =============================================================================
# 04-Embedding service config
# =============================================================================


class EmbeddingProviderConfig(BaseModel):
    """Base configuration for an embedding provider."""

    api_base_url: Optional[str] = None
    model_name: str = "gemini-embedding-2"
    dimension: Optional[int] = None
    batch_size: int = 100
    max_tokens_per_text: int = 8192
    request_timeout: int = 30
    api_keys: List[ApiKeyConfig] = Field(default_factory=list)
    retry: RetryConfig = Field(default_factory=RetryConfig)


class DenseEmbeddingConfig(BaseModel):
    """Dense vector embedding configuration."""

    active_provider: str = "gemini"
    providers: Dict[str, EmbeddingProviderConfig] = Field(default_factory=dict)


class EmbeddingConfig(BaseModel):
    """Full embedding service configuration."""

    dense: DenseEmbeddingConfig = Field(default_factory=DenseEmbeddingConfig)
    max_concurrency: int = 3


# =============================================================================
# 01-Qdrant storage service config
# =============================================================================


class QdrantConnectionConfig(BaseModel):
    host: str = "localhost"
    port: int = 6333
    grpc_port: int = 6334
    api_key: Optional[str] = None
    prefer_grpc: bool = True
    https: bool = False
    timeout: int = 30
    connect_timeout: int = 10


class VectorConfig(BaseModel):
    name: str = "dense"
    size: int = 3072
    distance: str = "Cosine"


class HnswConfig(BaseModel):
    enabled: bool = False
    m: int = 16
    ef_construct: int = 100


class PayloadIndexConfig(BaseModel):
    field: str
    type: str = "keyword"


class CollectionConfig(BaseModel):
    name: str = "codebase"
    dense_vector: VectorConfig = Field(default_factory=VectorConfig)
    hnsw_config: HnswConfig = Field(default_factory=HnswConfig)
    payload_indexes: List[PayloadIndexConfig] = Field(default_factory=lambda: [
        PayloadIndexConfig(field="file_version_id"),
        PayloadIndexConfig(field="project_id"),
        PayloadIndexConfig(field="blob_hash"),
    ])


class QdrantStorageConfig(BaseModel):
    connection: QdrantConnectionConfig = Field(default_factory=QdrantConnectionConfig)
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    upsert_batch_size: int = 100
    scroll_batch_size: int = 100
    max_concurrency: int = 4


# =============================================================================
# 02-Memory Qdrant service config
# =============================================================================


class MemoryCacheConfig(BaseModel):
    max_branches: int = 5
    idle_timeout_minutes: int = 30  # 0 = never evict idle active instances


class MemoryInstanceConfig(BaseModel):
    collection_name: str = "codebase"
    dense_vector: VectorConfig = Field(default_factory=VectorConfig)
    hnsw_config: HnswConfig = Field(default_factory=lambda: HnswConfig(enabled=True))
    payload_indexes: List[PayloadIndexConfig] = Field(default_factory=lambda: [
        PayloadIndexConfig(field="file_version_id"),
        PayloadIndexConfig(field="file_path"),
        PayloadIndexConfig(field="entity_type"),
        PayloadIndexConfig(field="entity_name"),
        PayloadIndexConfig(field="project_id"),
    ])


class SearchConfig(BaseModel):
    default_top_k: int = 10
    max_top_k: int = 50


class MemoryQdrantConfig(BaseModel):
    cache: MemoryCacheConfig = Field(default_factory=MemoryCacheConfig)
    instance: MemoryInstanceConfig = Field(default_factory=MemoryInstanceConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)


# =============================================================================
# 03-Agent service config
# =============================================================================


class LLMModelConfig(BaseModel):
    name: str
    context_window: int
    max_output_tokens: int
    compression_threshold: Optional[int] = None  # token count to trigger context compression; None = disabled
    supports_tool_calling: bool = True
    supports_structured_output: bool = True


class ProviderModelRef(BaseModel):
    """A model reference within a provider. id = registry key, alias = actual API name."""
    id: str                        # references LLMConfig.models key
    alias: Optional[str] = None    # actual API model name; defaults to id if omitted
    tool_choice: Optional[str] = None  # "auto" | "none" | "required"; None = provider default
    thinking: Optional[str] = None     # raw JSON string, passed directly to provider as-is
    custom_headers: Optional[Dict[str, str]] = None  # extra HTTP headers for this model's requests


class LLMProviderConfig(BaseModel):
    api_format: str  # "gemini" | "openai_chat" | "openai_responses" | "anthropic" | "zhipu" | "deepseek" | "jev"
    api_base_url: str
    models: List[ProviderModelRef] = Field(default_factory=list)
    api_keys: List[ApiKeyConfig] = Field(default_factory=list)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    request_timeout: int = 1800  # single LLM call timeout in seconds (30min)
    max_concurrency: int = 3    # 该服务商的最大并发请求数（受服务商平台并发限制约束）


class LLMConfig(BaseModel):
    models: Dict[str, LLMModelConfig] = Field(default_factory=dict)  # model ID → metadata
    providers: Dict[str, LLMProviderConfig] = Field(default_factory=dict)
    default_provider: str = "gemini"
    default_model: str = "gemini-2.5-flash"


class AgentExecutionConfig(BaseModel):
    default_timeout: int = 3600  # agent task execution timeout in seconds (1h)
    default_temperature: float = 0.0
    trace_log_dir: Optional[str] = None  # None = disabled


class AgentProviderEntry(BaseModel):
    provider: str
    model: str
    priority: int = 0


class ContextCompressionConfig(BaseModel):
    """LLM config for the context compression agent."""
    provider: Optional[str] = None   # None → default_provider
    model: Optional[str] = None      # None → default_model
    providers: List[AgentProviderEntry] = Field(default_factory=list)
    temperature: float = 0.0
    timeout: int = 300               # compression agent timeout in seconds (5min)


class AgentConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    execution: AgentExecutionConfig = Field(default_factory=AgentExecutionConfig)
    context_compression: ContextCompressionConfig = Field(default_factory=ContextCompressionConfig)


# =============================================================================
# 05-Event detection service config
# =============================================================================


class HttpReceiverConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 9876
    endpoint: str = "/hook"


class NamedPipeConfig(BaseModel):
    path: str = "\\\\.\\pipe\\codebase-rag-hook"


class HookReceiverConfig(BaseModel):
    mode: str = "http"
    http: HttpReceiverConfig = Field(default_factory=HttpReceiverConfig)
    named_pipe: NamedPipeConfig = Field(default_factory=NamedPipeConfig)


class WatcherConfig(BaseModel):
    enabled: bool = False
    debounce_ms: int = 500
    watch_paths: List[str] = Field(default_factory=lambda: [
        "HEAD", "refs/heads/", "MERGE_HEAD", "REBASE_HEAD",
    ])


class DedupConfig(BaseModel):
    window_ms: int = 1000


class EventDetectionConfig(BaseModel):
    hook_receiver: HookReceiverConfig = Field(default_factory=HookReceiverConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)


# =============================================================================
# 06-Management API service config
# =============================================================================


class CorsConfig(BaseModel):
    enabled: bool = True
    allow_origins: List[str] = Field(default_factory=lambda: ["*"])


class ApiServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 18080
    workers: int = 1
    cors: CorsConfig = Field(default_factory=CorsConfig)


class AuthConfig(BaseModel):
    enabled: bool = False
    api_key: Optional[str] = None


class AdminUiConfig(BaseModel):
    """Local-only management dashboard configuration."""

    enabled: bool = True


class ClientStatusConfig(BaseModel):
    """Client availability dashboard and live probe configuration."""

    live_probe_enabled: bool = False
    live_probe_timeout: float = 10.0


class ManagementApiConfig(BaseModel):
    server: ApiServerConfig = Field(default_factory=ApiServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    admin_ui: AdminUiConfig = Field(default_factory=AdminUiConfig)
    client_status: ClientStatusConfig = Field(default_factory=ClientStatusConfig)
    registry_path: str = ""


# =============================================================================
# 07-Git operations service config
# =============================================================================


class FileFilterConfig(BaseModel):
    exclude_patterns: List[str] = Field(default_factory=list)


class GitOperationsConfig(BaseModel):
    git_executable: str = "git"
    command_timeout: int = 30
    max_concurrency: int = 4
    file_filter: FileFilterConfig = Field(default_factory=FileFilterConfig)


# =============================================================================
# 08-Workflow orchestrator service config
# =============================================================================


class ContentCommitConfig(BaseModel):
    auto_generate: bool = True
    max_file_concurrency: int = 5
    point_token_limit: int = 8192      # max tokens per knowledge point description
    skip_unchanged_hash: bool = True


class BranchSwitchConfig(BaseModel):
    max_diff_concurrency: int = 3


class InitializationConfig(BaseModel):
    mode: str = "incremental"             # "full" = 全量初始化 | "incremental" = 仅跟踪增量（默认）
    max_file_concurrency: int = 10


class WorkflowProviderEntry(BaseModel):
    """A single provider/model pair in a workflow agent fallback queue."""
    provider: str                       # LLM provider name (references agent.llm.providers key)
    model: str                          # LLM model id (references provider's models[].id)
    priority: int = 0                   # smaller value = higher priority


class KnowledgeProducerConfig(BaseModel):
    """Config for the knowledge_producer agent (source code → business description)."""
    providers: List[WorkflowProviderEntry] = Field(default_factory=list)
    timeout: int = 120                  # agent timeout in seconds
    max_points_per_call: int = 10       # max knowledge points per single tool call
    max_rate_limit_retries: int = -1    # per-key 429 retry cap; -1 = unlimited


class CompensationConfig(BaseModel):
    """Config for the background failed-file compensation task."""
    enabled: bool = True                # enable/disable compensation loop
    scan_interval: int = 60             # base interval between scans (seconds)
    base_backoff: int = 30              # base backoff duration (seconds)
    max_backoff: int = 1800             # max backoff cap (seconds, default 30min)
    batch_size: int = 10                # max files processed per round
    max_file_retries: int = 50          # per-file retry limit before dead letter


class WorkflowConfig(BaseModel):
    data_dir: str = ""
    content_commit: ContentCommitConfig = Field(default_factory=ContentCommitConfig)
    branch_switch: BranchSwitchConfig = Field(default_factory=BranchSwitchConfig)
    initialization: InitializationConfig = Field(default_factory=InitializationConfig)
    knowledge_producer: KnowledgeProducerConfig = Field(default_factory=KnowledgeProducerConfig)
    compensation: CompensationConfig = Field(default_factory=CompensationConfig)


# =============================================================================
# 11-Context Engine service config
# =============================================================================


class McpServerBinding(BaseModel):
    """Agent 接入单个 MCP 服务的绑定配置。"""
    # 工具白名单：仅引入指定工具；为空列表或不配置则引入该服务的所有工具
    include_tools: List[str] = Field(default_factory=list)


class ContextEngineConfig(BaseModel):
    provider: Optional[str] = None      # LLM provider; None → agent default_provider
    model: Optional[str] = None         # LLM model; None → agent default_model
    providers: List[AgentProviderEntry] = Field(default_factory=list)
    max_rate_limit_retries: int = -1    # per-key 429 retry cap; -1 = unlimited
    # 需要接入的 MCP 服务（key 对应 mcp_client.servers 的 key，value 为绑定配置）
    mcp_servers: Dict[str, McpServerBinding] = Field(
        default_factory=lambda: {"codebase_rag": McpServerBinding(include_tools=["codebase_search"])}
    )
    rg_timeout: int = 10                # single ripgrep subprocess timeout (seconds)
    max_file_read_lines: int = 500      # safety cap for read_file (truncate beyond this)


# =============================================================================
# MCP Client service config
# =============================================================================


class McpServerEntryConfig(BaseModel):
    """Configuration for a single MCP Server that this system can connect to.

    Supports two transport modes:
    - HTTP (streamable_http / sse): requires ``url``
    - stdio: requires ``command``, optional ``args`` and ``env``
    """
    transport: str = "streamable_http"          # streamable_http / sse / stdio
    # --- HTTP transport fields ---
    url: Optional[str] = None                   # MCP endpoint URL (required for HTTP)
    headers: Dict[str, str] = Field(default_factory=dict)  # custom headers (auth etc.)
    # --- stdio transport fields ---
    command: Optional[str] = None               # executable (required for stdio)
    args: List[str] = Field(default_factory=list)  # command arguments
    env: Dict[str, str] = Field(default_factory=dict)  # environment variables
    # --- common ---
    timeout: int = 30                           # request / startup timeout (seconds)


class McpClientConfig(BaseModel):
    servers: Dict[str, McpServerEntryConfig] = Field(default_factory=dict)


# =============================================================================
# 10-MCP service config
# =============================================================================


class MCPServerInstanceConfig(BaseModel):
    """Configuration for a single MCP Server instance.

    Each instance serves both transports simultaneously:
      - SSE:              GET /sse + POST /messages  (legacy)
      - Streamable HTTP:  POST|GET|DELETE /mcp       (modern)
    """
    host: str = "127.0.0.1"
    port: int = 19090
    name: str = "mcp-server"           # Server name (MCP protocol metadata)
    version: str = "1.0.0"             # Server version


class MCPRerankerProviderEntry(BaseModel):
    """A single provider/model pair in the reranker fallback queue."""
    provider: str                       # LLM provider name (references agent.llm.providers key)
    model: str                          # LLM model id (references provider's models[].id)
    priority: int = 0                   # smaller value = higher priority


class MCPRerankerConfig(BaseModel):
    """Reranker business config (RAG search logic, sibling of servers)."""
    providers: List[MCPRerankerProviderEntry] = Field(default_factory=list)
    recall_limit: int = 400             # max candidates from dense search (Stage 1)
    timeout: int = 60                   # reranker timeout in seconds (default 60s)
    max_rate_limit_retries: int = -1    # per-key 429 retry cap; -1 = unlimited


class KnowledgeSubmissionConfig(BaseModel):
    """MCP knowledge submission limits."""
    point_token_limit: int = 8192  # 单条知识描述最大 tokens


class MCPConfig(BaseModel):
    servers: Dict[str, MCPServerInstanceConfig] = Field(default_factory=dict)
    reranker: MCPRerankerConfig = Field(default_factory=MCPRerankerConfig)
    knowledge_submission: KnowledgeSubmissionConfig = Field(default_factory=KnowledgeSubmissionConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_pending_knowledge(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "knowledge_submission" in data or "pending_knowledge" not in data:
            return data
        legacy = data.get("pending_knowledge")
        if not isinstance(legacy, dict):
            return data
        migrated = dict(data)
        migrated["knowledge_submission"] = {
            "point_token_limit": legacy.get("point_token_limit", 8192),
        }
        return migrated


# =============================================================================
# Root config
# =============================================================================


class AppConfig(BaseModel):
    """Root configuration model — Single Source of Truth."""

    system: SystemConfig = Field(default_factory=SystemConfig)
    qdrant_storage: QdrantStorageConfig = Field(default_factory=QdrantStorageConfig)
    memory_qdrant: MemoryQdrantConfig = Field(default_factory=MemoryQdrantConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    event_detection: EventDetectionConfig = Field(default_factory=EventDetectionConfig)
    management_api: ManagementApiConfig = Field(default_factory=ManagementApiConfig)
    git_operations: GitOperationsConfig = Field(default_factory=GitOperationsConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    context_engine: ContextEngineConfig = Field(default_factory=ContextEngineConfig)
    mcp_client: McpClientConfig = Field(default_factory=McpClientConfig)

    @model_validator(mode="after")
    def _derive_data_dir_paths(self) -> "AppConfig":
        data_dir = self.system.data_dir
        if not data_dir:
            data_dir = _default_data_dir()
            self.system.data_dir = data_dir

        runtime_data_dir = _join_config_path(data_dir, "data")

        trace_fields = self.agent.execution.model_fields_set
        trace_log_dir = self.agent.execution.trace_log_dir
        if (
            "trace_log_dir" not in trace_fields
            or trace_log_dir == ""
        ):
            self.agent.execution.trace_log_dir = _join_config_path(runtime_data_dir, "agent_traces")

        registry_fields = self.management_api.model_fields_set
        if (
            "registry_path" not in registry_fields
            or not self.management_api.registry_path
        ):
            self.management_api.registry_path = _join_config_path(runtime_data_dir, "project_registry.json")

        workflow_fields = self.workflow.model_fields_set
        if (
            "data_dir" not in workflow_fields
            or not self.workflow.data_dir
        ):
            self.workflow.data_dir = runtime_data_dir

        return self
