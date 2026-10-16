import pytest
from fastapi.testclient import TestClient

from aoce.app import INCIDENTS, app
from aoce.config import get_settings
from test_app import webhook

client = TestClient(app)
HOOK = {"Authorization": "Bearer hook-secret"}
KEY = {"Authorization": "Bearer key-secret"}


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setattr("aoce.app.make_investigator", lambda: None)
    monkeypatch.setenv("AOCE_WEBHOOK_TOKEN", "hook-secret")
    monkeypatch.setenv("AOCE_API_KEY", "key-secret")
    get_settings.cache_clear()
    INCIDENTS.clear()
    yield
    get_settings.cache_clear()
    INCIDENTS.clear()


@pytest.fixture
def no_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("AOCE_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("AOCE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # away from a developer's .env, so "unset" really is unset
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_healthz_needs_no_token():
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, KEY])
def test_alerts_reject_anything_but_the_webhook_token(secrets, headers):
    resp = client.post("/alerts", json=webhook("firing"), headers=headers)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert len(INCIDENTS) == 0


def test_alerts_accept_the_webhook_token(secrets):
    assert client.post("/alerts", json=webhook("firing"), headers=HOOK).status_code == 202


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/incidents"), ("GET", "/incidents/x"), ("GET", "/tools"), ("POST", "/tools/db_diagnostics"),
])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, HOOK])
def test_read_endpoints_reject_anything_but_the_api_key(secrets, method, path, headers):
    assert client.request(method, path, json={}, headers=headers).status_code == 401


def test_read_endpoints_accept_the_api_key(secrets):
    incident_id = client.post("/alerts", json=webhook("firing"), headers=HOOK).json()["incidents"][0]
    assert client.get(f"/incidents/{incident_id}", headers=KEY).status_code == 200
    assert client.get("/tools", headers=KEY).status_code == 200


@pytest.mark.parametrize(("method", "path"), [("POST", "/alerts"), ("GET", "/incidents"), ("POST", "/tools/x")])
def test_endpoints_are_off_when_their_secret_is_unset(no_secrets, method, path):
    resp = client.request(method, path, json={}, headers={"Authorization": "Bearer "})
    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"]
