"""query_metrics: RED metrics for a service from Prometheus span metrics (AOCE-25).

The OTel Demo's collector turns every span into `traces_span_metrics_*` series (span_metrics
connector), labelled by service_name, span_kind and status_code. We look at SERVER spans only, so
each service is measured by the requests it serves rather than the calls it makes.
"""

import re
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import Any
from urllib.parse import urlencode

import httpx

from aoce.tools import Tool, ToolError

CALLS = "traces_span_metrics_calls_total"
DURATION_BUCKET = "traces_span_metrics_duration_milliseconds_bucket"

MAX_SERIES = 10
MAX_POINTS = 20
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

SIGNAL_UNITS = {"error_rate": "ratio (0-1)", "latency_p95": "milliseconds", "request_rate": "requests/second"}

DESCRIPTION = """\
Query Prometheus for a service's request rate, error rate or p95 latency, measured on the requests it \
serves. Returns, per service, the average over the investigation window, the average over the equally \
long window just before it (the baseline), the peak, the latest value and a short downsampled series, \
plus the exact PromQL used so you can cite it.
Use it to confirm which service is unhealthy and when it started. Leave service null to compare all \
services at once (top 10 by peak). Use signal "promql" with your own query only when the three built-in \
signals cannot answer the question; results are summarised the same way and capped at 10 series."""

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "signal": {
            "type": "string",
            "enum": ["error_rate", "latency_p95", "request_rate", "promql"],
            "description": "Which metric to fetch.",
        },
        "service": {
            "type": ["string", "null"],
            "description": "service_name label, e.g. 'product-catalog'. Null for all services.",
        },
        "promql": {
            "type": ["string", "null"],
            "description": "Instant-vector PromQL expression. Required when signal is 'promql', otherwise null.",
        },
        "window_minutes": {
            "type": "integer",
            "description": "Length of the investigation window ending at `end`, 5-180. The baseline is the same length, just before it.",
        },
        "end": {
            "type": ["string", "null"],
            "description": "End of the window as an ISO-8601 timestamp (e.g. the alert time plus a few minutes). Null for now.",
        },
    },
    "required": ["signal", "service", "promql", "window_minutes", "end"],
    "additionalProperties": False,
}


def build_promql(signal: str, service: str | None) -> str:
    selector = 'span_kind="SPAN_KIND_SERVER"'
    if service:
        selector += f',service_name="{service}"'
    calls = f"sum by (service_name) (rate({CALLS}{{{selector}}}[1m]))"
    if signal == "request_rate":
        return calls
    if signal == "error_rate":
        errors = f'sum by (service_name) (rate({CALLS}{{{selector},status_code="STATUS_CODE_ERROR"}}[1m]))'
        # Services with zero errors have no error series; "or ... * 0" keeps them in the result as 0.
        return f"({errors} or {calls} * 0) / {calls}"
    if signal == "latency_p95":
        return f"histogram_quantile(0.95, sum by (service_name, le) (rate({DURATION_BUCKET}{{{selector}}}[1m])))"
    raise ToolError(f"unknown signal: {signal}")


def _parse_end(end: str | None) -> datetime:
    if not end:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"end is not an ISO-8601 timestamp: {end!r}") from e
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _downsample(points: list[tuple[float, float]], n: int) -> list[tuple[float, float]]:
    if len(points) <= n:
        return points
    stride = len(points) / n
    return [points[int(i * stride)] for i in range(n - 1)] + [points[-1]]


def summarise(series: dict[str, Any], window_start: float) -> dict[str, Any]:
    points = [(float(t), float(v)) for t, v in series["values"] if v not in ("NaN", "+Inf", "-Inf")]
    baseline = [v for t, v in points if t < window_start]
    window = [v for t, v in points if t >= window_start]

    def rnd(x: float | None) -> float | None:
        return None if x is None else round(x, 4)

    baseline_avg = fmean(baseline) if baseline else None
    window_avg = fmean(window) if window else None
    change = None
    if baseline_avg and window_avg is not None:
        change = round(window_avg / baseline_avg, 2)
    return {
        "labels": series["metric"],
        "baseline_avg": rnd(baseline_avg),
        "window_avg": rnd(window_avg),
        "window_max": rnd(max(window) if window else None),
        "latest": rnd(points[-1][1] if points else None),
        "change_vs_baseline": change,
        "series": [
            [datetime.fromtimestamp(t, UTC).strftime("%H:%M:%S"), rnd(v)]
            for t, v in _downsample([p for p in points if p[0] >= window_start], MAX_POINTS)
        ],
    }


def query_metrics_tool(settings, transport: httpx.AsyncBaseTransport | None = None) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        signal = args["signal"]
        service = args.get("service") or None
        if service and not SERVICE_RE.match(service):
            raise ToolError(f"invalid service name: {service!r}")
        if signal == "promql":
            promql = (args.get("promql") or "").strip()
            if not promql:
                raise ToolError("signal 'promql' needs a promql expression")
        else:
            promql = build_promql(signal, service)

        minutes = min(max(int(args.get("window_minutes") or 30), 5), 180)
        end = _parse_end(args.get("end"))
        window_start = end - timedelta(minutes=minutes)
        start = window_start - timedelta(minutes=minutes)
        step = max(15, minutes * 60 // 60)

        params = {"query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": step}
        try:
            async with httpx.AsyncClient(base_url=settings.prometheus_url, timeout=15, transport=transport) as client:
                resp = await client.get("/api/v1/query_range", params=params)
        except httpx.HTTPError as e:
            raise ToolError(f"Prometheus unreachable at {settings.prometheus_url}: {e}") from e
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or body.get("status") != "success":
            raise ToolError(f"Prometheus error ({resp.status_code}): {body.get('error', resp.text[:300])}")

        results = [summarise(s, window_start.timestamp()) for s in body["data"]["result"]]
        results.sort(key=lambda r: r["window_max"] if r["window_max"] is not None else float("-inf"), reverse=True)
        graph_url = f"{settings.prometheus_public_url}/graph?" + urlencode(
            {"g0.expr": promql, "g0.range_input": f"{minutes * 2}m", "g0.end_input": end.strftime("%Y-%m-%d %H:%M:%S")}
        )
        return {
            "signal": signal,
            "unit": SIGNAL_UNITS.get(signal, "as queried"),
            "window": {"start": window_start.isoformat(), "end": end.isoformat(), "baseline_start": start.isoformat()},
            "results": results[:MAX_SERIES],
            "series_truncated": max(0, len(results) - MAX_SERIES),
            "no_data": not results,
            "evidence": {"promql": promql, "graph_url": graph_url},
        }

    return Tool("query_metrics", DESCRIPTION, INPUT_SCHEMA, handler)
