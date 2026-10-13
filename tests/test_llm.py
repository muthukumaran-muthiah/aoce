from types import SimpleNamespace

import pytest

from aoce.config import Settings
from aoce.llm import FALLBACK_BETA, LLMClient, LLMRefusalError


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **params):
        self.calls.append(params)
        return self.response


def fake_client(response):
    messages = FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def response(stop_reason="end_turn", text="hello"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        content=[SimpleNamespace(type="text", text=text)],
        _request_id="req_1",
    )


async def test_create_sends_model_effort_and_fallbacks():
    client, messages = fake_client(response())
    llm = LLMClient(Settings(model="claude-opus-5-5", effort="high"), client=client)

    resp = await llm.create([{"role": "user", "content": "hi"}], system="sys", tools=[{"name": "t"}])

    params = messages.calls[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["output_config"] == {"effort": "high"}
    assert params["betas"] == [FALLBACK_BETA]
    assert params["fallbacks"] == "default"
    assert params["system"] == "sys"
    assert params["tools"] == [{"name": "t"}]
    assert LLMClient.text(resp) == "hello"


async def test_refusal_raises():
    client, _ = fake_client(response(stop_reason="refusal"))
    llm = LLMClient(Settings(), client=client)
    with pytest.raises(LLMRefusalError, match="cyber"):
        await llm.create([{"role": "user", "content": "hi"}])
