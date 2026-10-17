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
    assert params["cache_control"] == {"type": "ephemeral"}
    assert params["system"] == "sys"
    assert params["tools"] == [{"name": "t"}]
    assert LLMClient.text(resp) == "hello"


async def test_haiku_omits_fallbacks():
    client, messages = fake_client(response())
    llm = LLMClient(Settings(model="claude-haiku-5-5"), client=client)

    await llm.create([{"role": "user", "content": "hi"}])

    params = messages.calls[0]
    assert params["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in params
    assert "betas" not in params


async def test_refusal_raises():
    client, _ = fake_client(response(stop_reason="refusal"))
    llm = LLMClient(Settings(model="claude-opus-5-5"), client=client)
    with pytest.raises(LLMRefusalError, match="cyber"):
        await llm.create([{"role": "user", "content": "hi"}])


async def test_aclose_closes_the_http_client():
    closed = []

    async def close():
        closed.append(True)

    llm = LLMClient(Settings(anthropic_api_key="k"), client=SimpleNamespace(close=close))
    await llm.aclose()
    assert closed == [True]


async def test_real_client_can_be_closed():
    llm = LLMClient(Settings(anthropic_api_key="k"))
    await llm.aclose()
    assert llm._client.is_closed()


async def test_effort_is_omitted_for_models_that_reject_it():
    client, messages = fake_client(response())
    await LLMClient(Settings(model="claude-haiku-4-5"), client=client).create([{"role": "user", "content": "hi"}])
    await LLMClient(Settings(model="claude-haiku-5-5", effort="low"), client=client).create([{"role": "user", "content": "hi"}])

    assert "output_config" not in messages.calls[0]
    assert messages.calls[1]["output_config"] == {"effort": "low"}
