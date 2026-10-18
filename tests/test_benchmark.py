"""The benchmark (eval/benchmark.yaml, AOCE-42) and its runner, against a fake stack with a fake clock."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("run_benchmark", ROOT / "eval" / "run_benchmark.py")
rb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rb)

SCENARIOS = rb.load_benchmark()
CATALOGUE = yaml.safe_load((ROOT / "eval" / "failure_catalogue.yaml").read_text())
# Variants of each flag in src/flagd/demo.flagd.json of demo 3.1.0.
ON_OFF = ["off", "on"]
PERCENT = ["10%", "100%", "25%", "50%", "75%", "90%", "off"]
VARIANTS = {
    "productCatalogLockContention": ON_OFF, "paymentFailure": PERCENT, "paymentUnreachable": ON_OFF,
    "productCatalogFailure": ON_OFF, "adHighCpu": ON_OFF, "recommendationCacheFailure": ON_OFF,
    "emailMemoryLeak": ["10000x", "1000x", "100x", "10x", "1x", "off"], "kafkaQueueProblems": ON_OFF,
    "intlShippingSlowdown": ["10sec", "5sec", "off"], "loadGeneratorFloodHomepage": ON_OFF,
}


def test_benchmark_is_the_catalogues_ten_benchmark_scenarios():
    flags = [s["flag"] for s in SCENARIOS]
    assert len(flags) == 10 == len(set(flags))
    assert set(flags) == {s["flag"] for s in CATALOGUE["scenarios"] if s["tier"] == "benchmark"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["flag"])
def test_scenario_is_runnable(scenario):
    assert scenario["variant"] in VARIANTS[scenario["flag"]]
    assert scenario["expected"]["root_cause"] and scenario["expected"]["grading"]["must_identify"]
    entry = next(s for s in CATALOGUE["scenarios"] if s["flag"] == scenario["flag"])
    trigger = scenario["trigger"]
    if trigger["kind"] == "synthetic":
        # Synthetic pages stand in for a missing rule only; once a rule pages for it, use it instead.
        assert not entry["alerts"] and entry.get("gap")
        assert trigger["after_s"] > 0 and trigger["alert"]["labels"]["alertname"]
    else:
        assert trigger["kind"] == "alert" and trigger["expect"]
        predicted = {(a["rule"], a["service"]) for a in entry["alerts"]}
        assert {(e["alertname"], e["service"]) for e in trigger["expect"]} <= predicted


class FakeStack:
    """flagd UI, Prometheus and the agent. A flag that is on pages its expected alerts after
    `alert_delay` seconds; each incident concludes `investigate_for` seconds after it opens."""

    def __init__(self, alert_delay: float = 180, investigate_for: float = 120, recover_for: float = 60) -> None:
        self.t = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
        self.flags = {f: {"state": "ENABLED", "variants": {v: v for v in vs}, "defaultVariant": "off"}
                      for f, vs in VARIANTS.items()}
        self.flags["productCatalogFailure"]["targeting"] = {"if": [{"==": [{"var": "product_id"}, "OLJCESPC7Z"]}, "off", "off"]}
        self.alert_delay, self.investigate_for, self.recover_for = alert_delay, investigate_for, recover_for
        self.on_since: dict[str, datetime] = {}
        self.off_at: dict[str, datetime] = {}
        self.incidents: list[dict] = []
        self.paged: set[tuple[str, str]] = set()
        self.writes: list[dict] = []
        self.webhooks: list[dict] = []

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)

    def clock(self):
        return rb.Clock(now=lambda: self.t, sleep=self.sleep)

    def _open(self, alertname: str, service: str) -> None:
        self.incidents.append({
            "id": f"inc{len(self.incidents)}", "received_at": self.t.isoformat(), "status": "queued",
            "alert": {"labels": {"alertname": alertname, "service_name": service}},
            "investigation": None, "_opened": self.t,
        })

    def _tick(self) -> None:
        by_flag = {s["flag"]: s for s in SCENARIOS}
        for flag, since in self.on_since.items():
            if self.t - since >= timedelta(seconds=self.alert_delay):
                for e in by_flag[flag]["trigger"].get("expect", []):
                    if (flag, e["service"]) not in self.paged:
                        self.paged.add((flag, e["service"]))
                        self._open(e["alertname"], e["service"])
        for i in self.incidents:
            if i["status"] == "queued" and self.t - i["_opened"] >= timedelta(seconds=self.investigate_for):
                i["status"] = "concluded"
                i["investigation"] = {"steps": 5, "tokens_used": 1000, "report": {"root_cause": "it was the flag"}}

    def _alerting(self) -> list[dict]:
        names = [f for f in self.on_since] + [f for f, t in self.off_at.items()
                                               if self.t - t < timedelta(seconds=self.recover_for)]
        return [{"labels": {"alertname": "HighErrorRate", "service_name": n}, "state": "firing"} for n in names]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self._tick()
        path = request.url.path
        if path == "/feature/api/read":
            return httpx.Response(200, json={"flags": json.loads(json.dumps(self.flags))})
        if path == "/feature/api/write":
            new = json.loads(request.content)["data"]["flags"]
            self.writes.append(new)
            for flag, spec in new.items():
                was_on = self.flags[flag]["defaultVariant"] != "off"
                now_on = spec["defaultVariant"] != "off"
                if now_on and not was_on:
                    self.on_since[flag] = self.t
                if was_on and not now_on:
                    self.on_since.pop(flag, None)
                    self.off_at[flag] = self.t
            self.flags = new
            return httpx.Response(200, json={})
        if path == "/api/v1/alerts":
            return httpx.Response(200, json={"status": "success", "data": {"alerts": self._alerting()}})
        if path == "/incidents":
            assert request.headers["authorization"] == "Bearer api-key"
            return httpx.Response(200, json=[{k: v for k, v in i.items() if k != "_opened"} for i in self.incidents])
        if path == "/alerts" and request.method == "POST":
            assert request.headers["authorization"] == "Bearer hook"
            body = json.loads(request.content)
            self.webhooks.append(body)
            labels = body["alerts"][0]["labels"]
            self._open(labels["alertname"], labels["service_name"])
            return httpx.Response(202, json={"incidents": [self.incidents[-1]["id"]]})
        return httpx.Response(404)


def make_runner(stack: FakeStack) -> rb.Runner:
    client = httpx.Client(transport=httpx.MockTransport(stack.handle))
    clock = stack.clock()
    return rb.Runner(
        rb.Flagd(client, "http://flagd/feature", clock),
        rb.Agent(client, "http://agent", "api-key", "hook"),
        rb.Prometheus(client, "http://prom"),
        clock, log=lambda _: None,
    )


def scenario(flag: str) -> dict:
    return next(s for s in SCENARIOS if s["flag"] == flag)


def test_alert_scenario_records_pages_reports_and_restores_flags():
    stack = FakeStack()
    runner = make_runner(stack)
    baseline = runner.flagd.read()

    result = runner.run(scenario("paymentFailure"), baseline)

    assert result["paged"] and result["warnings"] == []
    assert {(i["alertname"], i["service"]) for i in result["incidents"]} == {
        ("HighErrorRate", "payment"), ("HighErrorRate", "checkout")}
    assert all(i["status"] == "concluded" and i["report"] for i in result["incidents"])
    assert 180 <= result["time_to_page_s"] <= 190
    assert result["expected_alerts_missing"] == []
    assert result["expected"]["culprit"] == "payment"
    assert stack.writes[0]["paymentFailure"]["defaultVariant"] == "50%"
    assert stack.flags == baseline  # switched back off
    assert not stack._alerting()  # waited for recovery


def test_targeting_override_is_applied_then_restored():
    stack = FakeStack()
    runner = make_runner(stack)
    baseline = runner.flagd.read()

    runner.run(scenario("productCatalogFailure"), baseline)

    assert stack.writes[0]["productCatalogFailure"]["targeting"]["if"][1] == "on"
    assert stack.flags["productCatalogFailure"]["targeting"]["if"][1] == "off"


def test_synthetic_scenario_posts_the_alert_after_the_fault_develops():
    stack = FakeStack()
    runner = make_runner(stack)
    baseline = runner.flagd.read()

    result = runner.run(scenario("kafkaQueueProblems"), baseline)

    assert len(stack.webhooks) == 1
    labels = stack.webhooks[0]["alerts"][0]["labels"]
    assert labels["alertname"] == "KafkaConsumerLagHigh" and labels["service_name"] == "fraud-detection"
    assert result["paged"] and 300 <= result["time_to_page_s"] <= 310
    assert result["synthetic_incidents"] == [result["incidents"][0]["id"]]
    assert result["incidents"][0]["status"] == "concluded"


def test_no_page_is_recorded_not_raised():
    stack = FakeStack(alert_delay=10_000)
    runner = make_runner(stack)
    baseline = runner.flagd.read()

    result = runner.run(scenario("adHighCpu"), baseline)

    assert not result["paged"] and result["incidents"] == []
    assert result["expected_alerts_missing"] == [{"alertname": "HighLatencyP95", "service": "ad"}]
    assert any("no page" in w for w in result["warnings"])
    assert stack.flags == baseline


def test_unknown_variant_is_rejected_before_anything_changes():
    stack = FakeStack()
    runner = make_runner(stack)
    with pytest.raises(ValueError, match="no variant"):
        runner.flagd.set("paymentFailure", "60%")
    assert stack.writes == []


def test_list_needs_no_stack(capsys):
    assert rb.main(["--list", "--only", "kafkaQueueProblems"]) == 0
    out = capsys.readouterr().out
    assert "synthetic KafkaConsumerLagHigh after 300s" in out and "fraud-detection" in out
