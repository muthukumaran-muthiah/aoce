# AI On-Call Engineer (AOCE)

Entry for the Dubai Chamber **Create AI Agents Championship** (Business category).
An AI investigation agent that answers "what changed and why did it break?" with cited evidence.
Jira project: AOCE.

## Local environment: Docker + Kubernetes + Tilt
Everything runs on local Kubernetes and is driven by Tilt. No manual scripts.

**Prerequisites**
- `ANTHROPIC_API_KEY` exported in the shell that runs `tilt up` (Tilt puts it in a Kubernetes secret)
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

## Failure scenarios (flagd) relevant to the agent
| Flag | Scenario | Agent capability tested |
|---|---|---|
| `productCatalogLockContention` | Lock contention on the product catalog DB | DB diagnostics (AOCE-57), main demo candidate |
| `paymentFailure` / `paymentUnreachable` | Payment errors / payment down | Error rate, dependency failure |
| `cartFailure` | Cart fails n% | Error rate |
| `kafkaQueueProblems` / `kafkaConsumerDead` | Consumer lag climbs | Queue backlog |
| `emailMemoryLeak` | Memory leak | Saturation |
| `adHighCpu` / `adManualGc` | CPU / GC pressure | Resource metrics |
| `intlShippingSlowdown` / `imageSlowLoad` | Latency | p95 latency + traces |
| `recommendationCacheFailure` | Cache failure | Dependency failure |
| `emitRawPii` | Raw PII in telemetry | PII redaction test (AOCE-39) |

Full catalogue with expected root causes: AOCE-22.

## Agent code
```
src/aoce/app.py      FastAPI app: /healthz, POST /alerts (Alertmanager webhook), /incidents, /incidents/{id}
src/aoce/investigation.py  Hypothesis-driven investigation loop (AOCE-29)
src/aoce/alerts.py   Alertmanager webhook v4 models
src/aoce/llm.py      Claude client (claude-opus-5-5, effort high, server-side refusal fallback)
src/aoce/config.py   Settings from AOCE_* env vars (see .env.example)
src/aoce/tools/      Read-only tools the agent calls: query_metrics (Prometheus), db_diagnostics (Postgres)
```

### Alerts to the agent (AOCE-21)
Prometheus evaluates two rules on span metrics from server spans: `HighErrorRate` (more than 5% failed
requests for 1 minute) and `HighLatencyP95` (p95 above 1s for 2 minutes). Alertmanager posts firing
alerts to `http://aoce-agent.aoce:8000/alerts`. Check that the agent received them at
http://localhost:8000/incidents.

### Investigation loop (AOCE-29)
Each new firing alert starts an investigation in the background. Claude forms a hypothesis, calls a
read-only tool to test it (every call carries the hypothesis it tests), and repeats until it reports
through `finish_investigation`: summary, root cause, confidence, each hypothesis with its evidence,
recommended action, and what was and was not checked. The loop stops at 12 steps, 300k tokens or 5
minutes (`AOCE_INVESTIGATION_*`); near a limit Claude is told to report with what it has, and an
investigation that never reports ends as `inconclusive`. Follow it at
`http://localhost:8000/incidents/<id>`: `status` goes `investigating` → `concluded` / `inconclusive` /
`failed`, and `investigation.trace` lists every tool call. Without `ANTHROPIC_API_KEY` incidents are
marked `skipped`. Alertmanager re-sends of the same alert do not open a second incident.

### Trying a tool by hand
```bash
curl -s localhost:8000/tools/query_metrics -H 'content-type: application/json' \
  -d '{"signal":"latency_p95","service":null,"promql":null,"window_minutes":15,"end":null}'
curl -s localhost:8000/tools/db_diagnostics -H 'content-type: application/json' \
  -d '{"check":"blocking","limit":10}'
```
Tests run in CI (`.github/workflows/ci.yml`). Locally, in Docker: `docker run --rm -v "$PWD":/app -w /app python:3.12-slim sh -c 'pip install -q -e ".[dev]" && pytest -q'`
