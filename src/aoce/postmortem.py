"""Markdown postmortem for a finished incident (AOCE-36).

Built from what the agent already recorded, with no further Claude call: the alert gives the impact,
the audit log (AOCE-40) gives the timeline, and the investigation report gives the root cause,
hypotheses and action items. A fallback report (AOCE-64) or a failed or skipped investigation still
renders; the document then says no root cause was established. Creating follow-up Jira tickets from
the action items is not done here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aoce.app import Incident

_STATUS_LINES = {
    "concluded": "The agent concluded the investigation.",
    "inconclusive": "The agent finished without establishing a root cause.",
    "failed": "The investigation failed before the agent reported.",
    "skipped": "The incident was not investigated (no Anthropic API key was set).",
}


def _when(at: datetime | None) -> str:
    return at.strftime("%Y-%m-%d %H:%M:%S UTC") if at else "unknown"


def _utc(at: datetime) -> datetime:
    """Any time in UTC; webhook times without an offset are taken as UTC, so all of them compare."""
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def _one_line(text: Any) -> str:
    """Model and label text on a single Markdown line: newlines folded, no stray list or heading."""
    return " ".join(str(text).split())


def _resolved_at(incident: Incident) -> datetime | None:
    # Alertmanager sends 0001-01-01T00:00:00Z as endsAt while an alert is still firing.
    ends_at = incident.alert.ends_at
    return _utc(ends_at) if ends_at and ends_at.year > 1 else None


def render_postmortem(incident: Incident) -> str:
    alert = incident.alert
    investigation = incident.investigation
    report: dict[str, Any] = (investigation.report if investigation else None) or {}
    alertname = alert.labels.get("alertname", "alert")
    service = alert.service or "unknown service"
    started_at = _utc(alert.starts_at)
    resolved_at = _resolved_at(incident)

    lines = [f"# Postmortem: {_one_line(alertname)} on {_one_line(service)}", ""]
    lines += [f"Incident `{incident.id}`. {_STATUS_LINES.get(incident.status, '')}".rstrip(), ""]
    if report.get("fallback"):
        lines += ["> This report was assembled from the agent's tool calls because Claude never sent its own.", ""]

    lines += ["## Summary", "", _one_line(report.get("summary") or "No summary was produced."), ""]

    lines += ["## Impact", ""]
    lines.append(f"- Service: {_one_line(service)}")
    lines.append(f"- Alert: {_one_line(alertname)}" + (
        f" (severity {_one_line(alert.labels['severity'])})" if "severity" in alert.labels else ""
    ))
    if summary := alert.annotations.get("summary") or alert.annotations.get("description"):
        lines.append(f"- Symptom: {_one_line(summary)}")
    lines.append(f"- Started: {_when(started_at)}")
    if resolved_at:
        minutes = max(int((resolved_at - started_at).total_seconds() // 60), 0)
        lines.append(f"- Resolved: {_when(resolved_at)} (about {minutes} min)")
    else:
        lines.append("- Resolved: not recorded")
    lines.append("")

    lines += ["## Timeline", ""]
    events = [(started_at, "Alert started firing."), (_utc(incident.received_at), "Agent received the alert.")]
    for entry in incident.audit:
        outcome = "" if entry.outcome == "ok" else f" ({entry.outcome})"
        events.append((_utc(entry.at), f"Ran `{entry.tool}` in {entry.duration_ms} ms{outcome}."))
    if resolved_at:
        events.append((resolved_at, "Alert resolved."))
    lines += [f"- {_when(at)}: {text}" for at, text in sorted(events, key=lambda e: e[0])]
    lines.append("")

    lines += ["## Root cause", ""]
    if root_cause := report.get("root_cause"):
        lines.append(f"{_one_line(root_cause)} (confidence: {_one_line(report.get('confidence', 'unknown'))})")
    else:
        lines.append("Not established.")
    if investigation and investigation.error:
        lines.append(f"\nThe investigation ended with an error: {_one_line(investigation.error)}")
    lines.append("")

    if hypotheses := report.get("hypotheses"):
        lines += ["## Hypotheses", ""]
        for h in hypotheses:
            lines.append(f"- **{_one_line(h.get('status', 'inconclusive'))}**: {_one_line(h.get('statement', ''))}")
            lines += [f"  - {_one_line(e)}" for e in h.get("evidence", [])]
        lines.append("")

    lines += ["## Action items", ""]
    actions = []
    if recommended := report.get("recommended_action"):
        actions.append(_one_line(recommended))
    actions += [f"Check {_one_line(item)}, which the agent did not." for item in report.get("not_checked", [])]
    if not root_cause:
        actions.append("Find the root cause by hand; the agent did not establish one.")
    lines += [f"- [ ] {action}" for action in actions]
    lines.append("")

    if investigation:
        lines += ["## Investigation", ""]
        lines.append(
            f"- Stopped: {investigation.stop_reason} after {investigation.steps} step(s), "
            f"{investigation.tokens_used} tokens, {len(incident.audit)} tool call(s), "
            f"attempt {incident.attempts}"
        )
        if checked := report.get("checked"):
            lines.append("- Checked: " + ", ".join(_one_line(c) for c in checked))
        lines.append("")

    return "\n".join(lines)
