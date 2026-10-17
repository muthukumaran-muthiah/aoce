import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aoce.config import Settings
from aoce.tools import ToolRegistry, build_registry
from aoce.tools.logs import BODY_CHARS, MAX_ATTRIBUTES, build_query, search_logs_tool

END = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


def hit(service: str, body: str, severity: str = "ERROR", number: int | None = 17, **attributes) -> dict:
    severity_doc = {"text": severity} | ({"number": number} if number is not None else {})
    return {
        "_index": "otel-logs-2026-10-13",
        "_source": {
            "@timestamp": "2026-10-13T08:58:00Z",
            "body": body,
            "severity": severity_doc,
            "resource": {"service.name": service, "k8s.pod.name": f"{service}-abc"},
            "attributes": attributes,
            "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
            "spanId": "00f067aa0ba902b7",
        },
    }


def response(hits: list[dict], total: int | None = None) -> dict:
    services: dict[str, int] = {}
    for h in hits:
        name = h["_source"]["resource"]["service.name"]
        services[name] = services.get(name, 0) + 1
    return {
        "timed_out": False,
        "hits": {"total": {"value": len(hits) if total is None else total, "relation": "eq"}, "hits": hits},
        "aggregations": {
            "by_service": {"buckets": [{"key": k, "doc_count": v} for k, v in services.items()]},
            "by_severity": {"buckets": [{"key": "ERROR", "doc_count": len(hits)}]},
        },
    }


def registry_with(handler) -> tuple[ToolRegistry, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    tool = search_logs_tool(Settings(opensearch_url="http://os"), transport=httpx.MockTransport(respond))
    return ToolRegistry([tool]), seen


def args(**overrides):
    base = {
        "service": "product-catalog",
        "min_severity": "error",
        "text": None,
        "window_minutes": 10,
        "end": END.isoformat(),
        "limit": 20,
    }
    return base | overrides


async def test_searches_the_window_and_summarises_records():
    hits = [
        hit("product-catalog", "query timed out after 5000ms on order 123"),
        hit("product-catalog", "query timed out after 7000ms on order 456"),
        hit("product-catalog", "connection reset", **{"code.function": "GetProduct"}),
    ]
    registry, seen = registry_with(lambda r: httpx.Response(200, json=response(hits, total=40)))

    content, is_error = await registry.run("search_logs", args())

    assert not is_error
    out = json.loads(content)
    assert out["total_matches"] == 40 and out["returned"] == 3 and not out["no_data"]
    assert out["by_service"] == {"product-catalog": 3}
    assert out["top_patterns_in_returned"][0] == {"pattern": "query timed out after #ms on order #", "count": 2}
    record = out["records"][2]
    assert record["service"] == "product-catalog"
    assert record["severity"] == "ERROR"
    assert record["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert record["attributes"] == {"code.function": "GetProduct"}

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/otel-logs-*/_search"
    assert request.url.params["ignore_unavailable"] == "true"
    sent = json.loads(request.content)
    assert sent == out["evidence"]["opensearch_query"]
    time_range = sent["query"]["bool"]["filter"][0]["range"]["@timestamp"]
    assert time_range == {"gte": (END - timedelta(minutes=10)).isoformat(), "lte": END.isoformat()}
    assert sent["size"] == 20 and sent["timeout"]


def test_query_filters_service_severity_and_text():
    q = build_query("cart", "warn", "timeout -retry", "s", "e", 5)
    filters = q["query"]["bool"]["filter"]
    text = json.dumps(filters)
    assert '{"range": {"severity.number": {"gte": 13}}}' in text
    # Records with only a severity text still match, in any case, for warn and above but not info.
    assert '"WARNING"' in text and '"error"' in text and '"Fatal"' in text and '"INFO"' not in text
    assert '{"term": {"resource.service.name.keyword": "cart"}}' in text
    assert filters[-1] == {"simple_query_string": {"query": "timeout -retry", "fields": ["body"], "default_operator": "and"}}


def test_query_without_service_or_text_has_only_time_and_severity():
    filters = build_query(None, "error", None, "s", "e", 5)["query"]["bool"]["filter"]
    assert len(filters) == 2


async def test_limits_are_enforced():
    long_attrs = {f"a{i}": "x" * 1000 for i in range(30)}
    hits = [hit("cart", "y" * 5000, **long_attrs)]
    registry, seen = registry_with(lambda r: httpx.Response(200, json=response(hits)))

    out = json.loads((await registry.run("search_logs", args(limit=500, window_minutes=10_000)))[0])

    sent = json.loads(seen[0].content)
    assert sent["size"] == 50
    assert sent["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"] == (END - timedelta(minutes=180)).isoformat()
    record = out["records"][0]
    assert len(record["body"]) <= BODY_CHARS + 1
    assert len(record["attributes"]) == MAX_ATTRIBUTES
    assert all(len(v) <= 201 for v in record["attributes"].values())


async def test_record_without_severity_text_reports_the_number():
    hits = [hit("cart", "boom", severity="", number=17)]
    registry, _ = registry_with(lambda r: httpx.Response(200, json=response(hits)))
    out = json.loads((await registry.run("search_logs", args()))[0])
    assert out["records"][0]["severity"] == 17


async def test_no_matches_is_not_an_error():
    registry, _ = registry_with(lambda r: httpx.Response(200, json=response([])))
    content, is_error = await registry.run("search_logs", args())
    assert not is_error
    assert json.loads(content)["no_data"] is True


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"service": 'cart" OR *'}, "invalid service name"),
        ({"min_severity": "loud"}, "unknown min_severity"),
        ({"end": "yesterday"}, "not an ISO-8601"),
    ],
)
async def test_bad_input_is_reported_to_the_model(overrides, message):
    registry, seen = registry_with(lambda r: httpx.Response(200, json=response([])))
    content, is_error = await registry.run("search_logs", args(**overrides))
    assert is_error and message in content
    assert not seen


async def test_opensearch_errors_are_tool_errors():
    error = {
        "error": {"root_cause": [{"type": "query_shard_exception", "reason": "No mapping found for [@timestamp]"}]},
        "status": 400,
    }
    registry, _ = registry_with(lambda r: httpx.Response(400, json=error))
    content, is_error = await registry.run("search_logs", args())
    assert is_error and "No mapping found" in content


async def test_unreachable_opensearch_is_a_tool_error():
    def boom(request):
        raise httpx.ConnectError("connection refused")

    registry, _ = registry_with(boom)
    content, is_error = await registry.run("search_logs", args())
    assert is_error and "unreachable" in content


def test_definition_is_strict_and_registered():
    registry, _ = registry_with(lambda r: httpx.Response(200, json=response([])))
    (definition,) = registry.definitions()
    assert definition["strict"] is True
    schema = definition["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "search_logs" in build_registry(Settings()).names()
