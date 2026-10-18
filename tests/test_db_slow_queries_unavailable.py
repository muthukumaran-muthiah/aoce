"""db_diagnostics when pg_stat_statements is missing or not preloaded (AOCE-63).

CI's Postgres preloads the extension, so these fake the database errors instead of needing a server.
"""

import json

import psycopg
import pytest

from aoce.config import Settings
from aoce.tools import ToolRegistry
from aoce.tools import db


class FakeConn:
    info = type("Info", (), {"host": "db", "dbname": "app", "user": "monitor"})()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def fake_db(monkeypatch, request):
    error = request.param

    async def connect(*args, **kwargs):
        return FakeConn()

    async def fetch(conn, sql, params):
        if sql is db.SLOW_QUERIES_SQL:
            raise error("pg_stat_statements unavailable")
        if sql is db.CONNECTIONS_SQL:
            return [{"total": 5, "max_connections": 100}]
        if sql == "SELECT now() AS now":
            return [{"now": "2026-10-16T00:00:00Z"}]
        return []

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    monkeypatch.setattr(db, "_fetch", fetch)


@pytest.mark.parametrize(
    "fake_db",
    [psycopg.errors.UndefinedTable, psycopg.errors.ObjectNotInPrerequisiteState],
    indirect=True,
)
async def test_overview_reports_slow_queries_unavailable_and_runs_other_checks(fake_db):
    registry = ToolRegistry([db.db_diagnostics_tool(Settings(database_url="postgresql://x"))])
    content, is_error = await registry.run("db_diagnostics", {"check": "overview", "limit": 5})

    assert not is_error, content
    out = json.loads(content)
    assert out["slow_queries"]["error"].startswith("slow queries unavailable:")
    assert out["blocking"]["blocked_sessions"] == []
    assert out["seq_scans"]["rows"] == []
    assert out["connections"]["summary"]["usage_ratio"] == 0.05


async def test_a_failure_reading_the_server_time_is_a_tool_error(monkeypatch):
    async def connect(*args, **kwargs):
        return FakeConn()

    async def fetch(conn, sql, params):
        if sql == "SELECT now() AS now":
            raise psycopg.OperationalError("server closed the connection")
        return [{"total": 5, "max_connections": 100}] if sql is db.CONNECTIONS_SQL else []

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    monkeypatch.setattr(db, "_fetch", fetch)
    registry = ToolRegistry([db.db_diagnostics_tool(Settings(database_url="postgresql://x"))])
    content, is_error = await registry.run("db_diagnostics", {"check": "connections", "limit": 5})
    assert is_error and content == "database query failed: server closed the connection"


async def test_database_errors_are_redacted_before_the_model_sees_them(monkeypatch):
    async def connect(*args, **kwargs):
        raise psycopg.OperationalError("connection failed for jane@example.com: password=hunter2")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    registry = ToolRegistry([db.db_diagnostics_tool(Settings(database_url="postgresql://x"))])
    content, is_error = await registry.run("db_diagnostics", {"check": "connections", "limit": 5})
    assert is_error and content.startswith("cannot connect to the database:")
    assert "jane@example.com" not in content and "hunter2" not in content
