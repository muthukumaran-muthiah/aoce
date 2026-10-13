"""HTTP entry point: Alertmanager posts here, the agent investigates."""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI, status
from pydantic import BaseModel

from aoce.alerts import Alert, AlertmanagerWebhook

log = logging.getLogger("aoce")

app = FastAPI(title="AI On-Call Engineer", version="0.1.0")


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
