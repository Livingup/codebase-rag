"""Agent service error types."""


class AgentError(Exception):
    """Base class for all Agent service errors."""


class LLMAPIError(AgentError):
    """LLM API call failed (network / 5xx)."""


class LLMRateLimitError(AgentError):
    """Rate limit triggered (429)."""


class LLMAuthError(AgentError):
    """Authentication failed (401/403)."""


class LLMAllKeysExhaustedError(AgentError):
    """All keys for a provider are unhealthy."""


class AgentNotFoundError(AgentError):
    """Agent name not registered."""


class AgentTimeoutError(AgentError):
    """Agent execution total timeout exceeded."""


class ToolHandlerError(AgentError):
    """Tool handler execution error."""


class StructuredOutputError(AgentError):
    """Failed to parse structured output from LLM response."""


class AgentTaskComplete(Exception):
    """Raised by a tool handler to signal early exit from the agent loop.

    This is NOT an error — it's a control flow signal. When a tool handler
    raises this, the agent loop exits cleanly and returns the current result.
    """
