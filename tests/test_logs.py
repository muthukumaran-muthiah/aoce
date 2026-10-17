import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aoce.config import Settings
from aoce.tools import ToolRegistry, build_registry
from aoce.tools.logs import BODY_CHARS, MAX_ATTRIBUTES, build_query, is_sensitive_key, redact_log_text, search_logs_tool

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
    text_query = filters[-1]["simple_query_string"]
    assert text_query["query"] == "timeout -retry" and text_query["fields"] == ["body"]
    assert "FUZZY" not in text_query["flags"] and "ALL" not in text_query["flags"]


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


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("login failed for jane.doe@example.com", "jane.doe@example.com"),
        ("upstream sent Authorization: Bearer abc.DEF-123_xyz", "abc.DEF-123_xyz"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl rejected", "eyJzdWIiOiIxIn0"),
        ("retrying with password=hunter2&user=bob", "hunter2"),
        ('config {"api_key": "sk-123456"}', "sk-123456"),
        ("refresh with access_token=at-998877 failed", "at-998877"),
        ("auth_token: tk-5544 expired", "tk-5544"),
        ("env DB_PASSWORD=s3cr3t missing", "s3cr3t"),
        ("oauth client_secret: cs-abc rejected", "cs-abc"),
        ("header x_api_key=xk-777 invalid", "xk-777"),
        ('login password="my secret phrase" rejected', "secret phrase"),
        ("password: 'hunter 2' rejected", "hunter 2"),
        ("charge 4111 1111 1111 1111 declined", "4111 1111"),
        ("card 5500-0000-0000-0004 declined", "5500-0000"),
        ("key sk-ant-api03-abcdefghijklmnop123 rejected", "abcdefghijklmnop"),
        ("aws AKIAIOSFODNN7EXAMPLE denied", "AKIAIOSFODNN7EXAMPLE"),
        ("push with ghp_abcdefghijklmnopqrstuvwxyz0123456789 failed", "ghp_abcdef"),
    ],
)
def test_secrets_are_redacted_from_log_text(text, leaked):
    out = redact_log_text(text)
    assert leaked not in out and "[redacted]" in out


async def test_records_are_redacted_before_they_reach_the_model():
    body = "x" * (BODY_CHARS - 10) + " mail bob@example.com now"
    hits = [hit("cart", body, **{"http.request.header.authorization": "Basic Ym9iOnB3", "user.email": "a@b.co",
                                  "error.message": "token=abcd1234 expired", "order.id": "42"})]
    registry, _ = registry_with(lambda r: httpx.Response(200, json=response(hits)))

    content, _ = await registry.run("search_logs", args())

    assert "bob@ex" not in content and "Ym9iOnB3" not in content and "a@b.co" not in content
    record = json.loads(content)["records"][0]
    assert record["attributes"]["http.request.header.authorization"] == "[redacted]"
    assert record["attributes"]["user.email"] == "[redacted]"
    assert record["attributes"]["error.message"] == "token=[redacted] expired"
    assert record["attributes"]["order.id"] == "42"


async def test_capped_total_is_reported_as_a_lower_bound():
    body = response([hit("cart", "boom")])
    body["hits"]["total"] = {"value": 10_000, "relation": "gte"}
    registry, seen = registry_with(lambda r: httpx.Response(200, json=body))
    out = json.loads((await registry.run("search_logs", args()))[0])
    assert out["total_is_lower_bound"] is True
    assert json.loads(seen[0].content)["track_total_hits"] == 10_000


async def test_error_without_root_cause_and_unreachable_message_have_no_url():
    registry, _ = registry_with(lambda r: httpx.Response(500, json={"error": {"root_cause": [], "reason": "all shards failed"}}))
    content, is_error = await registry.run("search_logs", args())
    assert is_error and "all shards failed" in content

    def boom(request):
        raise httpx.ConnectError("connection refused to http://user:pw@os")

    registry, _ = registry_with(boom)
    content, is_error = await registry.run("search_logs", args())
    assert is_error and "unreachable" in content and "pw@" not in content and "http://" not in content


def test_patterns_collapse_uuids_whole():
    from aoce.tools.logs import _pattern

    assert _pattern("cart 123e4567-e89b-12d3-a456-426614174000 missing") == "cart # missing"


@pytest.mark.parametrize("content", [b'{"hits": {"tot', b"[1, 2]"])
async def test_invalid_json_body_is_a_tool_error(content):
    registry, _ = registry_with(lambda r: httpx.Response(200, content=content, headers={"content-type": "application/json"}))
    content, is_error = await registry.run("search_logs", args())
    assert is_error and "OpenSearch error (200)" in content


@pytest.mark.parametrize(
    "key, sensitive",
    [
        ("http.request.header.authorization", True), ("user.email", True), ("userEmail", True),
        ("access_token", True), ("x-api-key", True), ("apiKey", True), ("db.password", True),
        ("app.card.number", True), ("author", False), ("oauth.provider", False), ("cardinality", False),
        ("session.id", False), ("llm.tokens", False), ("order.id", False),
    ],
)
def test_sensitive_keys_are_matched_by_segment(key, sensitive):
    assert is_sensitive_key(key) is sensitive


async def test_opensearch_error_text_is_redacted():
    error = {"error": {"root_cause": [{"reason": "failed to parse value [bob@example.com]"}]}}
    registry, _ = registry_with(lambda r: httpx.Response(400, json=error))
    content, is_error = await registry.run("search_logs", args())
    assert is_error and "bob@example.com" not in content and "[redacted]" in content


def test_non_card_numbers_stay_readable():
    text = "order 1729181234567 took 120 ms, trace 4bf92f3577b34da6a3ce929d0e0e4736"
    assert redact_log_text(text) == text
