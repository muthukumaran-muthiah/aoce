"""Thin wrapper around the Anthropic Messages API used by the investigation loop (AOCE-29)."""

from typing import Any

import anthropic

from aoce.config import Settings

# Re-run a declined request on Anthropic's recommended fallback model instead of failing the investigation.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMRefusalError(RuntimeError):
    """The model (and its fallback) declined the request."""


class LLMClient:
    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.settings = settings
        self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

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
            "output_config": {"effort": self.settings.effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
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
