import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aoce.config import Settings
from aoce.tools import ToolRegistry
from aoce.tools.metrics import build_promql, query_metrics_tool

END = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


def matrix(series: dict[str, list[float]], minutes: int = 10, step: int = 60) -> dict:
    """Prometheus query_range body: baseline + window, one point per step."""
    start = END - timedelta(minutes=2 * minutes)
    result = []
    for service, values in series.items():
        result.append(
            {
                "metric": {"service_name": service},
                "values": [[(start + timedelta(seconds=i * step)).timestamp(), str(v)] for i, v in enumerate(values)],
            }
        )
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def registry_with(handler) -> tuple[ToolRegistry, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    tool = query_metrics_tool(Settings(prometheus_url="http://prom"), transport=httpx.MockTransport(respond))
    return ToolRegistry([tool]), seen


def args(**overrides):
    base = {"signal": "latency_p95", "service": None, "promql": None, "window_minutes": 10, "end": END.isoformat()}
    return base | overrides


async def test_summarises_window_against_baseline_and_sorts_by_peak():
    body = matrix({"cart": [10.0] * 21, "product-catalog": [100.0] * 10 + [100.0] + [2000.0] * 10})
    registry, seen = registry_with(lambda r: httpx.Response(200, json=body))

    content, is_error = await registry.run("query_metrics", args())

    assert not is_error
    out = json.loads(content)
    top = out["results"][0]
    assert top["labels"] == {"service_name": "product-catalog"}
    assert top["baseline_avg"] == 100.0
    assert top["window_max"] == 2000.0
    assert top["change_vs_baseline"] > 10
    assert out["unit"] == "milliseconds"
    assert "histogram_quantile(0.95" in out["evidence"]["promql"]
    assert out["evidence"]["graph_url"].startswith("http://localhost:9090/graph?")
    params = seen[0].url.params
    assert float(params["end"]) == END.timestamp()
    assert float(params["start"]) == (END - timedelta(minutes=20)).timestamp()


async def test_series_are_capped_and_downsampled():
    body = matrix({f"svc{i}": list(range(121)) for i in range(15)}, minutes=60, step=60)
    registry, _ = registry_with(lambda r: httpx.Response(200, json=body))

    out = json.loads((await registry.run("query_metrics", args(window_minutes=60)))[0])

    assert len(out["results"]) == 10
    assert out["series_truncated"] == 5
    assert len(out["results"][0]["series"]) <= 20


def test_error_rate_query_keeps_services_without_errors():
    q = build_promql("error_rate", "cart")
    assert 'service_name="cart"' in q
    assert 'status_code="STATUS_CODE_ERROR"' in q
    assert " or " in q and "* 0" in q


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"service": 'cart"} or vector(1)'}, "invalid service name"),
        ({"signal": "promql", "promql": None}, "needs a promql expression"),
        ({"end": "yesterday"}, "not an ISO-8601"),
    ],
)
async def test_bad_input_is_reported_to_the_model(overrides, message):
    registry, seen = registry_with(lambda r: httpx.Response(200, json=matrix({})))
    content, is_error = await registry.run("query_metrics", args(**overrides))
    assert is_error and message in content
    assert not seen


async def test_prometheus_errors_are_tool_errors():
    registry, _ = registry_with(
        lambda r: httpx.Response(400, json={"status": "error", "error": "parse error at char 3"})
    )
    content, is_error = await registry.run("query_metrics", args(signal="promql", promql="rate(("))
    assert is_error and "parse error" in content


async def test_unreachable_prometheus_is_a_tool_error():
    def boom(request):
        raise httpx.ConnectError("connection refused")

    registry, _ = registry_with(boom)
    content, is_error = await registry.run("query_metrics", args())
    assert is_error and "unreachable" in content


def test_definition_is_strict():
    registry, _ = registry_with(lambda r: httpx.Response(200, json=matrix({})))
    (definition,) = registry.definitions()
    assert definition["strict"] is True
    schema = definition["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
