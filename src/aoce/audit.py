"""Append-only audit log of every tool call (AOCE-40).

Each call through the tool registry produces one `AuditEntry`: who made it, which tool, with what
arguments, when, how long it took, how it ended and how much it returned. Entries are written as
one JSON log line each on the `aoce.audit` logger, so they outlive the in-memory incident store, and
appended to the incident whose investigation made the call. Calls made by hand through
`POST /tools/{name}` have no incident and are logged with actor "operator".

The incident is bound with `recording()`, a context variable, so tasks the investigation starts for
parallel tool calls inherit it and the tool layer needs no incident id passed down.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

log = logging.getLogger("aoce.audit")

Actor = Literal["agent", "operator"]
Outcome = Literal["ok", "error", "cancelled"]


class AuditEntry(BaseModel):
    at: datetime  # when the call started, UTC
    incident_id: str | None
    actor: Actor
    tool: str
    arguments: dict[str, Any]
    duration_ms: int
    # cancelled: the call was still running when it was stopped (tool timeout, hard deadline, shutdown).
    outcome: Outcome
    result_chars: int  # length of the result or error text the caller received, before any truncation


_current: ContextVar[tuple[str, list[AuditEntry]] | None] = ContextVar("aoce_audit", default=None)


@contextmanager
def recording(incident_id: str, entries: list[AuditEntry]) -> Iterator[None]:
    """Append tool calls made in this context (and tasks started from it) to an incident's entries.

    Nothing in the agent changes or removes an entry once appended.
    """
    token = _current.set((incident_id, entries))
    try:
        yield
    finally:
        _current.reset(token)


def record(
    tool: str, arguments: dict[str, Any], at: datetime, duration_s: float, content: str | None, is_error: bool
) -> AuditEntry:
    """Record one finished call. `content` None means it never returned (cancelled)."""
    bound = _current.get()
    entry = AuditEntry(
        at=at,
        incident_id=bound[0] if bound else None,
        actor="agent" if bound else "operator",
        tool=tool,
        arguments=dict(arguments),
        duration_ms=int(duration_s * 1000),
        outcome="cancelled" if content is None else "error" if is_error else "ok",
        result_chars=0 if content is None else len(content),
    )
    if bound:
        bound[1].append(entry)
    log.info("tool call %s", entry.model_dump_json())
    return entry
