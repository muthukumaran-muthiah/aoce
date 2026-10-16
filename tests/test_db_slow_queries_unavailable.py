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
