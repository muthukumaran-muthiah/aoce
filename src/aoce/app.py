"""HTTP entry point: Alertmanager posts here, the agent investigates."""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel

from aoce.alerts import Alert, AlertmanagerWebhook
from aoce.auth import require_api_key, require_webhook_token
from aoce.config import get_settings
from aoce.investigation import InvestigationResult, Investigator
from aoce.llm import LLMClient
from aoce.tools import build_registry

log = logging.getLogger("aoce")

app = FastAPI(title="AI On-Call Engineer", version="0.1.0")
tools = build_registry(get_settings())


class Incident(BaseModel):
    id: str
    received_at: datetime
    alert: Alert
    # queued -> investigating -> concluded | inconclusive | failed; skipped when no API key is set.
    status: str = "queued"
    investigation: InvestigationResult | None = None


# In-memory until incident memory lands (AOCE-31).
INCIDENTS: dict[str, Incident] = {}
_tasks: set[asyncio.Task] = set()


def make_investigator() -> Investigator | None:
    settings = get_settings()
    if settings.anthropic_api_key is None:
        return None
    return Investigator(LLMClient(settings), tools, settings)


async def run_investigation(incident: Incident, investigator: Investigator) -> None:
    incident.status = "investigating"
    try:
        incident.investigation = await investigator.investigate(incident.alert)
        incident.status = incident.investigation.status
    except Exception:
        log.exception("investigation of incident %s crashed", incident.id)
        incident.status = "failed"
    log.info("incident %s %s", incident.id, incident.status)


def open_incident_for(alert: Alert) -> Incident | None:
    """Return a new incident, or None if this alert already has an open one (Alertmanager re-sends)."""
    for existing in INCIDENTS.values():
        same = existing.alert.fingerprint == alert.fingerprint and existing.alert.starts_at == alert.starts_at
        if alert.fingerprint and same:
            return None
    return Incident(id=uuid.uuid4().hex[:12], received_at=datetime.now(UTC), alert=alert)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/alerts", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_webhook_token)])
async def receive_alerts(payload: AlertmanagerWebhook) -> dict[str, list[str]]:
    """Accept an Alertmanager webhook and open one incident per firing alert."""
    opened = []
    investigator = make_investigator()
    for alert in payload.alerts:
        if alert.status != "firing":
            continue
        incident = open_incident_for(alert)
        if incident is None:
            continue
        INCIDENTS[incident.id] = incident
        opened.append(incident.id)
        log.info("incident %s opened for %s (%s)", incident.id, alert.labels.get("alertname"), alert.service)
        if investigator is None:
            incident.status = "skipped"
            log.warning("ANTHROPIC_API_KEY is not set; incident %s will not be investigated", incident.id)
            continue
        task = asyncio.create_task(run_investigation(incident, investigator))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return {"incidents": opened}


@app.get("/incidents", dependencies=[Depends(require_api_key)])
async def list_incidents() -> list[Incident]:
    return list(INCIDENTS.values())


@app.get("/incidents/{incident_id}", dependencies=[Depends(require_api_key)])
async def get_incident(incident_id: str) -> Incident:
    incident = INCIDENTS.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"unknown incident: {incident_id}")
    return incident


@app.get("/tools", dependencies=[Depends(require_api_key)])
async def list_tools() -> list[dict[str, Any]]:
    """Tool definitions exactly as the agent sees them."""
    return tools.definitions()


@app.post("/tools/{name}", dependencies=[Depends(require_api_key)])
async def run_tool(name: str, tool_input: dict[str, Any]) -> Any:
    """Run one read-only tool by hand, e.g. to check it against the live demo before the agent uses it."""
    if name not in tools.names():
        raise HTTPException(status_code=404, detail=f"unknown tool: {name}")
    content, is_error = await tools.run(name, tool_input)
    if is_error:
        raise HTTPException(status_code=502, detail=content)
    return json.loads(content)
