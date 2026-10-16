"""Runtime settings, read from environment variables prefixed AOCE_ (plus ANTHROPIC_API_KEY)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AOCE_", env_file=".env", extra="ignore")

    # LLM
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = 16000

    # Investigation limits (AOCE-29): model turns, wall-clock seconds, and total tokens across turns.
    investigation_max_steps: int = 12
    investigation_timeout_s: float = 300
    investigation_token_budget: int = 300_000
    tool_timeout_s: float = 30

    # Observability backends (defaults are in-cluster service names in namespace otel-demo)
    prometheus_url: str = "http://prometheus.otel-demo:9090"
    # Where a human opens Prometheus; used for clickable evidence links.
    prometheus_public_url: str = "http://localhost:9090"
    opensearch_url: str = "http://opensearch.otel-demo:9200"
    opensearch_logs_index: str = "otel-logs-*"
    jaeger_url: str = "http://jaeger.otel-demo:16686"

    # Application database, read with a monitoring role (AOCE-57)
    database_url: str | None = None

    # Change history (AOCE-23, AOCE-28)
    github_repo: str | None = None
    github_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
