"""Keeps the failure catalogue (eval/failure_catalogue.yaml, AOCE-22) consistent with the deployment."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = yaml.safe_load((ROOT / "eval" / "failure_catalogue.yaml").read_text())
VALUES = yaml.safe_load((ROOT / "deploy" / "otel-demo" / "values.yaml").read_text())
SCENARIOS = CATALOGUE["scenarios"]

# Flags in src/flagd/demo.flagd.json of OpenTelemetry Demo 3.1.0 (Helm chart 0.42.3).
# When the chart is upgraded, update this list and the catalogue together.
DEMO_FLAGS = {
    "adFailure", "adHighCpu", "adManualGc", "aiRunawayAgent", "aiSlowResponse", "cartFailure",
    "emailMemoryLeak", "emitRawPii", "failedReadinessProbe", "imageSlowLoad", "intlShippingSlowdown",
    "kafkaQueueProblems", "loadGeneratorFloodHomepage", "paymentFailure", "paymentUnreachable",
    "productCatalogFailure", "productCatalogLockContention", "recommendationCacheFailure",
}
TIERS = {"benchmark", "extra", "not_a_fault", "not_observable", "out_of_scope"}
SCORED_TIERS = {"benchmark", "extra"}
SCORED = [s for s in SCENARIOS if s["tier"] in SCORED_TIERS]


def alert_rule_names() -> set[str]:
    groups = VALUES["prometheus"]["serverFiles"]["alerting_rules.yml"]["groups"]
    return {rule["alert"] for group in groups for rule in group["rules"]}


def test_catalogue_matches_the_deployed_chart():
    assert CATALOGUE["demo"]["chart"].endswith("0.42.3")
    flags = [s["flag"] for s in SCENARIOS]
    assert len(flags) == len(set(flags)), "a flag is listed twice"
    assert set(flags) == DEMO_FLAGS


def test_every_scenario_has_a_known_tier():
    assert {s["tier"] for s in SCENARIOS} <= TIERS


def test_benchmark_has_ten_scenarios():
    # AOCE-42: benchmark of 10 failure scenarios.
    assert len([s for s in SCENARIOS if s["tier"] == "benchmark"]) == 10


@pytest.mark.parametrize("scenario", SCORED, ids=lambda s: s["flag"])
def test_scored_scenarios_are_complete(scenario):
    for field in ("variant", "category", "culprit", "root_cause", "mechanism", "source",
                  "symptoms", "decisive_evidence", "red_herrings", "tools"):
        assert scenario.get(field), f"{scenario['flag']} is missing {field}"
    assert scenario["grading"]["must_identify"]
    assert scenario["grading"]["recommended_action"]
    # A scenario no rule pages for must say so, so the gap is not forgotten.
    assert scenario["alerts"] or scenario.get("gap")


@pytest.mark.parametrize("scenario", SCORED, ids=lambda s: s["flag"])
def test_alerts_name_existing_rules(scenario):
    rules = alert_rule_names()
    for alert in scenario["alerts"]:
        assert alert["rule"] in rules
        assert alert["service"]
