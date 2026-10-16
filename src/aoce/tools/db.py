"""db_diagnostics: read-only Postgres health checks (AOCE-57).

Missing indexes, slow queries and lock contention were the root cause in 3 of 6 discovery
interviews, and on-call engineers often lack DB access at night. This tool connects as a monitoring
role (pg_monitor in the OTel Demo), forces every transaction read-only with a statement timeout, and
never kills sessions or writes.
"""

from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from aoce.tools import Tool, ToolError

STATEMENT_TIMEOUT_MS = 5000
LARGE_TABLE_ROWS = 1000

SLOW_QUERIES_SQL = """
SELECT s.queryid::text AS queryid, s.calls,
       round(s.total_exec_time::numeric, 1) AS total_ms,
       round(s.mean_exec_time::numeric, 2) AS mean_ms,
       round(s.max_exec_time::numeric, 1) AS max_ms,
       s.rows,
       left(regexp_replace(s.query, '\\s+', ' ', 'g'), 300) AS query
FROM pg_stat_statements s
JOIN pg_database d ON d.oid = s.dbid
WHERE d.datname = current_database()
ORDER BY s.total_exec_time DESC
LIMIT %(limit)s
"""

BLOCKED_SQL = """
SELECT a.pid, a.usename, a.application_name, a.state, a.wait_event_type, a.wait_event,
       pg_blocking_pids(a.pid) AS blocked_by,
       round(extract(epoch FROM now() - a.query_start)::numeric, 1) AS waiting_s,
       left(regexp_replace(a.query, '\\s+', ' ', 'g'), 300) AS query
FROM pg_stat_activity a
WHERE cardinality(pg_blocking_pids(a.pid)) > 0
ORDER BY a.query_start
LIMIT %(limit)s
"""

BLOCKERS_SQL = """
SELECT a.pid, a.usename, a.application_name, a.state,
       round(extract(epoch FROM now() - a.xact_start)::numeric, 1) AS xact_age_s,
       left(regexp_replace(a.query, '\\s+', ' ', 'g'), 300) AS last_query,
       (SELECT array_agg(DISTINCT l.mode || ' on ' || coalesce(l.relation::regclass::text, l.locktype))
          FROM pg_locks l WHERE l.pid = a.pid AND l.granted) AS locks_held
FROM pg_stat_activity a
WHERE a.pid = ANY(%(pids)s)
"""

LONG_TRANSACTIONS_SQL = """
SELECT a.pid, a.usename, a.application_name, a.state,
       round(extract(epoch FROM now() - a.xact_start)::numeric, 1) AS xact_age_s,
       left(regexp_replace(a.query, '\\s+', ' ', 'g'), 300) AS last_query
FROM pg_stat_activity a
WHERE a.xact_start < now() - interval '30 seconds'
  AND a.backend_type = 'client backend'
  AND a.pid <> pg_backend_pid()
ORDER BY a.xact_start
LIMIT %(limit)s
"""

SEQ_SCANS_SQL = """
SELECT schemaname || '.' || relname AS table_name, seq_scan, seq_tup_read, idx_scan, n_live_tup,
       CASE WHEN seq_scan > 0 THEN seq_tup_read / seq_scan END AS avg_rows_per_seq_scan
FROM pg_stat_user_tables
WHERE seq_scan > 0 AND n_live_tup >= %(min_rows)s
ORDER BY seq_tup_read DESC
LIMIT %(limit)s
"""

CONNECTIONS_SQL = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE state = 'active') AS active,
       count(*) FILTER (WHERE state = 'idle') AS idle,
       count(*) FILTER (WHERE state LIKE 'idle in transaction%%') AS idle_in_transaction,
       current_setting('max_connections')::int AS max_connections
FROM pg_stat_activity
WHERE backend_type = 'client backend'
"""

CONNECTIONS_BY_APP_SQL = """
SELECT coalesce(nullif(application_name, ''), usename) AS client, count(*) AS connections
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY 1
ORDER BY 2 DESC
LIMIT %(limit)s
"""

CHECKS = ["slow_queries", "blocking", "seq_scans", "connections"]

DESCRIPTION = """\
Read-only diagnostics on the application's Postgres database. Checks:
- slow_queries: top statements by total execution time from pg_stat_statements (cumulative since the \
last stats reset, not only the alert window; compare mean_ms and max_ms).
- blocking: sessions waiting on locks, who blocks them, the locks the blockers hold, and transactions \
open longer than 30 seconds. Use this first when a service backed by the database gets slow or times out.
- seq_scans: tables with at least 1000 rows read by sequential scans, a sign of a missing index.
- connections: connection count by state and by client versus max_connections.
- overview: all of the above in one call.
Returns rows plus the SQL that produced them so you can cite it."""

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "check": {"type": "string", "enum": ["overview", *CHECKS], "description": "Which diagnostic to run."},
        "limit": {"type": "integer", "description": "Max rows per check, 1-20."},
    },
    "required": ["check", "limit"],
    "additionalProperties": False,
}


def _clean(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()} for row in rows]


async def _fetch(conn: psycopg.AsyncConnection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return _clean(await cur.fetchall())


async def _slow_queries(conn, limit: int) -> dict[str, Any]:
    try:
        rows = await _fetch(conn, SLOW_QUERIES_SQL, {"limit": limit})
    except psycopg.errors.UndefinedTable:
        return {"error": "pg_stat_statements is not installed in this database", "sql": SLOW_QUERIES_SQL.strip()}
    return {"rows": rows, "sql": SLOW_QUERIES_SQL.strip()}


async def _blocking(conn, limit: int) -> dict[str, Any]:
    blocked = await _fetch(conn, BLOCKED_SQL, {"limit": limit})
    pids = sorted({pid for row in blocked for pid in row["blocked_by"]})
    blockers = await _fetch(conn, BLOCKERS_SQL, {"pids": pids}) if pids else []
    long_tx = await _fetch(conn, LONG_TRANSACTIONS_SQL, {"limit": limit})
    return {
        "blocked_sessions": blocked,
        "blockers": blockers,
        "long_transactions": long_tx,
        "sql": [BLOCKED_SQL.strip(), BLOCKERS_SQL.strip(), LONG_TRANSACTIONS_SQL.strip()],
    }


async def _seq_scans(conn, limit: int) -> dict[str, Any]:
    rows = await _fetch(conn, SEQ_SCANS_SQL, {"limit": limit, "min_rows": LARGE_TABLE_ROWS})
    return {"rows": rows, "sql": SEQ_SCANS_SQL.strip()}


async def _connections(conn, limit: int) -> dict[str, Any]:
    summary = (await _fetch(conn, CONNECTIONS_SQL, {}))[0]
    by_client = await _fetch(conn, CONNECTIONS_BY_APP_SQL, {"limit": limit})
    summary["usage_ratio"] = round(summary["total"] / summary["max_connections"], 3)
    return {"summary": summary, "by_client": by_client, "sql": [CONNECTIONS_SQL.strip(), CONNECTIONS_BY_APP_SQL.strip()]}


RUNNERS = {"slow_queries": _slow_queries, "blocking": _blocking, "seq_scans": _seq_scans, "connections": _connections}


def db_diagnostics_tool(settings) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if not settings.database_url:
            raise ToolError("no database configured (set AOCE_DATABASE_URL)")
        check = args["check"]
        if check != "overview" and check not in RUNNERS:
            raise ToolError(f"unknown check: {check}")
        limit = min(max(int(args.get("limit") or 10), 1), 20)
        try:
            conn = await psycopg.AsyncConnection.connect(
                settings.database_url,
                autocommit=True,
                connect_timeout=5,
                options=f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}",
            )
        except psycopg.OperationalError as e:
            raise ToolError(f"cannot connect to the database: {str(e).strip()}") from e

        async with conn:
            try:
                checks = CHECKS if check == "overview" else [check]
                results = {name: await RUNNERS[name](conn, limit) for name in checks}
            except psycopg.Error as e:
                raise ToolError(f"database query failed: {str(e).strip()}") from e
            info = conn.info
            return {
                "database": {"host": info.host, "dbname": info.dbname, "user": info.user},
                "checked_at_server_time": (await _fetch(conn, "SELECT now() AS now", {}))[0]["now"],
                **results,
            }

    return Tool("db_diagnostics", DESCRIPTION, INPUT_SCHEMA, handler)
