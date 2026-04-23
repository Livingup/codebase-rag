"""Data models for Agent Service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional


# ---------------------------------------------------------------------------
# LLM response types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenUsage:
    """Token consumption for an LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(frozen=True)
class ToolCall:
    """A tool call request from the LLM."""

    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""

    content: Optional[str] = None
    thinking_content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    structured_output: Optional[Dict] = None
    finish_reason: str = "stop"  # "stop" | "tool_calls" | "length" | "error"
    usage: TokenUsage = field(default_factory=TokenUsage)


# ---------------------------------------------------------------------------
# Message types (for chat_completion)
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """A single message in a conversation."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: Optional[str] = None
    thinking_content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None  # for role="tool"
    name: Optional[str] = None  # tool name for role="tool"


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSchema:
    """Schema for a tool that an Agent can call."""

    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

@dataclass
class AgentDefinition:
    """Declarative Agent definition — registered by business modules."""

    name: str
    description: str
    system_prompt: str  # supports {variable} template
    provider: Optional[str] = None  # None → default_provider
    model: Optional[str] = None  # None → default_model
    tools: Optional[List[ToolSchema]] = None
    # 需要接入的 MCP 服务（key=服务名, value=该服务的工具白名单；空列表表示引入所有工具）
    mcp_servers: Optional[Dict[str, List[str]]] = None
    output_schema: Optional[Dict] = None
    temperature: Optional[float] = None
    timeout: Optional[int] = None
    max_rate_limit_retries: int = -1  # -1 = unlimited (bounded by timeout only)
    enable_context_compression: bool = True  # False for compression agent itself


# ---------------------------------------------------------------------------
# Agent execution result
# ---------------------------------------------------------------------------

@dataclass
class ToolCallRecord:
    """Record of a single tool call during execution."""

    round: int
    tool_name: str
    arguments: Dict[str, Any]
    result: str
    elapsed_ms: float


@dataclass
class AgentResult:
    """Result of an Agent execution."""

    content: Optional[str] = None
    structured_output: Optional[Dict] = None
    rounds: int = 0
    total_tokens: TokenUsage = field(default_factory=TokenUsage)
    tool_call_history: List[ToolCallRecord] = field(default_factory=list)


@dataclass
class AgentInput:
    """Input for batch_run_agent."""

    input_variables: Dict[str, str]
    tool_handlers: Optional[Dict[str, Callable[..., Coroutine]]] = None


@dataclass(frozen=True)
class AgentInfo:
    """Summary info about a registered Agent."""

    name: str
    description: str
    provider: str
    model: str


@dataclass(frozen=True)
class TokenEstimate:
    """Estimated token consumption for a prompt."""

    system_prompt_tokens: int
    user_input_tokens: int
    tool_schema_tokens: int
    total_tokens: int
    remaining_for_output: int
