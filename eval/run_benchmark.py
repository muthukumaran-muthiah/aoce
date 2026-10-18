"""Run the failure benchmark (AOCE-42) against a live `tilt up` stack and record what the agent concluded.

For each scenario in eval/benchmark.yaml: wait until nothing is alerting, switch the flag on in flagd,
wait for the agent to be paged (or post the synthetic alert), collect the incidents it opens and their
reports, switch the flag off, and wait for the stack to recover. Every flag is restored to how it was
found at the end, even on Ctrl-C. Results go to eval/results/<run id>/, one JSON file per scenario plus
summary.json, each next to the catalogue's ground truth for scoring (AOCE-43).

    python eval/run_benchmark.py --list
    python eval/run_benchmark.py --dry-run
    python eval/run_benchmark.py --only paymentFailure,paymentUnreachable

Needs the Tilt port-forwards (flagd UI via :8080, Prometheus :9090, agent :8000) and, from the
environment or the repo's .env, AOCE_API_KEY (to read /incidents) and AOCE_WEBHOOK_TOKEN (to post
synthetic alerts).
"""

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parent
FINISHED = ("concluded", "inconclusive", "failed", "skipped")
# Catalogue fields copied into each result as the ground truth to score against.
EXPECTED_FIELDS = ("culprit", "root_cause", "decisive_evidence", "red_herrings", "grading", "giveaway_logs")


def load_benchmark(path: Path = EVAL / "benchmark.yaml", catalogue: Path = EVAL / "failure_catalogue.yaml") -> list[dict]:
    """Benchmark scenarios, each with its catalogue entry under `expected` and defaults filled in."""
    bench = yaml.safe_load(path.read_text())
    entries = {s["flag"]: s for s in yaml.safe_load(catalogue.read_text())["scenarios"]}
    scenarios = []
    for s in bench["scenarios"]:
        entry = entries.get(s["flag"])
        if entry is None:
            raise ValueError(f"{s['flag']} is not in the failure catalogue")
        if entry["tier"] != "benchmark":
            raise ValueError(f"{s['flag']} is tier {entry['tier']} in the catalogue, not benchmark")
        scenarios.append({**bench["defaults"], **s, "expected": {k: entry.get(k) for k in EXPECTED_FIELDS}})
    return scenarios


@dataclass
class Clock:
    """Wall clock and sleep, swapped for a fake one in tests."""

    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep


class Flagd:
    """flagd UI API, served through the frontend proxy at /feature/api (demo 3.1.0)."""

    def __init__(self, client: httpx.Client, url: str, clock: Clock) -> None:
        self.client, self.url, self.clock = client, url.rstrip("/"), clock

    def read(self) -> dict[str, Any]:
        r = self.client.get(f"{self.url}/api/read")
        r.raise_for_status()
        return r.json()["flags"]

    def write(self, flags: dict[str, Any]) -> None:
        """Replace the whole flag file, then wait until a read returns it (the UI applies writes asynchronously)."""
        r = self.client.post(f"{self.url}/api/write", json={"data": {"flags": flags}})
        r.raise_for_status()
        for _ in range(20):
            if self.read() == flags:
                return
            self.clock.sleep(0.5)
        raise RuntimeError("flagd did not apply the flag change")

    def set(self, flag: str, variant: str, targeting: Any = None, base: dict[str, Any] | None = None) -> None:
        flags = json.loads(json.dumps(base if base is not None else self.read()))
        if variant not in flags[flag]["variants"]:
            raise ValueError(f"{flag} has no variant {variant!r}; it has {sorted(flags[flag]['variants'])}")
        flags[flag]["defaultVariant"] = variant
        if targeting is not None:
            flags[flag]["targeting"] = targeting
        self.write(flags)


class Agent:
    def __init__(self, client: httpx.Client, url: str, api_key: str | None, webhook_token: str | None) -> None:
        self.client, self.url = client, url.rstrip("/")
        self.api_key, self.webhook_token = api_key, webhook_token

    def incidents(self) -> list[dict[str, Any]]:
        r = self.client.get(f"{self.url}/incidents", headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        return r.json()

    def page(self, labels: dict[str, str], annotations: dict[str, str], now: datetime) -> list[str]:
        """Post one firing alert as Alertmanager would; returns the incident ids it opened."""
        alert = {
            "status": "firing",
            "labels": labels,
            "annotations": annotations,
            "startsAt": now.isoformat(),
            "fingerprint": f"benchmark-{uuid.uuid4().hex[:12]}",
        }
        body = {
            "version": "4",
            "groupKey": f"benchmark:{labels.get('alertname')}",
            "status": "firing",
            "receiver": "aoce-benchmark",
            "commonLabels": labels,
            "alerts": [alert],
        }
        r = self.client.post(f"{self.url}/alerts", json=body, headers={"Authorization": f"Bearer {self.webhook_token}"})
        r.raise_for_status()
        return r.json()["incidents"]


class Prometheus:
    def __init__(self, client: httpx.Client, url: str) -> None:
        self.client, self.url = client, url.rstrip("/")

    def alerting(self) -> list[str]:
        """Alerts pending or firing now, as 'alertname/service'."""
        r = self.client.get(f"{self.url}/api/v1/alerts")
        r.raise_for_status()
        return [f"{a['labels'].get('alertname')}/{a['labels'].get('service_name', '')}" for a in r.json()["data"]["alerts"]]


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class Runner:
    flagd: Flagd
    agent: Agent
    prometheus: Prometheus
    clock: Clock = field(default_factory=Clock)
    poll_s: float = 10
    quiet_timeout_s: float = 900
    conclusion_timeout_s: float = 900
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True)

    def wait_quiet(self) -> list[str]:
        """Wait until no alert is pending or firing and no investigation is running. Returns what is still
        alerting if that never happens within quiet_timeout_s, so the result can say the run was not clean."""
        deadline = self._after(self.quiet_timeout_s)
        while True:
            busy = self.prometheus.alerting()
            busy += [f"incident {i['id']} {i['status']}" for i in self.agent.incidents() if i["status"] not in FINISHED]
            if not busy or self.clock.now() >= deadline:
                return busy
            self.clock.sleep(self.poll_s)

    def run(self, scenario: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
        flag = scenario["flag"]
        result: dict[str, Any] = {
            "flag": flag,
            "variant": scenario["variant"],
            "trigger": scenario["trigger"]["kind"],
            "expected_alerts": scenario["trigger"].get("expect", []),
            "expected": scenario["expected"],
            "warnings": [],
        }
        if busy := self.wait_quiet():
            result["warnings"].append(f"stack was not quiet before injecting: {busy}")
        seen_before = {i["id"] for i in self.agent.incidents()}

        self.log(f"{flag}: switching on ({scenario['variant']})")
        injected_at = self.clock.now()
        result["injected_at"] = injected_at.isoformat()
        try:
            self.flagd.set(flag, scenario["variant"], scenario.get("targeting"), base=baseline)
            incidents = self._collect(scenario, injected_at, seen_before, result)
        finally:
            self.log(f"{flag}: switching off")
            self.flagd.write(baseline)
            result["reverted_at"] = self.clock.now().isoformat()

        result["incidents"] = [_summarize(i, injected_at) for i in incidents]
        result["paged"] = bool(incidents)
        result["time_to_page_s"] = min((i["time_to_page_s"] for i in result["incidents"]), default=None)
        fired = {(i["alertname"], i["service"]) for i in result["incidents"]}
        result["expected_alerts_missing"] = [
            e for e in result["expected_alerts"] if (e["alertname"], e["service"]) not in fired
        ]
        if busy := self.wait_quiet():
            result["warnings"].append(f"stack did not recover after the flag was switched off: {busy}")
        return result

    def _collect(self, scenario: dict[str, Any], injected_at: datetime, seen_before: set[str],
                 result: dict[str, Any]) -> list[dict[str, Any]]:
        """Poll /incidents until the first page plus settle_s, then until every incident has finished."""
        trigger = scenario["trigger"]
        page_deadline = self._after(scenario["page_timeout_s"], injected_at)
        synthetic_at = self._after(trigger["after_s"], injected_at) if trigger["kind"] == "synthetic" else None
        first_page: datetime | None = None
        while True:
            now = self.clock.now()
            if synthetic_at is not None and now >= synthetic_at:
                self.log(f"{scenario['flag']}: posting synthetic {trigger['alert']['labels']['alertname']}")
                result["synthetic_incidents"] = self.agent.page(
                    trigger["alert"]["labels"], trigger["alert"].get("annotations", {}), now)
                synthetic_at = None
                page_deadline = max(page_deadline, self._after(self.poll_s * 3))
            incidents = [i for i in self.agent.incidents() if i["id"] not in seen_before]
            if incidents and first_page is None:
                first_page = now
                self.log(f"{scenario['flag']}: paged ({len(incidents)} incident(s))")
            if first_page is None:
                if now >= page_deadline and synthetic_at is None:
                    result["warnings"].append(f"no page within {scenario['page_timeout_s']}s")
                    return []
            else:
                settled = now >= self._after(scenario["settle_s"], first_page)
                if settled and all(i["status"] in FINISHED for i in incidents):
                    return incidents
                if now >= self._after(self.conclusion_timeout_s, first_page):
                    result["warnings"].append(f"investigations still running after {self.conclusion_timeout_s}s")
                    return incidents
            self.clock.sleep(self.poll_s)

    def _after(self, seconds: float, start: datetime | None = None) -> datetime:
        return datetime.fromtimestamp((start or self.clock.now()).timestamp() + seconds, UTC)


def _summarize(incident: dict[str, Any], injected_at: datetime) -> dict[str, Any]:
    alert = incident["alert"]
    investigation = incident.get("investigation") or {}
    received = _parse_time(incident["received_at"])
    return {
        "id": incident["id"],
        "alertname": alert["labels"].get("alertname"),
        "service": alert["labels"].get("service_name") or alert["labels"].get("service"),
        "status": incident["status"],
        "time_to_page_s": round((received - injected_at).total_seconds(), 1),
        "steps": investigation.get("steps"),
        "tokens_used": investigation.get("tokens_used"),
        "report": investigation.get("report"),
        "error": investigation.get("error"),
    }


def load_dotenv(path: Path) -> None:
    """Fill unset environment variables from KEY=value lines (the same .env that Tilt reads)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#") and value and key not in os.environ:
            os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--list", action="store_true", help="print the scenarios and their expected root causes, then exit")
    p.add_argument("--dry-run", action="store_true", help="check the stack is reachable and print the plan; change nothing")
    p.add_argument("--only", help="comma-separated flags to run (default: all 10)")
    p.add_argument("--agent-url", default="http://localhost:8000")
    p.add_argument("--flagd-url", default="http://localhost:8080/feature")
    p.add_argument("--prometheus-url", default="http://localhost:9090")
    p.add_argument("--out", type=Path, default=EVAL / "results")
    args = p.parse_args(argv)

    scenarios = load_benchmark()
    if args.only:
        wanted = set(args.only.split(","))
        unknown = wanted - {s["flag"] for s in scenarios}
        if unknown:
            p.error(f"not in the benchmark: {', '.join(sorted(unknown))}")
        scenarios = [s for s in scenarios if s["flag"] in wanted]

    if args.list:
        for s in scenarios:
            t = s["trigger"]
            how = ", ".join(f"{e['alertname']}/{e['service']}" for e in t.get("expect", [])) or \
                f"synthetic {t['alert']['labels']['alertname']} after {t['after_s']}s"
            print(f"{s['flag']}={s['variant']}  pages: {how}\n  culprit: {s['expected']['culprit']}\n"
                  f"  root cause: {' '.join(s['expected']['root_cause'].split())}\n")
        return 0

    load_dotenv(ROOT / ".env")
    api_key, token = os.environ.get("AOCE_API_KEY"), os.environ.get("AOCE_WEBHOOK_TOKEN")
    if not api_key:
        p.error("AOCE_API_KEY is not set (environment or .env); it is needed to read /incidents")
    if not token and any(s["trigger"]["kind"] == "synthetic" for s in scenarios):
        p.error("AOCE_WEBHOOK_TOKEN is not set (environment or .env); synthetic scenarios post alerts with it")

    with httpx.Client(timeout=30) as client:
        clock = Clock()
        runner = Runner(Flagd(client, args.flagd_url, clock), Agent(client, args.agent_url, api_key, token),
                        Prometheus(client, args.prometheus_url), clock)
        baseline = runner.flagd.read()
        missing = [s["flag"] for s in scenarios if s["flag"] not in baseline]
        if missing:
            p.error(f"flagd has no flag {', '.join(missing)}: is this demo 3.1.0?")
        alerting = runner.prometheus.alerting()
        runner.agent.incidents()
        if args.dry_run:
            print(f"stack reachable; {len(scenarios)} scenario(s) would run; alerting now: {alerting or 'nothing'}")
            for s in scenarios:
                print(f"  {s['flag']} -> {s['variant']} ({s['trigger']['kind']})")
            return 0

        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = args.out / run_id
        out.mkdir(parents=True, exist_ok=True)
        results = []
        try:
            for s in scenarios:
                result = runner.run(s, baseline)
                (out / f"{s['flag']}.json").write_text(json.dumps(result, indent=2, default=str))
                results.append(result)
        finally:
            runner.flagd.write(baseline)
            summary = {
                "run_id": run_id,
                "scenarios": [
                    {k: r[k] for k in ("flag", "paged", "time_to_page_s", "expected_alerts_missing", "warnings")}
                    | {"statuses": [i["status"] for i in r["incidents"]]}
                    for r in results
                ],
            }
            (out / "summary.json").write_text(json.dumps(summary, indent=2))
            print(f"results in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
