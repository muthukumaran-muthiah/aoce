from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from aoce.alerts import Alert
from aoce.app import INCIDENTS, Incident, app
from aoce.audit import AuditEntry
from aoce.config import get_settings
from aoce.investigation import InvestigationResult
from aoce.postmortem import render_postmortem

AUTH = {"Authorization": "Bearer test-token"}
client = TestClient(app, headers=AUTH)

REPORT = {
    "summary": "Product catalog queries\nwait on a table lock.",
    "root_cause": "A long transaction holds a lock on products.",
    "confidence": "high",
    "hypotheses": [
        {"statement": "Lock contention in Postgres", "status": "confirmed", "evidence": ["db_diagnostics: 12 waiters"]},
        {"statement": "Bad deploy", "status": "rejected", "evidence": []},
    ],
    "recommended_action": "Terminate the blocking backend and disable productCatalogLockContention.",
    "checked": ["db_diagnostics", "query_metrics"],
    "not_checked": ["get_traces"],
}


def make_incident(
    status="concluded", report=REPORT, resolved_at=None, starts_at="2026-10-13T08:00:00Z", **extra
) -> Incident:
    alert = Alert.model_validate({
        "status": "firing",
        "labels": {"alertname": "HighErrorRate", "service_name": "product-catalog", "severity": "page"},
        "annotations": {"summary": "error rate above 5%"},
        "startsAt": starts_at,
        "endsAt": "2026-10-13T08:05:00Z",  # a firing alert's resolve-timeout, not an end
        "fingerprint": "fp",
    })
    investigation = None
    if status in ("concluded", "inconclusive", "failed"):
        investigation = InvestigationResult(
            status=status, stop_reason="finished", report=report, steps=3, tokens_used=1234, **extra
        )
    return Incident(
        id="abc123",
        received_at=datetime(2026, 10, 13, 8, 0, 30, tzinfo=UTC),
        alert=alert,
        resolved_at=resolved_at,
        status=status,
        investigation=investigation,
        audit=[
            AuditEntry(
                at="2026-10-13T08:01:00Z", incident_id="abc123", actor="agent", tool="db_diagnostics",
                arguments={}, duration_ms=40, outcome="ok", result_chars=10,
            ),
            AuditEntry(
                at="2026-10-13T08:00:45Z", incident_id="abc123", actor="agent", tool="query_metrics",
                arguments={}, duration_ms=12, outcome="error", result_chars=5,
            ),
        ],
    )


def test_concluded_incident_has_every_section():
    md = render_postmortem(make_incident())
    assert md.startswith("# Postmortem: HighErrorRate on product-catalog\n")
    for heading in ("## Summary", "## Impact", "## Timeline", "## Root cause", "## Hypotheses", "## Action items"):
        assert heading in md
    assert "Product catalog queries wait on a table lock." in md  # newlines folded
    assert "A long transaction holds a lock on products. (confidence: high)" in md
    assert "- **confirmed**: Lock contention in Postgres\n  - db_diagnostics: 12 waiters" in md
    assert "- [ ] Terminate the blocking backend" in md
    assert "- [ ] Check get_traces, which the agent did not." in md
    assert "severity page" in md
    assert "Resolved: not recorded" in md  # no resolved webhook, whatever the firing endsAt says
    assert "Alert resolved." not in md


def test_timeline_is_in_time_order_and_marks_failed_calls():
    md = render_postmortem(make_incident(resolved_at="2026-10-13T08:20:00Z"))
    timeline = md.split("## Timeline\n\n")[1].split("\n\n")[0].splitlines()
    assert [line.split(": ", 1)[1] for line in timeline] == [
        "Alert started firing.",
        "Agent received the alert.",
        "Ran `query_metrics` in 12 ms (error).",
        "Ran `db_diagnostics` in 40 ms.",
        "Alert resolved.",
    ]
    assert "(about 20 min)" in md


def test_webhook_times_are_shown_in_utc():
    md = render_postmortem(make_incident(starts_at="2026-10-13T08:00:00", resolved_at="2026-10-13T10:20:00+02:00"))
    assert "Started: 2026-10-13 08:00:00 UTC" in md  # no offset: taken as UTC
    assert "Resolved: 2026-10-13 08:20:00 UTC (about 20 min)" in md


def test_fallback_report_says_no_root_cause_was_found():
    report = {**REPORT, "root_cause": None, "recommended_action": None, "confidence": "low", "fallback": True}
    md = render_postmortem(make_incident(status="inconclusive", report=report))
    assert "assembled from the agent's tool calls" in md
    assert "## Root cause\n\nNot established." in md
    assert "- [ ] Find the root cause by hand" in md


def test_failed_and_skipped_incidents_still_render():
    failed = render_postmortem(make_incident(status="failed", report=None, error="401 invalid x-api-key"))
    assert "No summary was produced." in failed
    assert "401 invalid x-api-key" in failed
    skipped = render_postmortem(make_incident(status="skipped"))
    assert "not investigated" in skipped
    assert "## Investigation" not in skipped


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("AOCE_API_KEY", "test-token")
    get_settings.cache_clear()
    INCIDENTS.clear()
    yield
    INCIDENTS.clear()
    get_settings.cache_clear()


def test_endpoint_returns_markdown(api):
    INCIDENTS.add(make_incident(), 10)
    resp = client.get("/incidents/abc123/postmortem")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.startswith("# Postmortem: HighErrorRate")


def test_a_resolved_webhook_sets_the_end_time(api, monkeypatch):
    monkeypatch.setenv("AOCE_WEBHOOK_TOKEN", "test-token")
    get_settings.cache_clear()
    INCIDENTS.add(make_incident(), 10)
    resolved = {
        "groupKey": "g", "status": "resolved", "receiver": "aoce",
        "alerts": [{
            "status": "resolved", "labels": {}, "startsAt": "2026-10-13T08:00:00Z",
            "endsAt": "2026-10-13T08:20:00Z", "fingerprint": "fp",
        }],
    }
    assert client.post("/alerts", json=resolved).status_code == 202
    resolved["alerts"][0]["endsAt"] = "2026-10-13T09:00:00Z"  # a re-delivery keeps the first end time
    assert client.post("/alerts", json=resolved).status_code == 202
    md = client.get("/incidents/abc123/postmortem").text
    assert "Resolved: 2026-10-13 08:20:00 UTC (about 20 min)" in md


def test_endpoint_refuses_an_unfinished_incident(api):
    incident = make_incident()
    incident.status = "investigating"
    INCIDENTS.add(incident, 10)
    assert client.get("/incidents/abc123/postmortem").status_code == 409


def test_endpoint_unknown_incident_and_auth(api):
    assert client.get("/incidents/nope/postmortem").status_code == 404
    INCIDENTS.add(make_incident(), 10)
    assert TestClient(app).get("/incidents/abc123/postmortem").status_code == 401
