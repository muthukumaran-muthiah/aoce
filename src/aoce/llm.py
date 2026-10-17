"""Thin wrapper around the Anthropic Messages API used by the investigation loop (AOCE-29)."""

from typing import Any

import anthropic

from aoce.config import Settings

# Re-run a declined request on Anthropic's recommended fallback model instead of failing the investigation.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that support the server-side fallback. Haiku does not, so other models get no fallback parameter.
FALLBACK_MODEL_PREFIXES = ("claude-opus-", "claude-sonnet-", "claude-fable-")
# Models that reject output_config.effort (AOCE-68). Haiku 5.5 accepts it; Haiku 4.5 and Sonnet 4.5 do not.
NO_EFFORT_MODEL_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-3")


class LLMRefusalError(RuntimeError):
    """The model (and its fallback) declined the request."""


class LLMClient:
    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.settings = settings
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        await self._client.close()

    async def create(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Send one Messages API request. Tool calls are executed by the caller, not here."""
        params: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "messages": messages,
            # The system prompt and tool definitions repeat on every turn of an investigation.
            "cache_control": {"type": "ephemeral"},
        }
        if not self.settings.model.startswith(NO_EFFORT_MODEL_PREFIXES):
            params["output_config"] = {"effort": self.settings.effort}
        if self.settings.model.startswith(FALLBACK_MODEL_PREFIXES):
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools

        response = await self._client.beta.messages.create(**params)
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise LLMRefusalError(f"request declined (category={category}, request_id={response._request_id})")
        return response

    @staticmethod
    def text(response: Any) -> str:
        return "".join(block.text for block in response.content if block.type == "text")
