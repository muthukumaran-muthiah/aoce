"""get_traces: slow or failing traces for a service from Jaeger, summarised span by span (AOCE-27).

Metrics say which service is unhealthy; a trace says where the time went or which hop failed first.
Raw traces are far too large for the model (a checkout trace has hundreds of spans), so each trace is
reduced to its root, its failing spans and the spans with the most self time (time not spent waiting
on a child), and the operations that fail or eat time are also ranked across all returned traces.

Span attributes can hold user data (SQL literals, URLs with query strings, exception messages), so
only a short allow-list of attributes is kept, SQL goes through redact_query (AOCE-39), other text
(status descriptions, exception messages) has secrets, e-mails and quoted values masked, and every
string is truncated.
"""

import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from aoce.tools import Tool, ToolError
from aoce.tools.db import _query_text
from aoce.tools.logs import MASK, redact_log_text
from aoce.tools.metrics import _parse_end

MAX_TRACES = 20
# Traces fetched when looking for the slowest ones without a duration floor; Jaeger returns the most
# recent matches, so we fetch a wider sample and keep the slowest.
SLOW_SAMPLE = 100
TOP_SPANS = 5
TOP_OPERATIONS = 10
TEXT_CHARS = 200
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# Span attributes worth showing; everything else is dropped.
KEEP_TAGS = {
    "http.request.method",
    "http.method",
    "http.response.status_code",
    "http.status_code",
    "http.route",
    "rpc.method",
    "rpc.grpc.status_code",
    "db.system",
    "db.system.name",
    "db.operation",
    "db.operation.name",
    "db.statement",
    "db.query.text",
    "messaging.system",
    "messaging.destination.name",
    "otel.status_description",
    "error.type",
}
SQL_TAGS = {"db.statement", "db.query.text"}

DESCRIPTION = """\
Fetch recent traces for a service from Jaeger and summarise them. Use kind "error" for traces with a \
failed span, or "slow" for the slowest traces (optionally only those longer than min_duration_ms). \
For each trace you get its root operation, total duration, the services it crossed, its failing \
spans (with status and exception message) and the spans with the most self time, i.e. time not \
spent waiting on a child call, which is where the latency actually is. Across all traces, operations \
are ranked by error count and by total self time. Every trace has its trace ID and a Jaeger link so \
you can cite it.
Use it after query_metrics has pointed at a service, to find which downstream call or operation is \
responsible."""

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "service": {
            "type": "string",
            "description": "service.name as shown in Jaeger, e.g. 'product-catalog'.",
        },
        "kind": {
            "type": "string",
            "enum": ["error", "slow"],
            "description": "'error' for traces with a failed span, 'slow' for the slowest traces.",
        },
        "operation": {
            "type": ["string", "null"],
            "description": "Only traces containing this span name for the service, e.g. 'oteldemo.ProductCatalogService/GetProduct'. Null for any.",
        },
        "min_duration_ms": {
            "type": ["integer", "null"],
            "description": "Only traces at least this long. Null to take the slowest of the recent traces.",
        },
        "window_minutes": {
            "type": "integer",
            "description": "Search the window of this many minutes ending at `end`, 1-180.",
        },
        "end": {
            "type": ["string", "null"],
            "description": "End of the window as an ISO-8601 timestamp. Null for now.",
        },
        "limit": {
            "type": "integer",
            "description": f"Traces to return, 1-{MAX_TRACES}.",
        },
    },
    "required": ["service", "kind", "operation", "min_duration_ms", "window_minutes", "end", "limit"],
    "additionalProperties": False,
}


def _text(value: Any) -> str:
    s = str(value)
    return s if len(s) <= TEXT_CHARS else s[:TEXT_CHARS] + "..."


# Error text from drivers and frameworks quotes the offending value: `Key (email)=(jane@x.com)`,
# `invalid input syntax for type uuid: "abc"`. Mask quoted and parenthesised values outright.
_QUOTED = re.compile(r""""[^"]*"|'[^']*'|(?<==)\([^)]*\)""")


def _scrub(value: Any) -> str:
    """Mask secrets, e-mails, card numbers and quoted literals in free text, then truncate it."""
    return _text(_QUOTED.sub(MASK, redact_log_text(str(value))))


def _tags(tags: list[dict[str, Any]]) -> dict[str, Any]:
    return {t["key"]: t.get("value") for t in tags or []}


def _is_error(tags: dict[str, Any]) -> bool:
    return tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR"


def _kept_attributes(tags: dict[str, Any]) -> dict[str, Any]:
    kept = {}
    for key in sorted(KEEP_TAGS & tags.keys()):
        value = tags[key]
        if key in SQL_TAGS:
            value = _query_text(str(value))
        elif isinstance(value, str):
            value = _scrub(value)
        kept[key] = value
    return kept


def _exception(span: dict[str, Any]) -> dict[str, Any] | None:
    """The first exception event on the span (Jaeger shows span events as logs)."""
    for log in span.get("logs") or []:
        fields = _tags(log.get("fields"))
        if fields.get("event") == "exception" or "exception.type" in fields:
            return {
                "type": _text(fields.get("exception.type", "")),
                "message": _scrub(fields.get("exception.message", "")),
            }
    return None


def _self_time(span: dict[str, Any], children: list[dict[str, Any]]) -> int:
    """Span duration minus the part covered by its children, in microseconds."""
    start, end = span["startTime"], span["startTime"] + span["duration"]
    covered = 0
    cursor = start
    for c in sorted(children, key=lambda c: c["startTime"]):
        c_start = max(c["startTime"], cursor)
        c_end = min(c["startTime"] + c["duration"], end)
        if c_end > c_start:
            covered += c_end - c_start
            cursor = c_end
    return max(span["duration"] - covered, 0)


def _parent_id(span: dict[str, Any]) -> str | None:
    for ref in span.get("references") or []:
        if ref.get("refType") == "CHILD_OF":
            return ref.get("spanID")
    return None


def summarise_trace(trace: dict[str, Any], trace_url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the trace summary and one row per span (service, operation, self_us, error) for ranking."""
    processes = trace.get("processes") or {}
    spans = trace.get("spans") or []
    by_id = {s["spanID"]: s for s in spans}
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in spans:
        parent = _parent_id(s)
        if parent in by_id:
            children[parent].append(s)

    rows = []
    errors = []
    for s in spans:
        tags = _tags(s.get("tags"))
        service = processes.get(s.get("processID"), {}).get("serviceName", "unknown")
        error = _is_error(tags)
        row = {
            "service": service,
            "operation": s["operationName"],
            "duration_ms": round(s["duration"] / 1000, 2),
            "self_ms": round(_self_time(s, children[s["spanID"]]) / 1000, 2),
            "error": error,
        }
        rows.append(row)
        if error:
            entry = {
                "span_id": s["spanID"],
                "service": service,
                "operation": s["operationName"],
                "duration_ms": row["duration_ms"],
                "attributes": _kept_attributes(tags),
            }
            exception = _exception(s)
            if exception:
                entry["exception"] = exception
            errors.append((s["startTime"], entry))

    roots = [s for s in spans if _parent_id(s) not in by_id] or spans
    root = min(roots, key=lambda s: s["startTime"]) if roots else None
    start = min((s["startTime"] for s in spans), default=0)
    end = max((s["startTime"] + s["duration"] for s in spans), default=0)
    errors.sort(key=lambda e: e[0])
    summary = {
        "trace_id": trace.get("traceID"),
        "start": datetime.fromtimestamp(start / 1e6, UTC).isoformat() if spans else None,
        "duration_ms": round((end - start) / 1000, 2),
        "root": {
            "service": processes.get(root.get("processID"), {}).get("serviceName", "unknown"),
            "operation": root["operationName"],
        }
        if root
        else None,
        "span_count": len(spans),
        "services": sorted({r["service"] for r in rows}),
        # Earliest failure first: it is usually the origin, the rest are callers reporting it.
        "error_spans": [e for _, e in errors[:TOP_SPANS]],
        "error_span_count": len(errors),
        "top_self_time": [
            {k: r[k] for k in ("service", "operation", "self_ms", "duration_ms")}
            for r in sorted(rows, key=lambda r: r["self_ms"], reverse=True)[:TOP_SPANS]
        ],
        "url": trace_url,
    }
    return summary, rows


def rank_operations(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["service"], r["operation"])
        s = stats.setdefault(key, {"service": key[0], "operation": key[1], "spans": 0, "errors": 0, "self_ms": 0.0})
        s["spans"] += 1
        s["errors"] += r["error"]
        s["self_ms"] += r["self_ms"]
    ops = list(stats.values())
    for s in ops:
        s["self_ms"] = round(s["self_ms"], 2)
    return {
        "by_errors": sorted((s for s in ops if s["errors"]), key=lambda s: s["errors"], reverse=True)[:TOP_OPERATIONS],
        "by_self_time": sorted(ops, key=lambda s: s["self_ms"], reverse=True)[:TOP_OPERATIONS],
    }


async def _search(client: httpx.AsyncClient, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        resp = await client.get("/api/traces", params=params)
    except httpx.HTTPError as e:
        # The exception text can carry the URL, and with it any credentials in AOCE_JAEGER_URL.
        raise ToolError(f"Jaeger unreachable: {type(e).__name__}") from e
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    if resp.status_code != 200:
        errors = body.get("errors") or [{"msg": resp.text[:300]}]
        raise ToolError(f"Jaeger error ({resp.status_code}): {errors[0].get('msg')}")
    if "data" not in body:
        raise ToolError(f"Jaeger returned an unexpected body: {resp.text[:300]}")
    return body["data"] or []


def get_traces_tool(settings, transport: httpx.AsyncBaseTransport | None = None) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        service = (args.get("service") or "").strip()
        if not SERVICE_RE.match(service):
            raise ToolError(f"invalid service name: {service!r}")
        kind = args.get("kind")
        if kind not in ("error", "slow"):
            raise ToolError(f"unknown kind: {kind!r}")
        operation = args.get("operation") or None
        try:
            min_duration_ms = None if args.get("min_duration_ms") is None else int(args["min_duration_ms"])
            limit = min(max(int(args.get("limit") or 5), 1), MAX_TRACES)
            minutes = min(max(int(args.get("window_minutes") or 15), 1), 180)
        except (TypeError, ValueError) as e:
            raise ToolError(f"min_duration_ms, limit and window_minutes must be integers: {e}") from e
        if min_duration_ms is not None and min_duration_ms < 0:
            raise ToolError("min_duration_ms must be zero or more")
        end = _parse_end(args.get("end"))
        start = end - timedelta(minutes=minutes)

        params: dict[str, Any] = {
            "service": service,
            "start": int(start.timestamp() * 1e6),
            "end": int(end.timestamp() * 1e6),
            "lookback": "custom",
        }
        if operation:
            params["operation"] = operation
        if min_duration_ms:
            params["minDuration"] = f"{min_duration_ms}ms"
        # Jaeger returns the most recent matches in no particular order. For the slowest traces without a
        # duration floor, take a wider sample and keep the slowest.
        sample = {**params, "limit": limit if min_duration_ms else max(limit, SLOW_SAMPLE)}
        queries = []
        async with httpx.AsyncClient(base_url=settings.jaeger_url, timeout=15, transport=transport) as client:
            if kind == "error":
                tagged = {**params, "tags": json.dumps({"error": "true"}), "limit": limit}
                queries.append(tagged)
                traces = await _search(client, tagged)
                scanned = len(traces)
                if not traces:
                    # Not every Jaeger storage maps the OTel error status to the `error` tag; fall back to
                    # scanning recent traces for failed spans ourselves.
                    queries.append(sample)
                    traces = await _search(client, sample)
                    scanned = len(traces)
                traces = [t for t in traces if any(_is_error(_tags(s.get("tags"))) for s in t.get("spans") or [])]
            else:
                queries.append(sample)
                traces = await _search(client, sample)
                scanned = len(traces)

        public = settings.jaeger_public_url
        summaries, rows = [], []
        for trace in traces:
            summary, trace_rows = summarise_trace(trace, f"{public}/trace/{trace.get('traceID')}")
            start_us = min((sp["startTime"] for sp in trace.get("spans") or []), default=0)
            summaries.append((summary, trace_rows, start_us))
        if kind == "slow":
            summaries.sort(key=lambda s: s[0]["duration_ms"], reverse=True)
        else:
            summaries.sort(key=lambda s: s[2], reverse=True)
        summaries = summaries[:limit]
        for _, trace_rows, _ in summaries:
            rows.extend(trace_rows)

        # The UI reads start/end only with lookback=custom; show the number of traces asked for.
        search_url = f"{public}/search?" + urlencode({**queries[0], "limit": limit})
        return {
            "service": service,
            "kind": kind,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "traces": [s for s, _, _ in summaries],
            "traces_scanned": scanned,
            "no_data": not summaries,
            "operations": rank_operations(rows),
            "evidence": {
                "jaeger_queries": [f"GET /api/traces?{urlencode(q)}" for q in queries],
                "search_url": search_url,
            },
        }

    return Tool("get_traces", DESCRIPTION, INPUT_SCHEMA, handler)
