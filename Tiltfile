# AI On-Call Engineer (AOCE) - local dev environment
# Run:  tilt up      Stop: tilt down
# Requires: Kubernetes (Rancher Desktop), helm, tilt.

load('ext://helm_remote', 'helm_remote')
load('ext://namespace', 'namespace_create')
load('ext://secret', 'secret_from_dict')

OTEL_DEMO_NS = 'otel-demo'
OTEL_DEMO_CHART_VERSION = '0.42.3'   # app 3.1.0

# ---------------------------------------------------------------------------
# Demo system the agent investigates: OpenTelemetry Astronomy Shop (AOCE-19)
# Includes Prometheus (metrics), Jaeger (traces), OpenSearch (logs), Grafana,
# Postgres with pg_stat_statements, and flagd for failure injection.
# ---------------------------------------------------------------------------
namespace_create(OTEL_DEMO_NS)

helm_remote(
    'opentelemetry-demo',
    repo_name='open-telemetry',
    repo_url='https://open-telemetry.github.io/opentelemetry-helm-charts',
    version=OTEL_DEMO_CHART_VERSION,
    release_name='otel-demo',
    namespace=OTEL_DEMO_NS,
    values=['deploy/otel-demo/values.yaml'],
)
watch_file('deploy/otel-demo/values.yaml')

# Autoscaling for stateless request-path services (deploy/otel-demo/hpa.yaml).
k8s_yaml('deploy/otel-demo/hpa.yaml')
AUTOSCALED = ['frontend', 'frontend-proxy', 'ad', 'cart', 'checkout', 'currency', 'email',
              'image-provider', 'payment', 'product-catalog', 'quote', 'recommendation', 'shipping']

def hpa(name):
    return [name + ':horizontalpodautoscaler'] if name in AUTOSCALED else []

# Entry point: shop, Jaeger, Grafana, feature flags and load generator are all
# served through the frontend proxy on :8080.
k8s_resource('frontend-proxy', labels=['demo'], port_forwards=['8080:8080'], objects=hpa('frontend-proxy'),
    links=[
        link('http://localhost:8080/', 'Astronomy Shop'),
        link('http://localhost:8080/jaeger/ui/', 'Jaeger'),
        link('http://localhost:8080/grafana/', 'Grafana'),
        link('http://localhost:8080/feature/', 'Feature flags'),
        link('http://localhost:8080/loadgen/', 'Load generator'),
    ])

# Observability APIs the agent's tools will call (AOCE-25, 26, 27, 57).
k8s_resource('prometheus', labels=['observability'], port_forwards=['9090:9090'],
    links=[link('http://localhost:9090/', 'Prometheus')])
k8s_resource('jaeger', labels=['observability'])
k8s_resource('alertmanager', labels=['observability'], port_forwards=['9093:9093'],
    links=[link('http://localhost:9093/', 'Alertmanager')])
k8s_resource('grafana', labels=['observability'])
k8s_resource('opensearch', labels=['observability'], port_forwards=['9200:9200'])
k8s_resource('otel-collector-agent', labels=['observability'])
k8s_resource('opamp-server', labels=['observability'])

# Stateful / singletons.
k8s_resource('astronomy-db', labels=['demo'], port_forwards=['15432:5432'])
k8s_resource('flagd', labels=['demo'])

# Remaining shop services (autoscaled ones get their HPA attached).
for svc in ['accounting', 'ad', 'cart', 'checkout', 'currency', 'email',
            'fraud-detection', 'frontend', 'image-provider', 'kafka',
            'load-generator', 'payment', 'product-catalog', 'quote',
            'recommendation', 'shipping', 'telemetry-docs', 'valkey-cart']:
    k8s_resource(svc, labels=['demo'], objects=hpa(svc))

# ---------------------------------------------------------------------------
# The AOCE agent API (AOCE-24). Rebuilt and redeployed on every code change.
# ---------------------------------------------------------------------------
AOCE_NS = 'aoce'
namespace_create(AOCE_NS)

# Secrets. Put ANTHROPIC_API_KEY in a .env file at the repo root (copy .env.example;
# .env is gitignored), or export it in the shell that runs `tilt up`. Never put the
# key in this file: it is committed.
load('ext://dotenv', 'dotenv')
if os.path.exists('.env'):
    dotenv('.env')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
if not ANTHROPIC_API_KEY:
    warn('ANTHROPIC_API_KEY is not set: alerts will open incidents but nothing will investigate them. ' +
         'Add it to .env and Tilt reloads.')
watch_file('.env')
k8s_yaml(secret_from_dict('aoce-anthropic', namespace=AOCE_NS,
    inputs={'ANTHROPIC_API_KEY': ANTHROPIC_API_KEY}))

# API authentication (AOCE-61), also from .env. AOCE_WEBHOOK_TOKEN is shared with Alertmanager, which
# sends it as a bearer token on every webhook; AOCE_API_KEY is for you, on /incidents and /tools.
AOCE_WEBHOOK_TOKEN = os.getenv('AOCE_WEBHOOK_TOKEN', '')
AOCE_API_KEY = os.getenv('AOCE_API_KEY', '')
if not AOCE_WEBHOOK_TOKEN:
    warn('AOCE_WEBHOOK_TOKEN is not set: the agent rejects every alert. Add it to .env (e.g. openssl rand -hex 32).')
if not AOCE_API_KEY:
    warn('AOCE_API_KEY is not set: /incidents and /tools are turned off. Add it to .env to read incidents.')
k8s_yaml(secret_from_dict('aoce-auth', namespace=AOCE_NS,
    inputs={'AOCE_WEBHOOK_TOKEN': AOCE_WEBHOOK_TOKEN, 'AOCE_API_KEY': AOCE_API_KEY}))
k8s_yaml(secret_from_dict('aoce-webhook', namespace=OTEL_DEMO_NS, inputs={'token': AOCE_WEBHOOK_TOKEN}))

# Agent settings, passed to the pod as environment variables (see src/aoce/config.py).
# Edit a value here and Tilt redeploys the agent. URLs are in-cluster service names.
AGENT_SETTINGS = {
    'AOCE_MODEL': 'claude-haiku-5-5',       # cheapest; claude-sonnet-5-5 or claude-opus-5-5 for harder cases
    'AOCE_EFFORT': 'high',                     # low | medium | high | xhigh | max
    'AOCE_MAX_TOKENS': '16000',
    # Investigation limits (AOCE-29)
    'AOCE_INVESTIGATION_MAX_STEPS': '12',
    'AOCE_INVESTIGATION_TIMEOUT_S': '300',
    'AOCE_INVESTIGATION_TOKEN_BUDGET': '300000',
    'AOCE_TOOL_TIMEOUT_S': '30',
    # Backends the tools query
    'AOCE_PROMETHEUS_URL': 'http://prometheus.otel-demo:9090',
    'AOCE_PROMETHEUS_PUBLIC_URL': 'http://localhost:9090',
    'AOCE_OPENSEARCH_URL': 'http://opensearch.otel-demo:9200',
    'AOCE_JAEGER_URL': 'http://jaeger.otel-demo:16686',
    # Demo-only monitoring role from the OTel Demo's init.sql (pg_monitor, read-only use).
    'AOCE_DATABASE_URL': 'postgresql://monitoring_user:monitoring_password@astronomy-db.otel-demo:5432/astronomy_db',
}

def agent_manifests():
    objs = decode_yaml_stream(read_file('deploy/agent/agent.yaml'))
    for obj in objs:
        if obj['kind'] == 'Deployment':
            pod = obj['spec']['template']
            container = pod['spec']['containers'][0]
            container['env'] = container.get('env', []) + [
                {'name': k, 'value': v} for k, v in AGENT_SETTINGS.items()]
            # Restart the pod when a secret changes (the secret alone would not).
            pod['metadata'].setdefault('annotations', {})['aoce/api-key-hash'] = str(
                hash(ANTHROPIC_API_KEY + AOCE_WEBHOOK_TOKEN + AOCE_API_KEY))
    return encode_yaml_stream(objs)

docker_build('aoce-agent', '.', only=['pyproject.toml', 'README.md', 'src'])
k8s_yaml(agent_manifests())
k8s_resource('aoce-agent', labels=['agent'], port_forwards=['8000:8000'],
    # /incidents needs the API key, so open it from /docs (Authorize) or curl rather than a plain link.
    links=[link('http://localhost:8000/docs', 'Agent API')])
