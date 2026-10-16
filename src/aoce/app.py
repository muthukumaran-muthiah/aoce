"""HTTP entry point: Alertmanager posts here, the agent investigates."""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel

from aoce.alerts import Alert, AlertmanagerWebhook
from aoce.config import get_settings
from aoce.tools import build_registry

log = logging.getLogger("aoce")

app = FastAPI(title="AI On-Call Engineer", version="0.1.0")
tools = build_registry(get_settings())


class Incident(BaseModel):
    id: str
    received_at: datetime
    alert: Alert
    status: str = "queued"


# In-memory until incident memory lands (AOCE-31).
INCIDENTS: dict[str, Incident] = {}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/alerts", status_code=status.HTTP_202_ACCEPTED)
async def receive_alerts(payload: AlertmanagerWebhook) -> dict[str, list[str]]:
    """Accept an Alertmanager webhook and open one incident per firing alert."""
    opened = []
    for alert in payload.alerts:
        if alert.status != "firing":
            continue
        incident = Incident(id=uuid.uuid4().hex[:12], received_at=datetime.now(UTC), alert=alert)
        INCIDENTS[incident.id] = incident
        opened.append(incident.id)
        log.info("incident %s opened for %s (%s)", incident.id, alert.labels.get("alertname"), alert.service)
        # TODO(AOCE-29): start the investigation loop in the background.
    return {"incidents": opened}


@app.get("/incidents")
async def list_incidents() -> list[Incident]:
    return list(INCIDENTS.values())


@app.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    """Tool definitions exactly as the agent sees them."""
    return tools.definitions()


@app.post("/tools/{name}")
async def run_tool(name: str, tool_input: dict[str, Any]) -> Any:
    """Run one read-only tool by hand, e.g. to check it against the live demo before the agent uses it."""
    if name not in tools.names():
        raise HTTPException(status_code=404, detail=f"unknown tool: {name}")
    content, is_error = await tools.run(name, tool_input)
    if is_error:
        raise HTTPException(status_code=502, detail=content)
    return json.loads(content)
