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
from typing import Any, NamedTuple

from aoce import audit

log = logging.getLogger("aoce.tools")

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolError(Exception):
    """A tool failed in a way the model should see and can react to (bad input, backend down)."""


class ToolInputError(ToolError):
    """The arguments were wrong, not the backend: POST /tools answers 4xx for these."""


class ToolOutcome(NamedTuple):
    content: str
    is_error: bool
    # The arguments were rejected (schema or handler check), so a retry with the same input fails too.
    bad_input: bool = False


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "null": (type(None),), "object": (dict,), "array": (list,),
}


def schema_errors(schema: dict[str, Any], value: Any, path: str = "input") -> list[str]:
    """Check `value` against the subset of JSON Schema the tool definitions use (type, enum, properties,
    required, additionalProperties, items). The API enforces strict schemas for the model, but POST
    /tools takes arguments from people (AOCE-69)."""
    types = schema.get("type")
    if types is not None:
        allowed = [types] if isinstance(types, str) else types
        # bool is an int in Python, but not a JSON integer or number.
        ok = any(
            isinstance(value, _JSON_TYPES[t]) and not (isinstance(value, bool) and t in ("integer", "number"))
            for t in allowed
        )
        if not ok:
            return [f"{path}: expected {' or '.join(allowed)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: must be one of {schema['enum']}"]
    errors: list[str] = []
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{path}.{k}: required" for k in schema.get("required", []) if k not in value]
        for k, v in value.items():
            if k in props:
                errors += schema_errors(props[k], v, f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{k}: unexpected property")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errors += schema_errors(schema["items"], v, f"{path}[{i}]")
    return errors


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
        """Run a tool and return (content, is_error) ready for a tool_result block."""
        outcome = await self.call(name, tool_input)
        return outcome.content, outcome.is_error

    async def call(self, name: str, tool_input: dict[str, Any]) -> ToolOutcome:
        """Run a tool and say whether a failure was the caller's arguments.

        Every call, including unknown tools and calls cancelled mid-run, gets an audit entry (AOCE-40).
        """
        at, started = datetime.now(UTC), time.monotonic()
        content, is_error = None, True
        try:
            outcome = await self._run(name, tool_input)
            content, is_error = outcome.content, outcome.is_error
            return outcome
        finally:
            try:
                audit.record(name, tool_input, at, time.monotonic() - started, content, is_error)
            except Exception:
                # Auditing must never change the outcome of the call it observes.
                log.exception("auditing tool call %s failed", name)

    async def _run(self, name: str, tool_input: dict[str, Any]) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutcome(f"unknown tool: {name}", True, bad_input=True)
        if errors := schema_errors(tool.input_schema, tool_input):
            return ToolOutcome(f"invalid arguments: {'; '.join(errors)}"[:500], True, bad_input=True)
        try:
            result = await tool.handler(tool_input)
            return ToolOutcome(json.dumps(result, default=str), False)
        except ToolInputError as e:
            return ToolOutcome(str(e), True, bad_input=True)
        except ToolError as e:
            return ToolOutcome(str(e), True)
        except Exception as e:
            # A bug or an unexpected backend reply (KeyError, bad JSON) is one failed check, not a failed
            # investigation: log it and let the model try another hypothesis (AOCE-60).
            log.exception("tool %s raised", name)
            return ToolOutcome(f"{name} failed unexpectedly: {type(e).__name__}: {e}"[:500], True)


def build_registry(settings) -> ToolRegistry:
    from aoce.tools.db import db_diagnostics_tool
    from aoce.tools.logs import search_logs_tool
    from aoce.tools.metrics import query_metrics_tool
    from aoce.tools.traces import get_traces_tool

    return ToolRegistry(
        [query_metrics_tool(settings), db_diagnostics_tool(settings), search_logs_tool(settings), get_traces_tool(settings)]
    )
