"""search_logs: bounded, read-only log search over OpenSearch (AOCE-26).

The OTel Demo's collector exports every log record to OpenSearch (opensearch exporter, ss4o mapping)
into daily indices otel-logs-yyyy-MM-dd. A record looks like
{"@timestamp", "body", "severity": {"text", "number"}, "resource": {"service.name", ...},
 "attributes": {...}, "traceId", "spanId"}. The index has no template, so OpenSearch maps strings
dynamically as text with a .keyword subfield; exact filters and aggregations use that subfield
(SERVICE_KEY, SEVERITY_TEXT_KEY). An aggregation on the text field itself would fail the search.

The tool only calls _search, with a hard cap on hits, body length and attributes, and a server-side
timeout, so a broad query cannot flood the model's context or load the cluster.

Log text goes to the model and into the incident trace, so emails, bearer tokens, JWTs and key=value
secrets are masked in bodies and attribute values, and attributes whose key looks sensitive are
masked whole (as redact_query does for SQL, AOCE-39).
"""

import re
from collections import Counter
from datetime import timedelta
from typing import Any

import httpx

from aoce.tools import Tool, ToolError
from aoce.tools.metrics import SERVICE_RE, _parse_end

MAX_HITS = 50
BODY_CHARS = 500
MAX_ATTRIBUTES = 10
ATTRIBUTE_CHARS = 200
QUERY_CHARS = 200
MAX_PATTERNS = 10
SEARCH_TIMEOUT = "10s"
# Exact counts beyond this cost a full scan of the matches; report a lower bound instead.
TOTAL_HITS_CAP = 10_000

SERVICE_KEY = "resource.service.name.keyword"
SEVERITY_TEXT_KEY = "severity.text.keyword"
# OpenTelemetry severity numbers: the lowest number of each level (logs data model, SeverityNumber).
SEVERITY_NUMBERS = {"debug": 5, "info": 9, "warn": 13, "error": 17, "fatal": 21}
# Severity text for records that carry no number; matched on the keyword subfield, so list each case.
SEVERITY_TEXTS = {
    "debug": ["DEBUG"],
    "info": ["INFO", "INFORMATION"],
    "warn": ["WARN", "WARNING"],
    "error": ["ERROR", "ERR"],
    "fatal": ["FATAL", "CRITICAL", "CRIT", "PANIC", "EMERGENCY"],
}
LEVELS = list(SEVERITY_NUMBERS)

MASK = "[redacted]"
# Matched per key segment (split on . _ - and camelCase), so author, oauth.provider or cardinality stay.
SENSITIVE_SEGMENTS = {
    "auth", "authorization", "token", "password", "passwd", "pwd", "secret", "cookie", "credential",
    "credentials", "email", "card", "cvv", "jwt",
}
SENSITIVE_COMPOUNDS = ("apikey", "accesskey", "privatekey", "accesstoken", "refreshtoken")
_KEY_SEGMENT = re.compile(r"[._\-]+|(?<=[a-z0-9])(?=[A-Z])")
_SECRETS = [
    (re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.I), r"\1 " + MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), MASK),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), MASK),
    (
        re.compile(
            r"""\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[\w.-]*["']?\s*[=:]\s*)"""
            r"""(?:"[^"]*"|'[^']*'|[^\s"',;&]+)""",
            re.I,
        ),
        r"\1" + MASK,
    ),
]

DESCRIPTION = """\
Search application logs in OpenSearch over a time window, filtered by service, minimum severity and \
optional words in the log message. Returns the total number of matching records, counts per service \
and per severity, the newest matching records (timestamp, service, severity, message, trace and span \
id, a few attributes) and the most frequent message patterns among them (numbers and ids replaced with \
#), plus the exact OpenSearch query so you can cite it. Emails, tokens and other \
secrets in messages and attributes are replaced with [redacted].
Use it to find the errors a service logged around the alert time, or to check whether a suspect \
service logged anything unusual. Start with min_severity "error" and no text; add text to look for a \
specific message. Leave service null to see which services are logging errors at all."""

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "service": {
            "type": ["string", "null"],
            "description": "service.name of the emitting service, e.g. 'product-catalog'. Null for all services.",
        },
        "min_severity": {
            "type": "string",
            "enum": LEVELS,
            "description": "Lowest severity to include. 'error' returns error and fatal records.",
        },
        "text": {
            "type": ["string", "null"],
            "description": "Words that must all appear in the log message (simple query syntax: \"exact phrase\", -exclude, prefix*). Null for any message.",
        },
        "window_minutes": {
            "type": "integer",
            "description": "Length of the window ending at `end`, 1-180.",
        },
        "end": {
            "type": ["string", "null"],
            "description": "End of the window as an ISO-8601 timestamp (e.g. the alert time plus a few minutes). Null for now.",
        },
        "limit": {"type": "integer", "description": "Max records returned, 1-50. Counts and patterns cover all matches."},
    },
    "required": ["service", "min_severity", "text", "window_minutes", "end", "limit"],
    "additionalProperties": False,
}


def build_query(service: str | None, min_severity: str, text: str | None, start: str, end: str, limit: int) -> dict:
    texts = [t for level in LEVELS[LEVELS.index(min_severity):] for t in SEVERITY_TEXTS[level]]
    severity = {
        "bool": {
            "should": [
                {"range": {"severity.number": {"gte": SEVERITY_NUMBERS[min_severity]}}},
                # Records without a number (some SDKs and bridged loggers only set the text).
                {
                    "bool": {
                        "must_not": {"exists": {"field": "severity.number"}},
                        "filter": {"terms": {SEVERITY_TEXT_KEY: [v for t in texts for v in (t, t.lower(), t.capitalize())]}},
                    }
                },
            ],
            "minimum_should_match": 1,
        }
    }
    filters: list[dict[str, Any]] = [{"range": {"@timestamp": {"gte": start, "lte": end}}}, severity]
    if service:
        filters.append({"term": {SERVICE_KEY: service}})
    if text:
        # No fuzzy or regex operators: they are costly on a wildcard index.
        filters.append(
            {
                "simple_query_string": {
                    "query": text,
                    "fields": ["body"],
                    "default_operator": "and",
                    "flags": "AND|OR|NOT|PHRASE|PREFIX|WHITESPACE|ESCAPE|PRECEDENCE",
                    "analyze_wildcard": False,
                }
            }
        )
    return {
        "size": limit,
        "timeout": SEARCH_TIMEOUT,
        "track_total_hits": TOTAL_HITS_CAP,
        "sort": [{"@timestamp": {"order": "desc"}}],
        "query": {"bool": {"filter": filters}},
        "_source": ["@timestamp", "body", "severity", "resource", "attributes", "traceId", "spanId"],
        "aggs": {
            "by_service": {"terms": {"field": SERVICE_KEY, "size": 10}},
            "by_severity": {"terms": {"field": SEVERITY_TEXT_KEY, "size": 10}},
        },
    }


_VARIABLE = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b|\b[0-9a-fA-F]{8,}\b|\d+(?:\.\d+)?")


def _pattern(body: str) -> str:
    return " ".join(_VARIABLE.sub("#", body).split())[:BODY_CHARS]


def is_sensitive_key(key: str) -> bool:
    segments = [seg.lower() for seg in _KEY_SEGMENT.split(key) if seg]
    joined = "".join(segments)
    return any(seg in SENSITIVE_SEGMENTS for seg in segments) or any(c in joined for c in SENSITIVE_COMPOUNDS)


def redact_log_text(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def _cut(value: Any, n: int) -> Any:
    """Redact, then truncate, so a cut never leaves half a secret behind unmasked."""
    if not isinstance(value, str | int | float | bool | type(None)):
        value = str(value)
    if not isinstance(value, str):
        return value
    value = redact_log_text(value)
    return value[:n] + "…" if len(value) > n else value


def _record(hit: dict[str, Any]) -> dict[str, Any]:
    src = hit.get("_source", {})
    resource = src.get("resource") or {}
    attributes = src.get("attributes") or {}
    severity = src.get("severity") or {}
    return {
        "timestamp": src.get("@timestamp"),
        "service": resource.get("service.name"),
        "severity": severity.get("text") or severity.get("number"),
        "body": _cut(str(src.get("body", "")), BODY_CHARS),
        "trace_id": src.get("traceId") or None,
        "span_id": src.get("spanId") or None,
        "attributes": {
            k: MASK if is_sensitive_key(k) else _cut(v, ATTRIBUTE_CHARS)
            for k, v in list(attributes.items())[:MAX_ATTRIBUTES]
        },
    }


def _buckets(aggs: dict[str, Any], name: str) -> dict[str, int]:
    return {b["key"]: b["doc_count"] for b in aggs.get(name, {}).get("buckets", [])}


def search_logs_tool(settings, transport: httpx.AsyncBaseTransport | None = None) -> Tool:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        service = args.get("service") or None
        if service and not SERVICE_RE.match(service):
            raise ToolError(f"invalid service name: {service!r}")
        min_severity = args.get("min_severity") or "error"
        if min_severity not in SEVERITY_NUMBERS:
            raise ToolError(f"unknown min_severity: {min_severity!r} (use one of {', '.join(LEVELS)})")
        text = (args.get("text") or "").strip()[:QUERY_CHARS] or None
        minutes = min(max(int(args.get("window_minutes") or 15), 1), 180)
        limit = min(max(int(args.get("limit") or 20), 1), MAX_HITS)
        end = _parse_end(args.get("end"))
        start = end - timedelta(minutes=minutes)

        index = settings.opensearch_logs_index
        query = build_query(service, min_severity, text, start.isoformat(), end.isoformat(), limit)
        params = {"ignore_unavailable": "true", "allow_no_indices": "true"}
        try:
            async with httpx.AsyncClient(base_url=settings.opensearch_url, timeout=15, transport=transport) as client:
                resp = await client.post(f"/{index}/_search", params=params, json=query)
        except httpx.HTTPError as e:
            # No URL in the message: it goes to the model and could carry credentials (user:pass@host).
            raise ToolError(f"OpenSearch unreachable: {type(e).__name__}") from e
        try:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if resp.status_code != 200 or "hits" not in body:
            error = body.get("error", resp.text[:300])
            if isinstance(error, dict):
                error = (error.get("root_cause") or [{}])[0].get("reason") or error.get("reason") or error
            # Parse errors can echo query or document values.
            raise ToolError(f"OpenSearch error ({resp.status_code}): {redact_log_text(str(error))[:300]}")

        hits = body["hits"]["hits"]
        total = body["hits"].get("total", {})
        total_count = total.get("value", len(hits)) if isinstance(total, dict) else int(total)
        records = [_record(h) for h in hits]
        patterns = Counter(_pattern(r["body"]) for r in records if r["body"])
        aggs = body.get("aggregations", {})
        return {
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "total_matches": total_count,
            "total_is_lower_bound": isinstance(total, dict) and total.get("relation") == "gte",
            "returned": len(records),
            "timed_out": bool(body.get("timed_out")),
            "by_service": _buckets(aggs, "by_service"),
            "by_severity": _buckets(aggs, "by_severity"),
            "top_patterns_in_returned": [{"pattern": p, "count": c} for p, c in patterns.most_common(MAX_PATTERNS)],
            "records": records,
            "no_data": total_count == 0,
            "evidence": {"index": index, "opensearch_query": query},
        }

    return Tool("search_logs", DESCRIPTION, INPUT_SCHEMA, handler)
