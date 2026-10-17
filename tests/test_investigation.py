import asyncio
import json
from types import SimpleNamespace

import pytest

from aoce.alerts import Alert
from aoce.audit import AuditEntry, recording
from aoce.config import Settings
from aoce.investigation import FINISH, Investigator, tool_definitions
from aoce.llm import LLMRefusalError
from aoce.tools import Tool, ToolError, ToolRegistry

REPORT = {
    "summary": "product-catalog is blocked on a lock on catalog.products.",
    "root_cause": "A long transaction holds a lock on catalog.products.",
    "confidence": "high",
    "hypotheses": [{"statement": "DB lock", "status": "confirmed", "evidence": ["pg_blocking_pids"]}],
    "recommended_action": "Turn off productCatalogLockContention.",
    "checked": ["metrics", "db"],
    "not_checked": ["logs"],
}

ALERT = Alert.model_validate(
    {
        "status": "firing",
        "labels": {"alertname": "HighErrorRate", "service_name": "product-catalog"},
        "annotations": {},
        "startsAt": "2026-10-16T08:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus:9090/graph",
        "fingerprint": "fp0",
    }
)


def tool_use(name, input, id="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=id)


def text(t):
    return SimpleNamespace(type="text", text=t)


def response(*blocks, tokens=100):
    usage = SimpleNamespace(input_tokens=tokens, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    return SimpleNamespace(content=list(blocks), usage=usage, stop_reason="end_turn")


class FakeLLM:
    def __init__(self, *responses, delay=0.0):
        self.responses = list(responses)
        self.calls = []
        self.delay = delay

    async def create(self, messages, *, system=None, tools=None):
        self.calls.append([dict(m) for m in messages])
        await asyncio.sleep(self.delay)
        return self.responses.pop(0)

    async def aclose(self):
        pass


def registry(calls=None):
    calls = calls if calls is not None else []

    async def echo(tool_input):
        calls.append(tool_input)
        if tool_input["x"] == "boom":
            raise ToolError("backend down")
        if tool_input["x"] == "bug":
            return {"x": {}["missing"]}
        if tool_input["x"] == "slow":
            await asyncio.sleep(1)
        return {"x": tool_input["x"]}

    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}
    return ToolRegistry([Tool("echo", "echo", schema, echo)])


def settings(**kw):
    return Settings(anthropic_api_key="k", **kw)


def test_tool_definitions_require_a_hypothesis_and_add_finish():
    reg = registry()
    defs = tool_definitions(reg)
    echo = defs[0]
    assert echo["input_schema"]["required"] == ["x", "hypothesis"]
    assert "hypothesis" in echo["input_schema"]["properties"]
    assert "hypothesis" not in reg.definitions()[0]["input_schema"]["properties"]  # registry untouched
    assert defs[-1]["name"] == FINISH and defs[-1]["strict"] is True


async def test_tool_then_finish_records_trace_and_strips_hypothesis():
    seen = []
    llm = FakeLLM(
        response(
            tool_use("echo", {"x": "a", "hypothesis": "h1"}, "t1"),
            tool_use("echo", {"x": "b", "hypothesis": "h2"}, "t2"),
        ),
        response(tool_use(FINISH, REPORT, "t3")),
    )
    result = await Investigator(llm, registry(seen), settings()).investigate(ALERT)

    assert result.status == "concluded" and result.stop_reason == "finished"
    assert result.report == REPORT
    assert result.steps == 2 and result.tokens_used == 200
    assert seen == [{"x": "a"}, {"x": "b"}]
    assert [(c.tool, c.hypothesis) for c in result.trace] == [("echo", "h1"), ("echo", "h2")]
    # Both tool results go back in one user message, after the full assistant content.
    second = llm.calls[1]
    assert second[1]["role"] == "assistant" and len(second[1]["content"]) == 2
    assert [b["tool_use_id"] for b in second[2]["content"]] == ["t1", "t2"]


async def test_tool_error_is_returned_to_the_model():
    llm = FakeLLM(response(tool_use("echo", {"x": "boom", "hypothesis": "h"})), response(tool_use(FINISH, REPORT)))
    result = await Investigator(llm, registry(), settings()).investigate(ALERT)
    block = llm.calls[1][2]["content"][0]
    assert block["is_error"] is True and block["content"] == "backend down"
    assert result.trace[0].is_error and result.status == "concluded"


async def test_slow_tool_times_out_as_an_error():
    llm = FakeLLM(response(tool_use("echo", {"x": "slow", "hypothesis": "h"})), response(tool_use(FINISH, REPORT)))
    result = await Investigator(llm, registry(), settings(tool_timeout_s=0.05)).investigate(ALERT)
    assert result.trace[0].is_error and "timed out" in result.trace[0].result_preview


async def test_step_limit_asks_for_a_report():
    llm = FakeLLM(
        response(tool_use("echo", {"x": "a", "hypothesis": "h"})),
        response(tool_use("echo", {"x": "b", "hypothesis": "h"})),
        response(tool_use(FINISH, {**REPORT, "root_cause": None, "confidence": "low"})),
    )
    result = await Investigator(llm, registry(), settings(investigation_max_steps=2)).investigate(ALERT)
    nudge = llm.calls[2][-1]["content"][-1]
    assert nudge["type"] == "text" and "max_steps" in nudge["text"]
    assert result.status == "inconclusive" and result.stop_reason == "finished"


async def test_ignoring_the_wrap_up_ends_inconclusive():
    llm = FakeLLM(
        response(tool_use("echo", {"x": "a", "hypothesis": "h"}), tokens=500),
        response(tool_use("echo", {"x": "b", "hypothesis": "h"})),
    )
    result = await Investigator(llm, registry(), settings(investigation_token_budget=400)).investigate(ALERT)
    assert result.status == "inconclusive" and result.stop_reason == "token_budget"
    assert len(result.trace) == 1  # the tool call after the wrap-up was not run
    report = result.report
    assert report["fallback"] and report["root_cause"] is None and report["confidence"] == "low"
    assert report["hypotheses"] == [
        {"statement": "h", "status": "inconclusive", "evidence": ['echo {"x": "a"}']}
    ]
    assert report["checked"] == ["echo"] and report["not_checked"] == []
    assert "token budget limit" in report["summary"] and "not what they returned" in report["summary"]
    assert "model_text" not in report


async def test_a_plain_text_answer_to_the_wrap_up_is_kept():
    llm = FakeLLM(response(tool_use("echo", {"x": "a", "hypothesis": "h"}), tokens=500), response(text("It's the DB.")))
    result = await Investigator(llm, registry(), settings(investigation_token_budget=400)).investigate(ALERT)
    assert result.report["fallback"] and result.report["model_text"] == "It's the DB."


async def test_ending_without_a_report_is_nudged_once():
    llm = FakeLLM(response(text("I think it is the DB.")), response(text("Still the DB.")))
    result = await Investigator(llm, registry(), settings()).investigate(ALERT)
    assert FINISH in llm.calls[1][-1]["content"]
    assert result.stop_reason == "no_report" and result.report["fallback"]
    assert result.report["model_text"] == "Still the DB." and "even after a reminder" in result.report["summary"]


async def test_hard_timeout():
    llm = FakeLLM(response(text("x")), delay=1)
    result = await Investigator(llm, registry(), settings(investigation_timeout_s=0.05)).investigate(ALERT)
    assert result.status == "inconclusive" and result.stop_reason == "timeout"
    assert result.report["fallback"] and "hard time limit" in result.report["summary"]


async def test_refusal_fails_the_investigation():
    class RefusingLLM:
        async def create(self, messages, *, system=None, tools=None):
            raise LLMRefusalError("request declined (category=cyber, request_id=req_1)")

    result = await Investigator(RefusingLLM(), registry(), settings()).investigate(ALERT)
    assert result.status == "failed" and result.stop_reason == "error"
    assert "declined" in result.error


async def test_unexpected_tool_exception_is_returned_to_the_model():
    content, is_error = await registry().run("echo", {"x": "bug"})
    assert is_error
    assert content == "echo failed unexpectedly: KeyError: 'missing'"

    llm = FakeLLM(response(tool_use("echo", {"x": "bug", "hypothesis": "h"})), response(tool_use(FINISH, REPORT, id="t2")))
    result = await Investigator(llm, registry(), settings()).investigate(ALERT)
    assert result.status == "concluded"
    assert result.trace[0].is_error
    tool_result = llm.calls[1][-1]["content"][0]
    assert tool_result["is_error"] and "KeyError" in tool_result["content"]


async def test_registry_that_raises_does_not_fail_the_investigation():
    class BrokenRegistry(ToolRegistry):
        async def run(self, name, tool_input):
            raise ValueError("bad JSON from Prometheus")

    broken = BrokenRegistry(registry()._tools.values())
    llm = FakeLLM(response(tool_use("echo", {"x": "a", "hypothesis": "h"})), response(tool_use(FINISH, REPORT, id="t2")))
    result = await Investigator(llm, broken, settings()).investigate(ALERT)
    assert result.status == "concluded"
    assert result.trace[0].is_error
    assert "ValueError: bad JSON from Prometheus" in result.trace[0].result_preview


@pytest.mark.parametrize(("status", "retryable"), [(529, True), (500, True), (429, True), (401, False), (400, False)])
async def test_only_transient_api_errors_are_retryable(status, retryable):
    import anthropic
    import httpx

    error = anthropic.APIStatusError(
        "boom", response=httpx.Response(status, request=httpx.Request("POST", "https://api")), body=None
    )

    class FailingLLM:
        async def create(self, messages, *, system=None, tools=None):
            raise error

    result = await Investigator(FailingLLM(), registry(), settings()).investigate(ALERT)
    assert result.status == "failed" and result.retryable is retryable


# AOCE-40: audit log of tool calls


def audit_lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage().removeprefix("tool call ")) for r in caplog.records if r.name == "aoce.audit"]


async def test_a_call_outside_an_investigation_is_logged_as_an_operator_call(caplog):
    caplog.set_level("INFO", logger="aoce.audit")
    content, _ = await registry().run("echo", {"x": "hi"})
    [line] = audit_lines(caplog)
    assert line["actor"] == "operator" and line["incident_id"] is None
    assert line["tool"] == "echo" and line["arguments"] == {"x": "hi"}
    assert line["outcome"] == "ok" and line["result_chars"] == len(content)
    assert line["duration_ms"] >= 0 and line["at"]


async def test_every_investigation_call_is_appended_to_the_incident(caplog):
    caplog.set_level("INFO", logger="aoce.audit")
    llm = FakeLLM(
        response(
            tool_use("echo", {"x": "a", "hypothesis": "h1"}, id="t1"),
            tool_use("echo", {"x": "boom", "hypothesis": "h2"}, id="t2"),
            tool_use("nope", {"hypothesis": "h3"}, id="t3"),
        ),
        response(tool_use(FINISH, REPORT)),
    )
    entries: list[AuditEntry] = []
    with recording("inc1", entries):
        await Investigator(llm, registry(), settings()).investigate(ALERT)

    assert {(e.tool, e.outcome) for e in entries} == {("echo", "ok"), ("echo", "error"), ("nope", "error")}
    assert all(e.incident_id == "inc1" and e.actor == "agent" for e in entries)
    assert {e.arguments.get("x") for e in entries} == {"a", "boom", None}
    assert all("hypothesis" not in e.arguments for e in entries)
    assert next(e for e in entries if e.outcome == "ok").result_chars == len('{"x": "a"}')
    # Each entry is also a log line, so the record survives the in-memory store.
    assert [line["tool"] for line in audit_lines(caplog)] == [e.tool for e in entries]


async def test_a_call_cut_off_by_the_tool_timeout_is_recorded_as_cancelled():
    llm = FakeLLM(response(tool_use("echo", {"x": "slow", "hypothesis": "h"})), response(tool_use(FINISH, REPORT)))
    entries: list[AuditEntry] = []
    with recording("inc1", entries):
        await Investigator(llm, registry(), settings(tool_timeout_s=0.05)).investigate(ALERT)
    [entry] = entries
    assert entry.outcome == "cancelled" and entry.result_chars == 0 and entry.duration_ms >= 40


async def test_recording_does_not_leak_past_its_block():
    entries: list[AuditEntry] = []
    with recording("inc1", entries):
        await registry().run("echo", {"x": "in"})
    await registry().run("echo", {"x": "out"})
    assert [e.arguments["x"] for e in entries] == ["in"]
