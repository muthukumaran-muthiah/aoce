# Private model options (AOCE-41)

AOCE sends the model what it finds while investigating: alert labels, metric values, log lines, trace
summaries and database diagnostics. For an enterprise that cannot send that data to a public API endpoint,
there are four ways to run the agent against Claude while keeping traffic inside its own cloud account or
network. This page says what each one is and which settings change.

Claude's weights cannot be self-hosted. "Self-hosted" here means the agent and everything it reads run in your
cluster, and the model is reached through a private route you control.

## At a glance

| Option | Who runs the model | Data path | Billing | Code change in AOCE |
|---|---|---|---|---|
| Claude API (today) | Anthropic | Public HTTPS to `api.anthropic.com` | Anthropic account | None |
| Your own gateway or proxy | Anthropic or a cloud provider behind it | Agent -> your gateway -> provider | Whatever the gateway uses | None (settings only) |
| Amazon Bedrock | AWS | Inside your AWS account and region | AWS bill | Yes, the provider setting (see Follow-up) |
| Google Vertex AI | Google Cloud | Inside your GCP project and region | GCP bill | Yes, the provider setting (see Follow-up); also no fallbacks |
| Claude Platform on AWS | Anthropic, on AWS infrastructure | AWS endpoint, IAM-signed | AWS Marketplace | Yes, the provider setting (see Follow-up) |

All four keep the same model behaviour, tools and prompts. The investigation loop, tool registry and limits do
not change.

## What the agent sends today

`src/aoce/llm.py` builds one `anthropic.AsyncAnthropic` client and calls `client.beta.messages.create` with:

- `model` from `AOCE_MODEL` (default `claude-haiku-5-5`)
- `output_config.effort` from `AOCE_EFFORT`, only for model families known to accept it (`AOCE_SEND_EFFORT` overrides)
- top-level `cache_control` (automatic prompt caching)
- the server-side `fallbacks: "default"` beta, only for model IDs starting with `claude-opus-`, `claude-sonnet-` or `claude-fable-`

`src/aoce/app.py` turns investigation off when `ANTHROPIC_API_KEY` is unset. Those three request features and
that check are what matter when you change provider.

## Option 1: a gateway or proxy (no code change)

Use this when your company already routes LLM traffic through an internal gateway (an API gateway, an LLM
proxy, or an egress proxy with logging and DLP). `llm.py` passes only `api_key` to `AsyncAnthropic`, and the
SDK then takes the base URL from `ANTHROPIC_BASE_URL`, so the agent talks to the gateway instead of
`api.anthropic.com`.

| Setting | Where | Value |
|---|---|---|
| `ANTHROPIC_BASE_URL` | Not in `AGENT_SETTINGS` yet: add `'ANTHROPIC_BASE_URL': '<url>'` to it in the Tiltfile (or set it in `.env` when running locally) | Your gateway URL, e.g. `https://llm-gateway.internal.example.com` |
| `ANTHROPIC_API_KEY` | `.env` (Tilt puts it in the `aoce-anthropic` secret) | The key the gateway expects. It must be set: the agent does not investigate without it |
| `AOCE_MODEL` | `AGENT_SETTINGS` | The model name the gateway routes on |

The gateway must:

- speak the Anthropic Messages API (`POST /v1/messages`) and pass `x-api-key` and `anthropic-beta` headers through;
- accept top-level `cache_control` and `output_config.effort`, or you set `AOCE_SEND_EFFORT=false`;
- pass through or ignore `fallbacks` and its `anthropic-beta` header. The agent sends them whenever `AOCE_MODEL`
  starts with `claude-opus-`, `claude-sonnet-` or `claude-fable-`, whatever the gateway's backend is. A gateway
  that rejects them fails every investigation. If it does (or it fronts Bedrock or Vertex), use a Haiku model,
  for which none is sent, until the provider switch in [Follow-up](#follow-up-a-provider-setting) lands.

If the gateway only offers an OpenAI-compatible endpoint, it will not work: the agent uses the Anthropic SDK.

## Option 2: Amazon Bedrock

Claude runs inside your AWS account. Prompts and responses stay in the region you call, under your AWS
data terms, IAM policies and CloudTrail logging. A VPC interface endpoint (PrivateLink) keeps the traffic off
the public internet; check that the endpoint you use offers one in your region.

The client classes in options 2 to 4 ship in the `anthropic` versions `pyproject.toml` already allows
(`>=1.13,<2`; checked against 1.13.0 and 1.15.0). The snippets are illustrative: `settings.aws_region` and the
`gcp_*` fields do not exist in `Settings` until the [Follow-up](#follow-up-a-provider-setting) adds them.

Code change in `LLMClient.__init__`: build the Bedrock client from the same SDK instead of `AsyncAnthropic`.

```python
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle

client = AsyncAnthropicBedrockMantle(aws_region=settings.aws_region)
```

Install `anthropic[bedrock]` (adds `boto3`). The client exposes the same `beta.messages.create`, so nothing
else in the loop changes.

| Setting | Value |
|---|---|
| `AOCE_MODEL` | Bedrock model ID, with the `anthropic.` prefix: `anthropic.claude-haiku-5-5`, `anthropic.claude-sonnet-5-5`, `anthropic.claude-opus-5-5`. Check your region's model catalogue first |
| `AWS_REGION` | Region to call, e.g. `me-central-1` or `eu-central-1` |
| AWS credentials | The pod's IAM role (EKS Pod Identity or IRSA) allowed to invoke the model on Bedrock; or `AWS_BEARER_TOKEN_BEDROCK` for a Bedrock API key |
| `ANTHROPIC_API_KEY` | Not used. The `app.py` check must accept a provider instead (see Follow-up) |

Request features on Bedrock:

- **Effort:** supported. The effort check already matches prefixed IDs such as `anthropic.claude-haiku-5-5`.
- **Automatic caching (`cache_control`):** supported on the Messages API endpoint this client uses. The older
  `bedrock-runtime` integration (`AsyncAnthropicBedrock`, Opus 4.6 and earlier) rejects it with a 400, so do not use that client.
- **Server-side fallbacks:** not available. Prefixed IDs do not match the fallback check, so none is sent; a
  refusal fails the investigation and the trace fallback report (AOCE-64) still gets written.

## Option 3: Google Vertex AI

Claude runs inside your GCP project. Data stays in the region you choose, under Google Cloud's data terms,
IAM and audit logs. Private Service Connect keeps the traffic on Google's network.

```python
from anthropic.lib.vertex import AsyncAnthropicVertex

client = AsyncAnthropicVertex(project_id=settings.gcp_project_id, region=settings.gcp_region)
```

Install `anthropic[vertex]` (adds `google-auth`). Authentication is Application Default Credentials: Workload
Identity on GKE, or `gcloud auth application-default login` on a laptop. No Anthropic key.

| Setting | Value |
|---|---|
| `AOCE_MODEL` | Bare model ID, no prefix: `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`. Check your region's Model Garden first |
| `ANTHROPIC_VERTEX_PROJECT_ID` | GCP project ID (the SDK reads it when `project_id` is not passed) |
| `CLOUD_ML_REGION` | `global`, a multi-region (`us`, `eu`), or one region. Pick one region when data must not leave it |
| `GOOGLE_APPLICATION_CREDENTIALS` | Only outside GKE: path to a service-account key file |
| `ANTHROPIC_API_KEY` | Not used (see Follow-up) |

Request features on Vertex:

- **Effort and automatic caching:** supported.
- **Server-side fallbacks:** not available, and **this one needs the code change**. Vertex model IDs are
  bare, so `claude-opus-*` and `claude-sonnet-*` match the fallback check and the request is rejected.
  With the default Haiku model nothing is sent and it works as is.

## Option 4: Claude Platform on AWS

Anthropic runs the API on AWS infrastructure, with IAM (SigV4) authentication and AWS Marketplace billing.
Unlike Bedrock it has the same features as the Claude API on the same day, including server-side fallbacks, and
uses bare model IDs. Choose it when procurement wants an AWS contract but the agent should behave exactly as it
does today.

```python
from anthropic import AsyncAnthropicAWS

client = AsyncAnthropicAWS()
```

Install `anthropic[aws]`. It needs `AWS_REGION` and `ANTHROPIC_AWS_WORKSPACE_ID` (there is no default for
either) and AWS credentials from the pod's IAM role. `AOCE_MODEL` stays `claude-haiku-5-5`.

## Data residency on the Claude API itself

If only the region matters and a public endpoint is acceptable, the Claude API takes an `inference_geo`
request parameter (for example `"us"`) that pins where inference runs. Bedrock and Vertex do not use it; their
region setting does the same job. The agent does not send it yet.

## Follow-up: a provider setting

Options 2 to 4 need one code change, which is not part of this docs-only ticket:

1. Add `AOCE_LLM_PROVIDER` (`anthropic` | `bedrock` | `vertex` | `aws`) plus the provider fields above to
   `Settings` in `src/aoce/config.py`.
2. Build the matching client in `LLMClient.__init__`.
3. Send `fallbacks` only when the provider is `anthropic` or `aws`.
4. In `app.py`, require `ANTHROPIC_API_KEY` only for the `anthropic` provider.
5. Add the provider's credentials to the Tiltfile and `deploy/agent/agent.yaml` (an IAM role or Workload
   Identity rather than a secret where possible).

## Summary (for the pitch deck, AOCE-41)

- The agent and every system it reads (Prometheus, OpenSearch, Jaeger, Postgres) run in the customer's cluster.
- The model call is the one thing that leaves the cluster, and it carries the evidence the agent gathered
  (metric values, log lines, trace summaries, query statistics). It can go to Bedrock or Vertex in the
  customer's own cloud account and region, or through the customer's own gateway.
- A gateway works with settings alone. Bedrock, Vertex and Claude Platform on AWS need the provider setting in
  [Follow-up](#follow-up-a-provider-setting). Prompts, tools and investigation limits stay the same.
