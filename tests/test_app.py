import time

import pytest
from fastapi.testclient import TestClient

import aoce.app
from aoce.app import INCIDENTS, app

client = TestClient(app)


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
def clear_incidents(monkeypatch):
    INCIDENTS.clear()
    # Each test builds its own investigator through make_investigator.
    monkeypatch.setattr(aoce.app, "_investigator", None)
    monkeypatch.setattr(aoce.app, "_investigator_built", False)


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

        async def aclose(self):
            pass

    monkeypatch.setattr("aoce.app.make_investigator", lambda: FakeInvestigator())
    with TestClient(app) as c:
        incident_id = c.post("/alerts", json=webhook("firing")).json()["incidents"][0]
        for _ in range(50):
            body = c.get(f"/incidents/{incident_id}").json()
            if body["status"] == "concluded":
                break
            time.sleep(0.02)
    assert body["status"] == "concluded"
    assert body["investigation"]["report"] == {"summary": "lock"}


def test_one_investigator_is_reused_and_closed_on_shutdown(monkeypatch):
    from aoce.investigation import InvestigationResult

    class FakeInvestigator:
        closed = False

        async def investigate(self, alert):
            return InvestigationResult(status="concluded", stop_reason="finished", report={"summary": "lock"})

        async def aclose(self):
            self.closed = True

    built = []

    def make():
        built.append(FakeInvestigator())
        return built[-1]

    monkeypatch.setattr("aoce.app.make_investigator", make)
    with TestClient(app) as c:
        first = webhook("firing")
        second = webhook("firing")
        second["alerts"][0]["fingerprint"] = "other"
        c.post("/alerts", json=first)
        c.post("/alerts", json=second)
        assert len(built) == 1
        assert not built[0].closed
    assert built[0].closed
