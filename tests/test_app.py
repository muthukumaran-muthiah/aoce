import time

import pytest
from fastapi.testclient import TestClient

from aoce.app import INCIDENTS, app
from aoce.config import get_settings

# Same value for both secrets so one client can call every endpoint; tests/test_auth.py checks them apart.
AUTH = {"Authorization": "Bearer test-token"}
client = TestClient(app, headers=AUTH)


def webhook(*statuses: str) -> dict:
    return {
        "version": "4",
        "groupKey": "{}:{alertname=\"HighErrorRate\"}",
        "status": "firing",
        "receiver": "aoce",
        "groupLabels": {"alertname": "HighErrorRate"},
        "commonLabels": {"alertname": "HighErrorRate"},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "alerts": [
            {
                "status": s,
                "labels": {"alertname": "HighErrorRate", "service_name": "product-catalog"},
                "annotations": {"summary": "error rate above 5%"},
                "startsAt": "2026-10-13T08:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus:9090/graph",
                "fingerprint": f"fp{i}",
            }
            for i, s in enumerate(statuses)
        ],
    }


@pytest.fixture(autouse=True)
def clear_incidents():
    INCIDENTS.clear()


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setenv("AOCE_WEBHOOK_TOKEN", "test-token")
    monkeypatch.setenv("AOCE_API_KEY", "test-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_firing_alerts_open_incidents_and_resolved_are_ignored():
    resp = client.post("/alerts", json=webhook("firing", "resolved", "firing"))
    assert resp.status_code == 202
    ids = resp.json()["incidents"]
    assert len(ids) == 2
    incidents = client.get("/incidents").json()
    assert {i["alert"]["labels"]["service_name"] for i in incidents} == {"product-catalog"}


def test_malformed_payload_is_rejected():
    assert client.post("/alerts", json={"alerts": []}).status_code == 422


def test_without_api_key_incidents_are_skipped(monkeypatch):
    monkeypatch.setattr("aoce.app.make_investigator", lambda: None)
    incident_id = client.post("/alerts", json=webhook("firing")).json()["incidents"][0]
    assert client.get(f"/incidents/{incident_id}").json()["status"] == "skipped"


def test_unknown_incident_is_404():
    assert client.get("/incidents/nope").status_code == 404


def test_resent_alert_does_not_open_a_second_incident(monkeypatch):
    monkeypatch.setattr("aoce.app.make_investigator", lambda: None)
    client.post("/alerts", json=webhook("firing"))
    assert client.post("/alerts", json=webhook("firing")).json()["incidents"] == []
    assert len(client.get("/incidents").json()) == 1


def test_firing_alert_is_investigated_in_the_background(monkeypatch):
    from aoce.investigation import InvestigationResult

    class FakeInvestigator:
        async def investigate(self, alert):
            return InvestigationResult(status="concluded", stop_reason="finished", report={"summary": "lock"}, steps=2)

    monkeypatch.setattr("aoce.app.make_investigator", lambda: FakeInvestigator())
    with TestClient(app, headers=AUTH) as c:
        incident_id = c.post("/alerts", json=webhook("firing")).json()["incidents"][0]
        for _ in range(50):
            body = c.get(f"/incidents/{incident_id}").json()
            if body["status"] == "concluded":
                break
            time.sleep(0.02)
    assert body["status"] == "concluded"
    assert body["investigation"]["report"] == {"summary": "lock"}
