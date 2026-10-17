import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aoce.config import Settings
from aoce.tools import ToolRegistry, build_registry
from aoce.tools.traces import SLOW_SAMPLE, get_traces_tool

END = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
T0 = int((END - timedelta(minutes=5)).timestamp() * 1e6)


def span(span_id, op, process, start_ms, duration_ms, parent=None, tags=(), logs=()):
    return {
        "traceID": "t",
        "spanID": span_id,
        "operationName": op,
        "references": [{"refType": "CHILD_OF", "traceID": "t", "spanID": parent}] if parent else [],
        "startTime": T0 + start_ms * 1000,
        "duration": duration_ms * 1000,
        "tags": [{"key": k, "type": "string", "value": v} for k, v in tags],
        "logs": list(logs),
        "processID": process,
    }


PROCESSES = {
    "p1": {"serviceName": "frontend", "tags": []},
    "p2": {"serviceName": "product-catalog", "tags": []},
    "p3": {"serviceName": "postgres", "tags": []},
}


def checkout_trace(trace_id="slow1", db_ms=900, error=False):
    """frontend GET -> product-catalog GetProduct -> SELECT (db_ms); each caller adds 50ms of its own."""
    db_tags = [("db.system", "postgresql"), ("db.statement", "SELECT * FROM products WHERE id = 'OLJCESPC7Z'")]
    logs = []
    if error:
        db_tags += [("error", True), ("otel.status_description", "canceling statement due to lock timeout")]
        logs = [
            {
                "timestamp": T0,
                "fields": [
                    {"key": "event", "value": "exception"},
                    {"key": "exception.type", "value": "QueryCanceled"},
                    {"key": "exception.message", "value": "lock timeout"},
                ],
            }
        ]
    spans = [
        span("a", "GET /api/products", "p1", 0, db_ms + 100, tags=[("http.request.method", "GET"), ("user.email", "x@y.z")]),
        span("b", "oteldemo.ProductCatalogService/GetProduct", "p2", 20, db_ms + 50, parent="a"),
        span("c", "SELECT products", "p3", 30, db_ms, parent="b", tags=db_tags, logs=logs),
    ]
    for s in spans:
        s["traceID"] = trace_id
    return {"traceID": trace_id, "spans": spans, "processes": PROCESSES}


def registry_with(handler) -> tuple[ToolRegistry, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    tool = get_traces_tool(
        Settings(jaeger_url="http://jaeger/jaeger/ui", jaeger_public_url="http://ui/jaeger/ui"),
        transport=httpx.MockTransport(respond),
    )
    return ToolRegistry([tool]), seen


def args(**overrides):
    base = {
        "service": "product-catalog",
        "kind": "slow",
        "operation": None,
        "min_duration_ms": None,
        "window_minutes": 15,
        "end": END.isoformat(),
        "limit": 5,
    }
    return base | overrides


async def test_slow_traces_are_sorted_and_self_time_points_at_the_database():
    body = {"data": [checkout_trace("fast", db_ms=50), checkout_trace("slow1", db_ms=900)]}
    registry, seen = registry_with(lambda r: httpx.Response(200, json=body))

    content, is_error = await registry.run("get_traces", args(limit=1))

    assert not is_error, content
    out = json.loads(content)
    (trace,) = out["traces"]
    assert trace["trace_id"] == "slow1"
    assert trace["duration_ms"] == 1000.0
    assert trace["root"] == {"service": "frontend", "operation": "GET /api/products"}
    assert trace["services"] == ["frontend", "postgres", "product-catalog"]
    assert trace["top_self_time"][0] == {
        "service": "postgres",
        "operation": "SELECT products",
        "self_ms": 900.0,
        "duration_ms": 900.0,
    }
    # Each caller's own time is its duration minus the time covered by its child.
    self_ms = {r["service"]: r["self_ms"] for r in trace["top_self_time"]}
    assert self_ms == {"postgres": 900.0, "product-catalog": 50.0, "frontend": 50.0}
    assert trace["url"] == "http://ui/jaeger/ui/trace/slow1"
    assert out["operations"]["by_self_time"][0]["operation"] == "SELECT products"
    assert out["traces_scanned"] == 2

    request = seen[0]
    assert request.url.path == "/jaeger/ui/api/traces"
    params = request.url.params
    assert params["service"] == "product-catalog"
    assert int(params["limit"]) == SLOW_SAMPLE
    assert int(params["end"]) == int(END.timestamp() * 1e6)
    assert int(params["start"]) == int((END - timedelta(minutes=15)).timestamp() * 1e6)
    assert "tags" not in params
    search = httpx.URL(out["evidence"]["search_url"])
    assert str(search).startswith("http://ui/jaeger/ui/search?")
    assert search.params["lookback"] == "custom" and search.params["limit"] == "1"


async def test_min_duration_and_operation_are_passed_to_jaeger():
    registry, seen = registry_with(lambda r: httpx.Response(200, json={"data": []}))

    out = json.loads(
        (await registry.run("get_traces", args(min_duration_ms=500, operation="oteldemo.X/Get", limit=3)))[0]
    )

    params = seen[0].url.params
    assert params["minDuration"] == "500ms"
    assert params["operation"] == "oteldemo.X/Get"
    assert int(params["limit"]) == SLOW_SAMPLE
    assert out["no_data"] is True


async def test_error_traces_show_the_failing_span_with_redacted_sql():
    body = {"data": [checkout_trace("err1", error=True)]}
    registry, seen = registry_with(lambda r: httpx.Response(200, json=body))

    out = json.loads((await registry.run("get_traces", args(kind="error")))[0])

    assert json.loads(seen[0].url.params["tags"]) == {"error": "true"}
    (trace,) = out["traces"]
    (failed,) = trace["error_spans"]
    assert failed["service"] == "postgres"
    assert failed["exception"] == {"type": "QueryCanceled", "message": "lock timeout"}
    assert failed["attributes"]["otel.status_description"] == "canceling statement due to lock timeout"
    assert failed["attributes"]["db.statement"] == "SELECT * FROM products WHERE id = $1"
    assert out["operations"]["by_errors"][0] == {
        "service": "postgres",
        "operation": "SELECT products",
        "spans": 1,
        "errors": 1,
        "self_ms": 900.0,
    }
    # Attributes outside the allow-list never reach the model.
    assert "x@y.z" not in json.dumps(out)


async def test_error_search_falls_back_to_scanning_recent_traces():
    def respond(request):
        if "tags" in request.url.params:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [checkout_trace("ok"), checkout_trace("err1", error=True)]})

    registry, seen = registry_with(respond)
    out = json.loads((await registry.run("get_traces", args(kind="error")))[0])

    assert len(seen) == 2
    assert [t["trace_id"] for t in out["traces"]] == ["err1"]
    assert out["traces_scanned"] == 2
    assert len(out["evidence"]["jaeger_queries"]) == 2


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"service": "cart&limit=1000"}, "invalid service name"),
        ({"service": ""}, "invalid service name"),
        ({"min_duration_ms": -1}, "zero or more"),
        ({"end": "yesterday"}, "not an ISO-8601"),
        ({"limit": "five"}, "must be integers"),
    ],
)
async def test_bad_input_is_reported_to_the_model(overrides, message):
    registry, seen = registry_with(lambda r: httpx.Response(200, json={"data": []}))
    content, is_error = await registry.run("get_traces", args(**overrides))
    assert is_error and message in content
    assert not seen


async def test_jaeger_errors_are_tool_errors():
    registry, _ = registry_with(
        lambda r: httpx.Response(400, json={"data": None, "errors": [{"code": 400, "msg": "parameter 'service' is required"}]})
    )
    content, is_error = await registry.run("get_traces", args())
    assert is_error and "parameter 'service' is required" in content


@pytest.mark.parametrize("body", ['{"data": [', "[]", "{}", '{"data": "x"}'])
async def test_malformed_jaeger_bodies_are_tool_errors(body):
    registry, _ = registry_with(
        lambda r: httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    content, is_error = await registry.run("get_traces", args())
    assert is_error and "unexpected body" in content


async def test_error_traces_are_newest_first():
    older, newer = checkout_trace("older", error=True), checkout_trace("newer", error=True)
    for s in newer["spans"]:
        s["startTime"] += 5_123_456
    registry, _ = registry_with(lambda r: httpx.Response(200, json={"data": [older, newer]}))
    out = json.loads((await registry.run("get_traces", args(kind="error")))[0])
    assert [t["trace_id"] for t in out["traces"]] == ["newer", "older"]


async def test_odd_error_bodies_are_tool_errors_and_redacted():
    registry, _ = registry_with(lambda r: httpx.Response(500, json={"errors": "boom for jane@example.com"}))
    content, is_error = await registry.run("get_traces", args())
    assert is_error and content.startswith("Jaeger error (500)") and "jane@example.com" not in content


async def test_error_fallback_scans_a_wide_sample_even_with_min_duration():
    registry, seen = registry_with(lambda r: httpx.Response(200, json={"data": []}))
    await registry.run("get_traces", args(kind="error", min_duration_ms=500, limit=3))
    assert [r.url.params["limit"] for r in seen] == ["3", str(SLOW_SAMPLE)]


async def test_unreachable_jaeger_is_a_tool_error():
    def boom(request):
        raise httpx.ConnectError("connection refused")

    registry, _ = registry_with(boom)
    content, is_error = await registry.run("get_traces", args())
    assert is_error and "unreachable" in content


def test_definition_is_strict_and_registered():
    registry, _ = registry_with(lambda r: httpx.Response(200, json={"data": []}))
    (definition,) = registry.definitions()
    assert definition["strict"] is True
    schema = definition["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "get_traces" in build_registry(Settings()).names()


async def test_free_text_in_errors_is_scrubbed():
    trace = checkout_trace("err1", error=True)
    db = trace["spans"][2]
    (status,) = [t for t in db["tags"] if t["key"] == "otel.status_description"]
    status["value"] = 'invalid input syntax for type uuid: "a1b2"'
    db["logs"][0]["fields"][2]["value"] = "duplicate key violates unique constraint: Key (email)=(jane@example.com)"
    registry, _ = registry_with(lambda r: httpx.Response(200, json={"data": [trace]}))

    out = json.loads((await registry.run("get_traces", args(kind="error")))[0])

    (failed,) = out["traces"][0]["error_spans"]
    assert failed["exception"]["message"] == "duplicate key violates unique constraint: Key (email)=[redacted]"
    assert failed["attributes"]["otel.status_description"] == "invalid input syntax for type uuid: [redacted]"
    assert "jane@example.com" not in json.dumps(out) and "a1b2" not in json.dumps(out)


async def test_operation_names_are_scrubbed():
    trace = checkout_trace("t1")
    trace["spans"][0]["operationName"] = "GET /users/jane@example.com"
    registry, _ = registry_with(lambda r: httpx.Response(200, json={"data": [trace]}))

    out = json.loads((await registry.run("get_traces", args()))[0])

    assert out["traces"][0]["root"]["operation"] == "GET /users/[redacted]"
    assert "jane@example.com" not in json.dumps(out)


async def test_error_fallback_runs_when_tagged_traces_have_no_failed_span():
    def respond(request):
        if "tags" in request.url.params:
            return httpx.Response(200, json={"data": [checkout_trace("ok")]})
        return httpx.Response(200, json={"data": [checkout_trace("err1", error=True)]})

    registry, seen = registry_with(respond)
    out = json.loads((await registry.run("get_traces", args(kind="error")))[0])

    assert len(seen) == 2
    assert [t["trace_id"] for t in out["traces"]] == ["err1"]


async def test_follows_from_span_is_not_a_root():
    trace = checkout_trace("t1")
    # The consumer's host clock runs a little behind, so it appears to start before the producer.
    consumer = span("d", "kafka consume", "p2", -5, 50)
    consumer["references"] = [{"refType": "FOLLOWS_FROM", "traceID": "t1", "spanID": "a"}]
    trace["spans"].append(consumer)
    registry, _ = registry_with(lambda r: httpx.Response(200, json={"data": [trace]}))

    out = json.loads((await registry.run("get_traces", args()))[0])

    assert out["traces"][0]["root"]["operation"] == "GET /api/products"
