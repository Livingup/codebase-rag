"""Agent execution trace logger — writes detailed run logs to files.

Each agent run produces a separate log file containing:
  - Agent definition (system prompt, tools, parameters)
  - Every message in the conversation (incremental append)

Used for debugging agent behavior, especially long-running agents
like the context engine.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from codebase_rag.services.agent.models import (
    AgentDefinition,
    Message,
    ToolSchema,
)


class AgentTraceLogger:
    """Writes agent execution trace to a file incrementally."""

    def __init__(self, log_dir: str, agent_name: str, session_id: str) -> None:
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._path = self._dir / f"{agent_name}_{ts}_{session_id[:8]}.md"
        self._start_time = time.monotonic()

    @property
    def path(self) -> str:
        return str(self._path)

    def log_header(
        self,
        agent_def: AgentDefinition,
        provider: str,
        model: str,
        temperature: float,
        timeout: int,
        all_tools: List[ToolSchema],
    ) -> None:
        """Write agent definition header."""
        lines = [
            f"# Agent Trace: {agent_def.name}",
            f"**Time**: {datetime.now().isoformat()}",
            f"**Description**: {agent_def.description}",
            f"**Provider**: {provider}",
            f"**Model**: {model}",
            f"**Temperature**: {temperature}",
            f"**Timeout**: {timeout}s",
            "",
            "## System Prompt",
            "```",
            agent_def.system_prompt,
            "```",
            "",
            "## Tools",
        ]
        for tool in all_tools:
            lines.append(f"### {tool.name}")
            lines.append(f"**Description**: {tool.description}")
            lines.append("```json")
            lines.append(json.dumps(tool.parameters, indent=2, ensure_ascii=False))
            lines.append("```")
            lines.append("")

        lines.append("---")
        lines.append("## Messages")
        lines.append("")

        self._write("\n".join(lines))

    def log_message(self, msg: Message, round_num: int) -> None:
        """Append a single message to the trace log."""
        elapsed = round(time.monotonic() - self._start_time, 1)
        lines = [f"### [{elapsed}s] Round {round_num} — {msg.role}"]

        if msg.role == "user":
            lines.append("```")
            if isinstance(msg.content, list):
                formatted_parts = []
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        formatted_parts.append(str(part.get("text", "")))
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        url = str((part.get("image_url") or {}).get("url", ""))
                        preview = url[:64] + "...[truncated]" if len(url) > 64 else url
                        formatted_parts.append(f"[image_url: {preview}]")
                    else:
                        formatted_parts.append(json.dumps(part, ensure_ascii=False))
                lines.append("\n".join(formatted_parts))
            else:
                lines.append(msg.content or "")
            lines.append("```")

        elif msg.role == "assistant":
            if msg.content:
                lines.append("**Content:**")
                lines.append("```")
                lines.append(msg.content)
                lines.append("```")
            if msg.thinking_content:
                lines.append("**Thinking:**")
                lines.append("```")
                lines.append(msg.thinking_content[:500] + ("..." if len(msg.thinking_content or "") > 500 else ""))
                lines.append("```")
            if msg.tool_calls:
                lines.append("**Tool Calls:**")
                for tc in msg.tool_calls:
                    lines.append(f"- `{tc.name}` (id: {tc.id})")
                    lines.append("  ```json")
                    lines.append("  " + json.dumps(tc.arguments, ensure_ascii=False))
                    lines.append("  ```")

        elif msg.role == "tool":
            lines.append(f"**Tool**: `{msg.name}` (call_id: {msg.tool_call_id})")
            content = msg.content or ""
            if len(content) > 2000:
                lines.append(f"**Result** ({len(content)} chars, truncated in log):")
                lines.append("```")
                lines.append(content[:2000] + "\n... [truncated]")
                lines.append("```")
            else:
                lines.append("**Result:**")
                lines.append("```")
                lines.append(content)
                lines.append("```")

        elif msg.role == "system":
            lines.append("*(system prompt — see header above)*")

        lines.append("")
        self._write("\n".join(lines))

    def log_finish(self, reason: str, rounds: int, total_tokens: Any) -> None:
        """Write final summary."""
        elapsed = round(time.monotonic() - self._start_time, 1)
        lines = [
            "---",
            "## Finished",
            f"**Reason**: {reason}",
            f"**Rounds**: {rounds}",
            f"**Elapsed**: {elapsed}s",
            f"**Total Tokens**: {total_tokens}",
            "",
        ]
        self._write("\n".join(lines))

    def _write(self, text: str) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
