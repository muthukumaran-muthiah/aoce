# AI On-Call Engineer (AOCE)

Entry for the Dubai Chamber **Create AI Agents Championship** (Business category).
An AI investigation agent that answers "what changed and why did it break?" with cited evidence.
Jira project: AOCE.

## Local environment: Docker + Kubernetes + Tilt
Everything runs on local Kubernetes and is driven by Tilt. No manual scripts.

**Prerequisites**
- `ANTHROPIC_API_KEY` in a `.env` file at the repo root (`cp .env.example .env`, then fill it in; `.env` is gitignored). Exporting it in the shell that runs `tilt up` also works. Tilt puts it in a Kubernetes secret.
- Agent settings (model, effort, investigation limits, backend URLs) are in `AGENT_SETTINGS` in the Tiltfile; edit a value and Tilt redeploys the agent.
- Rancher Desktop with Kubernetes enabled, container engine **dockerd (moby)**, 4 CPUs and 10-12 GB memory
- `tilt` and `helm` on PATH
- kubectl context set to `rancher-desktop`

```bash
tilt up      # deploy everything, open the Tilt UI (http://localhost:10350)
tilt down    # remove everything
```

## What Tilt deploys
| Resource | Namespace | Purpose |
|---|---|---|
| AOCE agent API (`src/aoce`, `deploy/agent/agent.yaml`) | `aoce` | The agent: receives Alertmanager webhooks on `POST /alerts` |
| OpenTelemetry Demo (Helm chart 0.42.3, app 3.1.0) | `otel-demo` | Astronomy Shop: the microservices system the agent investigates |

Config:
- `deploy/otel-demo/values.yaml`: demo's own AI agent/chatbot/MCP disabled; explicit CPU/memory requests and limits per service (~4 GB reserved in total)
- `deploy/otel-demo/hpa.yaml`: CPU-based autoscaling (1-3 replicas) for 13 stateless services; needs metrics-server (bundled with Rancher Desktop)

Recommended Rancher Desktop VM: **4 CPUs, 10-12 GB memory**.

## Port-forwards (set up by Tilt)
| URL | What |
|---|---|
| http://localhost:8000/docs | AOCE agent API |
| http://localhost:8080 | Astronomy Shop |
| http://localhost:8080/jaeger/ui | Jaeger (traces) |
| http://localhost:8080/grafana | Grafana |
| http://localhost:8080/feature | Feature flags (failure injection) |
| http://localhost:8080/loadgen | Load generator |
| http://localhost:9090 | Prometheus API (metrics) |
| http://localhost:9200 | OpenSearch API (logs) |
| http://localhost:9093 | Alertmanager (sends alerts to the agent) |
| localhost:15432 | Postgres `astronomy-db` (has `pg_stat_statements`) |

## Failure scenarios (flagd)
Turn a flag on at http://localhost:8080/feature. The ground truth for every flag (true root cause,
mechanism with source links, expected alerts, metrics, traces, logs, decisive evidence, red herrings
and what a report must say to get credit) is in [`eval/failure_catalogue.yaml`](eval/failure_catalogue.yaml) (AOCE-22).
The 10 benchmark scenarios (AOCE-42):

| Flag | What breaks | Pages the agent today |
|---|---|---|
| `productCatalogLockContention` | Table lock on `catalog.products` blocks product reads (main demo) | HighLatencyP95 product-catalog |
| `paymentFailure` | payment rejects n% of charges | HighErrorRate payment, checkout |
| `paymentUnreachable` | checkout calls a non-existent payment address | HighErrorRate checkout |
| `productCatalogFailure` | GetProduct fails for one product (needs a targeting edit, see catalogue) | HighErrorRate product-catalog (borderline) |
| `intlShippingSlowdown` | ShipOrder +10s for non-US orders | HighLatencyP95 checkout |
| `recommendationCacheFailure` | Unbounded cache growth: latency and memory | HighLatencyP95 recommendation |
| `adHighCpu` | Busy-loop threads in ad starve the node | Maybe, via latency |
| `emailMemoryLeak` | Memory leak, OOMKilled restarts | No: needs a memory/restart rule |
| `kafkaQueueProblems` | fraud-detection consumer lag | No: needs a consumer-lag rule |
| `loadGeneratorFloodHomepage` | Traffic surge, no faulty service | HighLatencyP95 frontend (likely) |

Also catalogued: `adFailure`, `adManualGc`, `cartFailure`, `imageSlowLoad` (extras); `emitRawPii`
(not a fault, used for the PII redaction test, AOCE-39); `failedReadinessProbe` (no effect: the chart
sets no readiness probe on cart); `aiRunawayAgent` / `aiSlowResponse` (the demo's own agent is off).
Expected symptoms come from reading the demo's source; each entry is `verified: false` until a live run confirms it.

## Agent code
```
src/aoce/app.py      FastAPI app: /healthz, POST /alerts (Alertmanager webhook), /incidents, /incidents/{id}
src/aoce/audit.py    Audit log of every tool call (AOCE-40)
src/aoce/auth.py     Bearer-token checks for /alerts, /incidents and /tools (AOCE-61)
src/aoce/investigation.py  Hypothesis-driven investigation loop (AOCE-29)
src/aoce/alerts.py   Alertmanager webhook v4 models
src/aoce/llm.py      Claude client (claude-haiku-5-5 by default, effort high; refusal fallback on Sonnet/Opus)
src/aoce/config.py   Settings from AOCE_* env vars (see .env.example)
src/aoce/tools/      Read-only tools the agent calls: query_metrics (Prometheus), db_diagnostics (Postgres),
                     search_logs (OpenSearch), get_traces (Jaeger)
```

### Alerts to the agent (AOCE-21)
Prometheus evaluates two rules on span metrics from server spans: `HighErrorRate` (more than 5% failed
requests for 1 minute) and `HighLatencyP95` (p95 above 1s for 2 minutes). Alertmanager posts firing
alerts to `http://aoce-agent.aoce:8000/alerts`. Check that the agent received them:
`curl -s localhost:8000/incidents -H "Authorization: Bearer $AOCE_API_KEY"`.

### API authentication (AOCE-61)
Set `AOCE_WEBHOOK_TOKEN` and `AOCE_API_KEY` in `.env` (`openssl rand -hex 32` each). Tilt puts them in
Kubernetes secrets; Alertmanager reads the webhook token from a mounted secret and sends it as a bearer
token on `POST /alerts`. `/incidents` and `/tools` need `Authorization: Bearer $AOCE_API_KEY` (in
http://localhost:8000/docs use Authorize). An unset value turns its endpoints off (403); `/healthz` is open.

### Investigation loop (AOCE-29)
Each new firing alert starts an investigation in the background. Claude forms a hypothesis, calls a
read-only tool to test it (every call carries the hypothesis it tests), and repeats until it reports
through `finish_investigation`: summary, root cause, confidence, each hypothesis with its evidence,
recommended action, and what was and was not checked. The loop stops at 12 steps, 300k tokens or 5
minutes (`AOCE_INVESTIGATION_*`); near a limit Claude is told to report with what it has, and if it
still doesn't, the investigation ends as `inconclusive` with a low-confidence report built from its
tool calls (`"fallback": true`). At most 2 investigations run at once
(`AOCE_MAX_CONCURRENT_INVESTIGATIONS`) and up to 20 more wait as `queued`
(`AOCE_MAX_QUEUED_INVESTIGATIONS`). Beyond that, a new alert is dropped without being stored and
the webhook answers 503, so Alertmanager retries it with backoff. The last 200 incidents are kept in memory
(`AOCE_MAX_INCIDENTS`). Follow it at
`http://localhost:8000/incidents/<id>`: `status` goes `queued` → `investigating` → `concluded` /
`inconclusive` / `failed`, and `investigation.trace` lists every tool call with its hypothesis.
`audit` is the incident's append-only audit log (AOCE-40): one entry per tool call as it finishes, with
the time, tool, arguments, duration, outcome (`ok`, `error`, or `cancelled` when a timeout cut it off)
and result size. Each entry, and each call made by hand through `POST /tools/{name}` (actor
`operator`), is also logged as a JSON line on the `aoce.audit` logger; that log is the durable record,
since incidents live only in memory. A retried incident keeps the entries of its failed attempts. Without `ANTHROPIC_API_KEY` incidents are
marked `skipped`. Alertmanager re-sends of the same alert do not open a second incident, with two
exceptions: an investigation that failed on a transient API error (429, 5xx, network) is retried up
to 3 times, replacing the failed incident, and an alert whose incident has been dropped from memory
is investigated afresh.

### Trying a tool by hand
```bash
curl -s localhost:8000/tools/query_metrics -H "Authorization: Bearer $AOCE_API_KEY" -H 'content-type: application/json' \
  -d '{"signal":"latency_p95","service":null,"promql":null,"window_minutes":15,"end":null}'
curl -s localhost:8000/tools/db_diagnostics -H "Authorization: Bearer $AOCE_API_KEY" -H 'content-type: application/json' \
  -d '{"check":"blocking","limit":10}'
curl -s localhost:8000/tools/get_traces -H "Authorization: Bearer $AOCE_API_KEY" -H 'content-type: application/json' \
  -d '{"service":"product-catalog","kind":"slow","operation":null,"min_duration_ms":null,"window_minutes":15,"end":null,"limit":5}'
curl -s localhost:8000/tools/search_logs -H "Authorization: Bearer $AOCE_API_KEY" -H 'content-type: application/json' \
  -d '{"service":null,"min_severity":"error","text":null,"window_minutes":15,"end":null,"limit":20}'
```
Tests run in CI (`.github/workflows/ci.yml`). Locally, in Docker: `docker run --rm -v "$PWD":/app -w /app python:3.12-slim sh -c 'pip install -q -e ".[dev]" && pytest -q'`
