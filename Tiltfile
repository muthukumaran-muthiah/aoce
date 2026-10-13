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
# ANTHROPIC_API_KEY is read from the shell that runs `tilt up`.
# ---------------------------------------------------------------------------
AOCE_NS = 'aoce'
namespace_create(AOCE_NS)
k8s_yaml(secret_from_dict('aoce-anthropic', namespace=AOCE_NS,
    inputs={'ANTHROPIC_API_KEY': os.getenv('ANTHROPIC_API_KEY', '')}))
docker_build('aoce-agent', '.', only=['pyproject.toml', 'README.md', 'src'])
k8s_yaml('deploy/agent/agent.yaml')
k8s_resource('aoce-agent', labels=['agent'], port_forwards=['8000:8000'],
    links=[link('http://localhost:8000/docs', 'Agent API')])
