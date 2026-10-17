"""Read-only investigation tools the agent can call.

Each tool exposes an Anthropic tool definition (name, description, strict JSON schema) and an async
handler. Handlers return plain dicts; every result carries the exact query that produced it under
"evidence" so the root-cause report can cite it (AOCE-30).
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aoce import audit

log = logging.getLogger("aoce.tools")

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolError(Exception):
    """A tool failed in a way the model should see and can react to (bad input, backend down)."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def names(self) -> list[str]:
        return list(self._tools)

    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition() for t in self._tools.values()]

    async def run(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool and return (content, is_error) ready for a tool_result block.

        Every call, including unknown tools and calls cancelled mid-run, gets an audit entry (AOCE-40).
        """
        at, started = datetime.now(UTC), time.monotonic()
        content, is_error = None, True
        try:
            content, is_error = await self._run(name, tool_input)
            return content, is_error
        finally:
            try:
                audit.record(name, tool_input, at, time.monotonic() - started, content, is_error)
            except Exception:
                # Auditing must never change the outcome of the call it observes.
                log.exception("auditing tool call %s failed", name)

    async def _run(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        tool = self._tools.get(name)
        if tool is None:
            return f"unknown tool: {name}", True
        try:
            result = await tool.handler(tool_input)
            return json.dumps(result, default=str), False
        except ToolError as e:
            return str(e), True
        except Exception as e:
            # A bug or an unexpected backend reply (KeyError, bad JSON) is one failed check, not a failed
            # investigation: log it and let the model try another hypothesis (AOCE-60).
            log.exception("tool %s raised", name)
            return f"{name} failed unexpectedly: {type(e).__name__}: {e}"[:500], True


def build_registry(settings) -> ToolRegistry:
    from aoce.tools.changes import get_diff_tool, recent_changes_tool
    from aoce.tools.db import db_diagnostics_tool
    from aoce.tools.logs import search_logs_tool
    from aoce.tools.metrics import query_metrics_tool

    return ToolRegistry(
        [
            query_metrics_tool(settings),
            db_diagnostics_tool(settings),
            search_logs_tool(settings),
            recent_changes_tool(settings),
            get_diff_tool(settings),
        ]
    )
