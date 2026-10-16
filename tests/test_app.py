import time

import pytest
from fastapi.testclient import TestClient

import aoce.app
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
def clear_incidents(monkeypatch):
    INCIDENTS.clear()
    # Each test builds its own investigator through make_investigator.
    monkeypatch.setattr(aoce.app, "_investigator", None)
    monkeypatch.setattr(aoce.app, "_investigator_built", False)
    monkeypatch.setattr(aoce.app, "_slots", None)


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

        async def aclose(self):
            pass

    monkeypatch.setattr("aoce.app.make_investigator", lambda: FakeInvestigator())
    with TestClient(app, headers=AUTH) as c:
        incident_id = c.post("/alerts", json=webhook("firing")).json()["incidents"][0]
        for _ in range(250):
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


def test_investigations_beyond_the_cap_wait_as_queued(monkeypatch):
    import asyncio

    from aoce.investigation import InvestigationResult

    release = None
    running = []

    class SlowInvestigator:
        async def investigate(self, alert):
            running.append(alert.fingerprint)
            await release.wait()
            return InvestigationResult(status="concluded", stop_reason="finished", report={"summary": "lock"})

        async def aclose(self):
            pass

    monkeypatch.setattr("aoce.app.make_investigator", lambda: SlowInvestigator())
    monkeypatch.setattr(aoce.app, "_slots", asyncio.Semaphore(2))

    def statuses(c):
        return sorted(i["status"] for i in c.get("/incidents").json())

    with TestClient(app) as c:
        release = c.portal.call(asyncio.Event)
        c.post("/alerts", json=webhook("firing", "firing", "firing"))
        for _ in range(250):
            if len(running) == 2:
                break
            time.sleep(0.02)
        time.sleep(0.05)
        assert len(running) == 2
        assert statuses(c) == ["investigating", "investigating", "queued"]
        c.portal.call(release.set)
        for _ in range(250):
            if statuses(c) == ["concluded"] * 3:
                break
            time.sleep(0.02)
        assert statuses(c) == ["concluded"] * 3


def test_incident_store_drops_the_oldest_finished_incident_first():
    from aoce.alerts import Alert
    from aoce.app import Incident, IncidentStore

    def incident(n, status):
        alert = Alert.model_validate(webhook("firing")["alerts"][0] | {"fingerprint": f"fp{n}"})
        return Incident(id=f"i{n}", received_at="2026-10-16T00:00:00Z", alert=alert, status=status)

    store = IncidentStore()
    store.add(incident(1, "investigating"), limit=2)
    store.add(incident(2, "concluded"), limit=2)
    store.add(incident(3, "queued"), limit=2)
    assert [i.id for i in store.values()] == ["i1", "i3"]
    # The dropped incident's alert no longer counts as a re-send.
    assert store.find(incident(2, "concluded").alert) is None
    assert store.find(incident(1, "queued").alert).id == "i1"

    store.add(incident(4, "queued"), limit=2)  # nothing finished: active incidents are never dropped
    assert [i.id for i in store.values()] == ["i1", "i3", "i4"]
    store.values()[0].status = "concluded"
    store.add(incident(5, "queued"), limit=2)  # once one finishes, it is the one dropped
    assert [i.id for i in store.values()] == ["i3", "i4", "i5"]


def test_a_cancelled_investigation_is_marked_failed(monkeypatch):
    import asyncio

    from aoce.alerts import Alert
    from aoce.app import Incident

    monkeypatch.setattr(aoce.app, "_slots", None)
    alert = Alert.model_validate(webhook("firing")["alerts"][0])
    incident = Incident(id="c1", received_at="2026-10-16T00:00:00Z", alert=alert)

    class Hangs:
        async def investigate(self, alert):
            await asyncio.Event().wait()

    async def go():
        task = asyncio.create_task(aoce.app.run_investigation(incident, Hangs()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert incident.status == "failed"
