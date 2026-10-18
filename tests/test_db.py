"""db_diagnostics against a real Postgres with pg_stat_statements.

Needs a superuser URL in AOCE_TEST_ADMIN_URL (CI starts one in Docker); skipped otherwise.
The fixture recreates the parts of the OTel Demo's init.sql the tool relies on: a pg_monitor role
and the catalog.products table that the productCatalogLockContention flag locks.
"""

import asyncio
import json
import os

import psycopg
import pytest

from aoce.config import Settings
from aoce.tools import ToolRegistry
from aoce.tools import db
from aoce.tools.db import db_diagnostics_tool

ADMIN_URL = os.environ.get("AOCE_TEST_ADMIN_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="AOCE_TEST_ADMIN_URL not set")

DB = "aoce_test"


def url_for(dbname: str, **overrides: str) -> str:
    return psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=dbname, **overrides)


@pytest.fixture(scope="module")
def monitoring_url():
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DB}")
        conn.execute("DROP ROLE IF EXISTS monitoring_user")
        conn.execute("CREATE ROLE monitoring_user LOGIN PASSWORD 'monitoring_password' IN ROLE pg_monitor")
    admin_db = url_for(DB)
    with psycopg.connect(admin_db, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        conn.execute("CREATE SCHEMA catalog")
        conn.execute("CREATE TABLE catalog.products (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO catalog.products VALUES ('OLJCESPC7Z', 'Explorascope')")
        conn.execute("CREATE TABLE catalog.big (id INT, payload TEXT)")
        conn.execute("INSERT INTO catalog.big SELECT g, md5(g::text) FROM generate_series(1, 5000) g")
        conn.execute("ANALYZE catalog.big")
        # pg_stat_statements keeps some utility statements verbatim, literals included.
        conn.execute("COMMENT ON TABLE catalog.big IS 'owner jane@example.com'")
        for _ in range(3):
            conn.execute("SELECT count(*) FROM catalog.big WHERE payload LIKE 'ab%'")
    yield url_for(DB, user="monitoring_user", password="monitoring_password"), admin_db


def registry(url: str) -> ToolRegistry:
    return ToolRegistry([db_diagnostics_tool(Settings(database_url=url))])


async def run(url: str, check: str, limit: int = 10) -> dict:
    content, is_error = await registry(url).run("db_diagnostics", {"check": check, "limit": limit})
    assert not is_error, content
    return json.loads(content)


async def test_overview_runs_every_check(monitoring_url):
    url, _ = monitoring_url
    out = await run(url, "overview")
    assert out["database"] == {"host": out["database"]["host"], "dbname": DB, "user": "monitoring_user"}
    assert any("catalog.big" in r["query"] for r in out["slow_queries"]["rows"])
    assert [r["table_name"] for r in out["seq_scans"]["rows"]] == ["catalog.big"]
    assert out["connections"]["summary"]["max_connections"] > 0
    assert out["blocking"]["blocked_sessions"] == []


async def test_slow_queries_text_has_no_literals(monitoring_url):
    url, _ = monitoring_url
    out = await run(url, "slow_queries", limit=20)
    texts = [r["query"] for r in out["slow_queries"]["rows"]]
    assert any(t.startswith("COMMENT ON TABLE catalog.big") for t in texts)
    assert "jane@example.com" not in json.dumps(out)


async def test_blocking_names_the_lock_holder(monitoring_url):
    """Reproduces productCatalogLockContention: one transaction holds ACCESS EXCLUSIVE on catalog.products."""
    url, admin_db = monitoring_url
    holder = await psycopg.AsyncConnection.connect(admin_db, application_name="lock-holder")
    waiter = await psycopg.AsyncConnection.connect(admin_db, autocommit=True, application_name="product-catalog")
    try:
        await holder.execute("LOCK TABLE catalog.products IN ACCESS EXCLUSIVE MODE")
        waiting = asyncio.create_task(waiter.execute("SELECT * FROM catalog.products WHERE name = 'jane@example.com'"))
        for _ in range(50):
            out = await run(url, "blocking")
            if out["blocking"]["blocked_sessions"]:
                break
            await asyncio.sleep(0.1)

        blocked = out["blocking"]["blocked_sessions"]
        assert [b["application_name"] for b in blocked] == ["product-catalog"]
        assert blocked[0]["query"] == "SELECT * FROM catalog.products WHERE name = $1"
        assert "jane@example.com" not in json.dumps(out)
        (blocker,) = out["blocking"]["blockers"]
        assert blocker["application_name"] == "lock-holder"
        assert "AccessExclusiveLock on catalog.products" in blocker["locks_held"]
    finally:
        await holder.rollback()
        await waiting
        await holder.close()
        await waiter.close()


async def test_every_query_runs_read_only_with_a_timeout(monitoring_url):
    url, _ = monitoring_url
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            await db._fetch(conn, "CREATE TEMP TABLE t (x int)", {})
        assert (await db._fetch(conn, "SHOW statement_timeout", {}))[0]["statement_timeout"] == "5s"
        # SET LOCAL ends with the transaction, so the session itself is untouched.
        assert (await (await conn.execute("SHOW statement_timeout")).fetchone())[0] == "0"


async def test_options_in_the_url_are_kept(monitoring_url):
    """The read-only setup no longer passes `options=`, which replaced the URL's own (AOCE-69)."""
    url, _ = monitoring_url
    seen = []
    real_fetch = db._fetch

    async def fetch(conn, sql, params):
        seen.append((await (await conn.execute("SHOW search_path")).fetchone())[0])
        return await real_fetch(conn, sql, params)

    tool = db.db_diagnostics_tool(Settings(database_url=url_for(DB, user="monitoring_user",
                                  password="monitoring_password", options="-c search_path=catalog")))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(db, "_fetch", fetch)
        content, is_error = await ToolRegistry([tool]).run("db_diagnostics", {"check": "connections", "limit": 5})
    assert not is_error, content
    assert set(seen) == {"catalog"}


async def test_activity_in_other_databases_is_left_out(monitoring_url):
    url, _ = monitoring_url
    other = await psycopg.AsyncConnection.connect(ADMIN_URL, application_name="other-db-client")
    try:
        await other.execute("SELECT 1")  # leaves the transaction open: idle in transaction
        out = await run(url, "connections")
        assert "other-db-client" not in [c["client"] for c in out["connections"]["by_client"]]
        summary = out["connections"]["summary"]
        assert summary["total"] > summary["in_this_database"] >= 1
        assert summary["idle_in_transaction"] >= 1
        assert "%%" not in json.dumps(out["connections"]["sql"])
    finally:
        await other.rollback()
        await other.close()


async def test_unreachable_database_is_a_tool_error():
    content, is_error = await registry("postgresql://nobody@127.0.0.1:1/x").run(
        "db_diagnostics", {"check": "connections", "limit": 5}
    )
    assert is_error and "cannot connect" in content


async def test_missing_database_url_is_a_tool_error():
    content, is_error = await registry(None).run("db_diagnostics", {"check": "connections", "limit": 5})
    assert is_error and "AOCE_DATABASE_URL" in content
