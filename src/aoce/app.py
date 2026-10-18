"""HTTP entry point: Alertmanager posts here, the agent investigates."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel

from aoce.alerts import Alert, AlertmanagerWebhook
from aoce.audit import AuditEntry, recording
from aoce.auth import require_api_key, require_webhook_token, warn_unset_secrets
from aoce.config import Settings, get_settings
from aoce.investigation import InvestigationResult, Investigator
from aoce.llm import LLMClient
from aoce.tools import ToolRegistry, build_registry

log = logging.getLogger("aoce")

_tools: tuple[Settings, ToolRegistry] | None = None


def get_tools() -> ToolRegistry:
    """The tool registry for the current settings, rebuilt when they change (AOCE-69), e.g. in tests."""
    global _tools
    settings = get_settings()
    if _tools is None or _tools[0] is not settings:
        _tools = (settings, build_registry(settings))
    return _tools[1]


IncidentStatus = Literal["queued", "investigating", "concluded", "inconclusive", "failed", "skipped"]


class Incident(BaseModel):
    id: str
    received_at: datetime
    alert: Alert
    # queued -> investigating -> concluded | inconclusive | failed; skipped when no API key is set.
    status: IncidentStatus = "queued"
    investigation: InvestigationResult | None = None
    # Set on a transient API failure (overload, rate limit, network, 5xx) that a re-send should retry.
    retryable: bool = False
    attempts: int = 1
    # Every tool call this alert's investigations made, earlier failed attempts included, in completion order (AOCE-40).
    audit: list[AuditEntry] = []
    # Why the incident failed when no investigation result says so: a crash or a shutdown mid-run.
    error: str | None = None


class IncidentSummary(BaseModel):
    """One row of GET /incidents; GET /incidents/{id} has the full report, trace and audit."""

    id: str
    received_at: datetime
    alertname: str | None
    service: str | None
    status: IncidentStatus
    root_cause: str | None = None
    attempts: int

    @classmethod
    def of(cls, incident: Incident) -> "IncidentSummary":
        report = incident.investigation.report if incident.investigation else None
        return cls(
            id=incident.id,
            received_at=incident.received_at,
            alertname=incident.alert.labels.get("alertname"),
            service=incident.alert.service,
            status=incident.status,
            root_cause=(report or {}).get("root_cause"),
            attempts=incident.attempts,
        )


class IncidentStore:
    """Incidents in memory, newest last, capped at `max_incidents` (AOCE-62) until AOCE-31 adds a real store.

    When full, the oldest finished incident is dropped. Queued or investigating incidents are never
    dropped, so a running task always has a readable record and a re-send cannot start a second
    investigation; the store only exceeds the cap while that many are active. An index on
    (fingerprint, starts_at) spots Alertmanager re-sends. An evicted incident's key goes with it, so
    an alert still repeating after that many newer incidents is investigated again (AOCE-31).
    """

    ACTIVE = ("queued", "investigating")

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._by_alert: dict[tuple[str, datetime], str] = {}

    @staticmethod
    def _key(alert: Alert) -> tuple[str, datetime] | None:
        return (alert.fingerprint, alert.starts_at) if alert.fingerprint else None

    def find(self, alert: Alert) -> Incident | None:
        key = self._key(alert)
        incident_id = self._by_alert.get(key) if key else None
        return self._incidents.get(incident_id) if incident_id else None

    def add(self, incident: Incident, limit: int) -> None:
        self._incidents[incident.id] = incident
        if key := self._key(incident.alert):
            self._by_alert[key] = incident.id
        while len(self._incidents) > max(limit, 1):
            oldest_finished = next((i for i in self._incidents.values() if i.status not in self.ACTIVE), None)
            if oldest_finished is None:
                break
            self._evict(oldest_finished)

    def remove(self, incident: Incident) -> None:
        self._evict(incident)

    def _evict(self, incident: Incident) -> None:
        del self._incidents[incident.id]
        key = self._key(incident.alert)
        if key and self._by_alert.get(key) == incident.id:
            del self._by_alert[key]

    def get(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    def values(self) -> list[Incident]:
        return list(self._incidents.values())

    def clear(self) -> None:
        self._incidents.clear()
        self._by_alert.clear()

    def active_count(self) -> int:
        return sum(i.status in self.ACTIVE for i in self._incidents.values())

    def __len__(self) -> int:
        return len(self._incidents)


INCIDENTS = IncidentStore()
_tasks: set[asyncio.Task] = set()
# Limits concurrent investigations (AOCE-59). Created on first use so it binds to the running loop.
_slots: asyncio.Semaphore | None = None


def investigation_slots() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(max(get_settings().max_concurrent_investigations, 1))
    return _slots


def make_investigator() -> Investigator | None:
    settings = get_settings()
    if settings.anthropic_api_key is None:  # blank counts as unset, see Settings._blank_is_unset
        return None
    return Investigator(LLMClient(settings), get_tools(), settings)


# One investigator, and so one Anthropic client and connection pool, for the life of the process
# (AOCE-65). Built on first use, closed on shutdown.
_investigator: Investigator | None = None
_investigator_built = False


def get_investigator() -> Investigator | None:
    global _investigator, _investigator_built
    if not _investigator_built:
        _investigator, _investigator_built = make_investigator(), True
    return _investigator


async def close_investigator() -> None:
    global _investigator, _investigator_built
    if _investigator is not None:
        await _investigator.aclose()
    _investigator, _investigator_built = None, False


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _slots
    _slots = None  # bind a fresh semaphore, sized from current settings, to this event loop
    warn_unset_secrets(get_settings())
    yield
    # Stop investigations before closing the client they use, so each ends as cancelled, not as an API error.
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    await close_investigator()


app = FastAPI(title="AI On-Call Engineer", version="0.1.0", lifespan=lifespan)


async def run_investigation(incident: Incident, investigator: Investigator) -> None:
    try:
        # Stays "queued" until a slot frees up.
        async with investigation_slots():
            incident.status = "investigating"
            with recording(incident.id, incident.audit):
                incident.investigation = await investigator.investigate(incident.alert)
            incident.status = incident.investigation.status
            incident.retryable = incident.investigation.retryable
    except asyncio.CancelledError:
        # Shutdown: leave a final status rather than "queued" or "investigating" forever.
        incident.status = "failed"
        incident.error = "cancelled: the agent shut down before the investigation finished"
        raise
    except Exception as e:
        log.exception("investigation of incident %s crashed", incident.id)
        incident.status = "failed"
        incident.error = f"crashed: {type(e).__name__}: {e}"[:500]
    log.info("incident %s %s", incident.id, incident.status)


MAX_ATTEMPTS = 3


def open_incident_for(alert: Alert) -> Incident | None:
    """Return a new incident, or None if this alert already has one (Alertmanager re-sends).

    A re-send of an alert whose investigation failed transiently (overload, rate limit, network,
    5xx) gets a replacement incident (the caller swaps it in) and retries, up to MAX_ATTEMPTS per alert, so an Anthropic
    outage neither re-bills forever nor floods the store. A refusal, bad key or crash is not retried.
    """
    existing = INCIDENTS.find(alert)
    if existing is None:
        return Incident(id=uuid.uuid4().hex[:12], received_at=datetime.now(UTC), alert=alert)
    if existing.status != "failed" or not existing.retryable or existing.attempts >= MAX_ATTEMPTS:
        return None
    return Incident(
        id=uuid.uuid4().hex[:12],
        received_at=datetime.now(UTC),
        alert=alert,
        attempts=existing.attempts + 1,
        audit=list(existing.audit),  # earlier attempts' calls stay visible on the replacement (AOCE-40)
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/alerts", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_webhook_token)])
async def receive_alerts(payload: AlertmanagerWebhook, response: Response) -> dict[str, Any]:
    """Accept an Alertmanager webhook and open one incident per firing alert.

    If the queue was full for any alert, answer 503 so Alertmanager retries the batch with backoff
    instead of waiting for `repeat_interval` (4h by default); alerts already opened dedup on retry.
    """
    opened = []
    dropped = 0
    investigator = get_investigator()
    settings = get_settings()
    capacity = settings.max_concurrent_investigations + settings.max_queued_investigations
    for alert in payload.alerts:
        if alert.status != "firing":
            continue
        incident = open_incident_for(alert)
        if incident is None:
            continue
        if investigator is not None and INCIDENTS.active_count() >= capacity:
            # Not stored, so it cannot push finished incidents out; the 503 below makes Alertmanager retry.
            log.warning("investigation queue is full; dropping %s (%s)", alert.labels.get("alertname"), alert.service)
            dropped += 1
            continue
        if (failed := INCIDENTS.find(alert)) is not None:
            INCIDENTS.remove(failed)  # a retry replaces its failed incident; only now, past the queue check
        INCIDENTS.add(incident, settings.max_incidents)
        opened.append(incident.id)
        log.info("incident %s opened for %s (%s)", incident.id, alert.labels.get("alertname"), alert.service)
        if investigator is None:
            incident.status = "skipped"
            log.warning("ANTHROPIC_API_KEY is not set; incident %s will not be investigated", incident.id)
            continue
        task = asyncio.create_task(run_investigation(incident, investigator))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    if dropped:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"incidents": opened, "dropped": dropped}
    return {"incidents": opened}


@app.get("/incidents", dependencies=[Depends(require_api_key)])
async def list_incidents(limit: int = Query(50, ge=1, le=500)) -> list[IncidentSummary]:
    """The newest `limit` incidents, oldest first, as summaries (AOCE-69)."""
    return [IncidentSummary.of(i) for i in INCIDENTS.values()[-limit:]]


@app.get("/incidents/{incident_id}", dependencies=[Depends(require_api_key)])
async def get_incident(incident_id: str) -> Incident:
    incident = INCIDENTS.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"unknown incident: {incident_id}")
    return incident


@app.get("/tools", dependencies=[Depends(require_api_key)])
async def list_tools() -> list[dict[str, Any]]:
    """Tool definitions exactly as the agent sees them."""
    return get_tools().definitions()


@app.post("/tools/{name}", dependencies=[Depends(require_api_key)])
async def run_tool(name: str, tool_input: dict[str, Any]) -> Any:
    """Run one read-only tool by hand, e.g. to check it against the live demo before the agent uses it.

    422 for arguments the tool rejects, 502 for a backend failure, 504 past AOCE_TOOL_TIMEOUT_S.
    """
    tools, timeout = get_tools(), get_settings().tool_timeout_s
    if name not in tools.names():
        raise HTTPException(status_code=404, detail=f"unknown tool: {name}")
    try:
        outcome = await asyncio.wait_for(tools.call(name, tool_input), timeout=timeout)
    except TimeoutError:
        raise HTTPException(status_code=504, detail=f"{name} timed out after {timeout:.0f}s") from None
    if outcome.is_error:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT if outcome.bad_input else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(status_code=code, detail=outcome.content)
    return json.loads(outcome.content)
