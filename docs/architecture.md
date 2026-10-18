# Architecture

How a failure in the Astronomy Shop becomes a cited root-cause report: an alert opens an incident,
the investigation loop tests hypotheses with read-only tools, and the result is a report you read at
`/incidents/{id}`.

## Alert to report

```mermaid
flowchart LR
    subgraph demo["otel-demo namespace"]
        flagd["flagd<br/>failure flags"] --> svc["Astronomy Shop<br/>microservices"]
        svc --> col["OTel Collector"]
        col --> prom[("Prometheus<br/>span metrics")]
        col --> os[("OpenSearch<br/>logs")]
        col --> jaeger[("Jaeger<br/>traces")]
        svc --> pg[("Postgres<br/>astronomy-db")]
        prom -- "HighErrorRate,<br/>HighLatencyP95" --> am["Alertmanager"]
    end

    subgraph agent["aoce namespace: AOCE agent"]
        webhook["POST /alerts<br/>bearer token"] --> store["Incident store<br/>dedup, queue, cap 200"]
        store --> loop["Investigation loop<br/>hypothesis, tool, result"]
        loop <--> claude(["Claude API"])
        loop --> registry["Tool registry<br/>read-only, audited"]
        loop --> report["Report<br/>finish_investigation<br/>or fallback"]
        report --> api["GET /incidents/{id}"]
    end

    am -- "webhook" --> webhook
    registry -- "query_metrics" --> prom
    registry -- "db_diagnostics" --> pg
    registry -- "search_logs" --> os
    registry -- "get_traces" --> jaeger
    api --> oncall["On-call engineer"]
```

## One investigation, step by step

```mermaid
sequenceDiagram
    autonumber
    participant AM as Alertmanager
    participant API as AOCE API
    participant Inv as Investigator
    participant C as Claude
    participant T as Tools
    participant B as Prometheus / Postgres /<br/>OpenSearch / Jaeger

    AM->>API: POST /alerts (firing alerts)
    API-->>AM: 202 accepted, or 503 if the queue is full
    API->>Inv: start incident (queued, then investigating)
    loop until reported or a limit is hit
        Inv->>C: alert, history, tool definitions
        C-->>Inv: tool call + hypothesis it tests
        Inv->>T: run tool (logged to audit)
        T->>B: read-only query
        B-->>T: data
        T-->>Inv: result + evidence (the exact query)
    end
    C-->>Inv: finish_investigation report
    Inv-->>API: concluded / inconclusive / failed
    Note over API: GET /incidents/{id} shows report, trace and audit log
```

## The pieces

| Stage | Where | What it does |
|---|---|---|
| Fault | flagd (`http://localhost:8080/feature`) | Turns on a failure scenario; ground truth is in [`eval/failure_catalogue.yaml`](../eval/failure_catalogue.yaml) |
| Detect | Prometheus rules in [`deploy/otel-demo/values.yaml`](../deploy/otel-demo/values.yaml) | `HighErrorRate` (over 5% failed for 1m) and `HighLatencyP95` (p95 over 1s for 2m) on server-span metrics |
| Notify | Alertmanager | POSTs firing alerts to `http://aoce-agent.aoce:8000/alerts` with the webhook bearer token |
| Accept | [`app.py`](../src/aoce/app.py), [`auth.py`](../src/aoce/auth.py) | Checks the token, opens one incident per firing alert, ignores re-sends, retries transient API failures up to 3 times |
| Queue | [`app.py`](../src/aoce/app.py) | 2 investigations at once, 20 more queued; beyond that the webhook answers 503 so Alertmanager retries |
| Investigate | [`investigation.py`](../src/aoce/investigation.py), [`llm.py`](../src/aoce/llm.py) | Claude forms a hypothesis, calls one tool to test it, reads the result, repeats. Limits: 12 steps, 300k tokens, 5 minutes |
| Gather evidence | [`tools/`](../src/aoce/tools/) | `query_metrics` (Prometheus), `db_diagnostics` (Postgres), `search_logs` (OpenSearch), `get_traces` (Jaeger). Read-only; each result carries the query that produced it |
| Audit | [`audit.py`](../src/aoce/audit.py) | One entry per tool call: time, tool, arguments, duration, outcome, result size |
| Report | `finish_investigation` | Summary, root cause, confidence, hypotheses with evidence, recommended action, what was and was not checked. If Claude never reports, a low-confidence fallback is built from the trace |
| Read | `GET /incidents/{id}` | Status, report, every tool call with its hypothesis, and the audit log |
