import time

import pytest
from fastapi.testclient import TestClient

import aoce.app
from aoce.app import INCIDENTS, app
from aoce.audit import AuditEntry
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


AUDIT_ENTRY = AuditEntry(
    at="2026-10-13T08:01:00Z", incident_id="x", actor="agent", tool="echo", arguments={},
    duration_ms=1, outcome="ok", result_chars=2,
)


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
    assert {(i["alertname"], i["service"]) for i in incidents} == {("HighErrorRate", "product-catalog")}


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


def test_resent_alert_reopens_after_a_failed_investigation(monkeypatch):
    monkeypatch.setattr("aoce.app.make_investigator", lambda: None)
    first = client.post("/alerts", json=webhook("firing")).json()["incidents"][0]
    INCIDENTS.get(first).status = "failed"  # not retryable: a re-send is still a duplicate
    assert client.post("/alerts", json=webhook("firing")).json()["incidents"] == []
    INCIDENTS.get(first).retryable = True  # e.g. a 529 or a network error
    INCIDENTS.get(first).audit.append(AUDIT_ENTRY)
    retry = client.post("/alerts", json=webhook("firing")).json()["incidents"]
    assert len(retry) == 1 and retry[0] != first
    assert INCIDENTS.get(first) is None and INCIDENTS.get(retry[0]).attempts == 2  # replaced, not piled up
    assert INCIDENTS.get(retry[0]).audit == [AUDIT_ENTRY]  # the failed attempt's calls carry over
    INCIDENTS.get(retry[0]).status, INCIDENTS.get(retry[0]).retryable = "failed", True
    third = client.post("/alerts", json=webhook("firing")).json()["incidents"][0]
    INCIDENTS.get(third).status, INCIDENTS.get(third).retryable = "failed", True
    assert client.post("/alerts", json=webhook("firing")).json()["incidents"] == []  # 3 attempts used
    assert len(INCIDENTS) == 1
    assert client.post("/alerts", json=webhook("firing")).json()["incidents"] == []


def test_alerts_beyond_the_queue_bound_get_a_503_so_alertmanager_retries(monkeypatch):
    import asyncio

    class Hangs:
        async def investigate(self, alert):
            await asyncio.Event().wait()

        async def aclose(self):
            pass

    monkeypatch.setattr("aoce.app.make_investigator", lambda: Hangs())
    monkeypatch.setenv("AOCE_MAX_CONCURRENT_INVESTIGATIONS", "1")
    monkeypatch.setenv("AOCE_MAX_QUEUED_INVESTIGATIONS", "1")
    get_settings.cache_clear()
    with TestClient(app, headers=AUTH) as c:
        payload = webhook("firing", "firing", "firing")
        for n, alert in enumerate(payload["alerts"]):
            alert["fingerprint"] = f"q{n}"
        resp = c.post("/alerts", json=payload)
        assert resp.status_code == 503  # so Alertmanager retries with backoff
        assert len(resp.json()["incidents"]) == 2 and resp.json()["dropped"] == 1
        assert len(c.get("/incidents").json()) == 2  # the third was dropped, not stored
        retry = c.post("/alerts", json=payload)  # the two already open dedup; the third is still full
        assert retry.status_code == 503 and retry.json() == {"incidents": [], "dropped": 1}
    get_settings.cache_clear()


def test_a_retry_dropped_by_a_full_queue_keeps_the_failed_incident(monkeypatch):
    import asyncio

    class Hangs:
        async def investigate(self, alert):
            await asyncio.Event().wait()

        async def aclose(self):
            pass

    monkeypatch.setattr("aoce.app.make_investigator", lambda: Hangs())
    monkeypatch.setenv("AOCE_MAX_CONCURRENT_INVESTIGATIONS", "1")
    monkeypatch.setenv("AOCE_MAX_QUEUED_INVESTIGATIONS", "0")
    get_settings.cache_clear()
    with TestClient(app, headers=AUTH) as c:
        failed_alert = webhook("firing")
        failed_alert["alerts"][0]["fingerprint"] = "f"
        failed_id = c.post("/alerts", json=failed_alert).json()["incidents"][0]
        INCIDENTS.get(failed_id).status, INCIDENTS.get(failed_id).retryable = "failed", True
        INCIDENTS.get(failed_id).attempts = 2
        busy = webhook("firing")
        busy["alerts"][0]["fingerprint"] = "busy"
        c.post("/alerts", json=busy)  # takes the only slot
        assert c.post("/alerts", json=failed_alert).status_code == 503
        assert INCIDENTS.get(failed_id).attempts == 2  # still there, attempts not reset
    get_settings.cache_clear()


def test_shutdown_cancels_running_investigations_before_closing_the_client(monkeypatch):
    import asyncio

    events = []

    class Hangs:
        async def investigate(self, alert):
            events.append("started")
            await asyncio.Event().wait()

        async def aclose(self):
            events.append("closed")

    monkeypatch.setattr("aoce.app.make_investigator", lambda: Hangs())
    with TestClient(app, headers=AUTH) as c:
        incident_id = c.post("/alerts", json=webhook("firing")).json()["incidents"][0]
        for _ in range(250):
            if events:
                break
            time.sleep(0.02)
    assert events == ["started", "closed"]
    assert INCIDENTS.get(incident_id).status == "failed"
    assert INCIDENTS.get(incident_id).error.startswith("cancelled")


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
    with TestClient(app, headers=AUTH) as c:
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

    with TestClient(app, headers=AUTH) as c:
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


def test_a_negative_queue_bound_fails_at_startup(monkeypatch):
    from pydantic import ValidationError

    from aoce.config import Settings

    monkeypatch.setenv("AOCE_MAX_QUEUED_INVESTIGATIONS", "-1")
    with pytest.raises(ValidationError):
        Settings()


def test_the_incident_exposes_an_audit_entry_per_tool_call(monkeypatch):
    from test_investigation import (
        REPORT,
        FakeLLM,
        registry,
        response,
        settings,
        tool_use,
    )

    from aoce.investigation import FINISH, Investigator

    llm = FakeLLM(response(tool_use("echo", {"x": "a", "hypothesis": "h"})), response(tool_use(FINISH, REPORT)))
    monkeypatch.setattr("aoce.app.make_investigator", lambda: Investigator(llm, registry(), settings()))
    with TestClient(app, headers=AUTH) as c:
        incident_id = c.post("/alerts", json=webhook("firing")).json()["incidents"][0]
        for _ in range(250):
            body = c.get(f"/incidents/{incident_id}").json()
            if body["status"] == "concluded":
                break
            time.sleep(0.02)
    [entry] = body["audit"]
    assert entry["incident_id"] == incident_id and entry["actor"] == "agent"
    assert entry["tool"] == "echo" and entry["arguments"] == {"x": "a"} and entry["outcome"] == "ok"


# AOCE-69 edge cases.


def echo_registry(delay: float = 0.0):
    import asyncio

    from aoce.tools import Tool, ToolError, ToolInputError, ToolRegistry

    async def echo(tool_input):
        await asyncio.sleep(delay)
        if tool_input["x"] == "bad":
            raise ToolInputError("x must not be bad")
        if tool_input["x"] == "down":
            raise ToolError("backend down")
        return {"x": tool_input["x"]}

    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}
    return ToolRegistry([Tool("echo", "echo", schema, echo)])


@pytest.mark.parametrize(
    ("tool_input", "code", "detail"),
    [
        ({"x": "a"}, 200, None),
        ({"x": 1}, 422, "input.x: expected string"),
        ({}, 422, "input.x: required"),
        ({"x": "a", "y": 1}, 422, "input.y: unexpected property"),
        ({"x": "bad"}, 422, "x must not be bad"),
        ({"x": "down"}, 502, "backend down"),
    ],
)
def test_tool_endpoint_answers_4xx_for_bad_arguments_and_502_for_backends(monkeypatch, tool_input, code, detail):
    monkeypatch.setattr("aoce.app.get_tools", echo_registry)
    resp = client.post("/tools/echo", json=tool_input)
    assert resp.status_code == code
    if detail:
        assert detail in resp.json()["detail"]


def test_tool_endpoint_times_out_with_504(monkeypatch):
    monkeypatch.setattr("aoce.app.get_tools", lambda: echo_registry(delay=1))
    monkeypatch.setenv("AOCE_TOOL_TIMEOUT_S", "0.05")
    get_settings.cache_clear()
    resp = client.post("/tools/echo", json={"x": "a"})
    assert resp.status_code == 504 and "timed out" in resp.json()["detail"]


def test_the_tool_registry_follows_the_current_settings(monkeypatch):
    first = aoce.app.get_tools()
    assert aoce.app.get_tools() is first
    monkeypatch.setenv("AOCE_PROMETHEUS_URL", "http://elsewhere:9090")
    get_settings.cache_clear()
    assert aoce.app.get_tools() is not first


def test_incidents_list_newest_summaries_up_to_limit(monkeypatch):
    monkeypatch.setattr("aoce.app.make_investigator", lambda: None)
    ids = client.post("/alerts", json=webhook("firing", "firing", "firing")).json()["incidents"]
    listed = client.get("/incidents", params={"limit": 2}).json()
    assert [i["id"] for i in listed] == ids[1:]
    assert set(listed[0]) == {"id", "received_at", "alertname", "service", "status", "root_cause", "attempts"}
    assert client.get("/incidents", params={"limit": 0}).status_code == 422


def test_a_crashed_investigation_says_why(monkeypatch):
    class Crashes:
        async def investigate(self, alert):
            raise RuntimeError("bug at postgresql://u:pw@db")

        async def aclose(self):
            pass

    monkeypatch.setattr("aoce.app.make_investigator", lambda: Crashes())
    with TestClient(app, headers=AUTH) as c:
        incident_id = c.post("/alerts", json=webhook("firing")).json()["incidents"][0]
        for _ in range(250):
            body = c.get(f"/incidents/{incident_id}").json()
            if body["status"] == "failed":
                break
            time.sleep(0.02)
    assert body["status"] == "failed" and body["error"] == "crashed: RuntimeError (details in the agent log)"


def test_a_blank_anthropic_key_skips_investigation(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    assert aoce.app.make_investigator() is None
    incident_id = client.post("/alerts", json=webhook("firing")).json()["incidents"][0]
    assert INCIDENTS.get(incident_id).status == "skipped"


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_secrets_count_as_unset(monkeypatch, value):
    from aoce.config import Settings

    for var in ("ANTHROPIC_API_KEY", "AOCE_WEBHOOK_TOKEN", "AOCE_API_KEY", "AOCE_DATABASE_URL", "AOCE_GITHUB_TOKEN"):
        monkeypatch.setenv(var, value)
    s = Settings()
    assert (s.anthropic_api_key, s.webhook_token, s.api_key, s.database_url, s.github_token) == (None,) * 5


def test_secrets_are_kept_out_of_reprs_and_dumps(monkeypatch):
    from aoce.config import Settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("AOCE_DATABASE_URL", "postgresql://u:db-secret@db/app")
    monkeypatch.setenv("AOCE_GITHUB_TOKEN", "ghp-secret")
    s = Settings()
    for secret in ("sk-ant-secret", "db-secret", "ghp-secret"):
        assert secret not in repr(s) and secret not in str(s.model_dump())
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-secret"
