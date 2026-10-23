"""State of the SDLC stages and the lifecycle diagram shown in the UI."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

from .i18n import t


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    agent: str
    artifact: str
    description: str
    phase: str


STAGES = (
    Stage("planning", "Planning", "Planning Agent", "planning.md", "Scope, stakeholders, milestones and risks", "Requirements engineering"),
    Stage("analysis", "Analysis", "Analysis Agent", "requirements.md", "FR/NFR, user stories, acceptance criteria", "Requirements engineering"),
    Stage("design", "Design", "Design Agent", "architecture.md", "Component/sequence diagrams, API contract, data schema", "Design and verification"),
    Stage("review", "Review", "Architecture Review Agent", "design_review.md", "Independent audit of the design against the requirements", "Design and verification"),
    Stage("development", "Development", "Development Agent", "source code", "Working code with the chosen technology, Dockerfile, project.yaml", "Implementation"),
    Stage("testing", "Testing", "Test Agent", "test_report.md", "Unit/API/UI tests, traceability matrix", "Implementation"),
    Stage("deployment", "Deployment", "Deployment Agent", "deployment.md", "Compose, environment variables, health checks and rollback", "Operations"),
    Stage("maintenance", "Maintenance", "Maintenance Agent", "maintenance_report.md", "Monitoring, incident response, improvement list", "Operations"),
)
STAGE_KEYS = tuple(stage.key for stage in STAGES)
STAGE_NAMES = {stage.key: stage.label for stage in STAGES}


def stage_label(key: str) -> str:
    return t(STAGE_NAMES[key]) if key in STAGE_NAMES else key


def stage_description(key: str) -> str:
    return t(next(stage.description for stage in STAGES if stage.key == key))
RUNNING_EVENTS = {
    "started", "repair_started", "answered", "gate_failed",
    "revision_started", "changes_requested", "approved", "traceability", "tests_protected",
}
# When a repair finishes it is the test's turn, when a design revision finishes it is the review's.
HANDOFFS = {("development", "repair_completed"): "testing", ("design", "revision_completed"): "review"}


def stage_states(events: list[dict], run_status: str) -> dict[str, str]:
    """Returns pending / running / question / done / skipped / stale / failed / paused for every stage.

    "skipped": the change request does not affect this stage, so its previous document was kept.
    "stale": the stage had completed but an earlier stage's document changed afterwards; it needs updating.
    """
    states = {key: "pending" for key in STAGE_KEYS}
    for event in events:
        stage, status = event["stage"], event["status"]
        if stage == "workflow" and status == "reset" and event["detail"] in states:
            for key in STAGE_KEYS[STAGE_KEYS.index(event["detail"]):]:
                states[key] = "pending"
            continue
        if stage == "workflow" and status == "stale" and (event["detail"] in states or event["detail"] == "decisions"):
            # "decisions": the architecture decisions changed, so every stage is affected.
            first = 0 if event["detail"] == "decisions" else STAGE_KEYS.index(event["detail"]) + 1
            for key in STAGE_KEYS[first:]:
                if states[key] in {"done", "skipped"}:
                    states[key] = "stale"
            continue
        if stage not in states:
            continue
        if (stage, status) in HANDOFFS:
            states[stage] = "done"
            states[HANDOFFS[(stage, status)]] = "running"
        elif status in RUNNING_EVENTS:
            states[stage] = "running"
        elif status == "question":
            states[stage] = "question"
        elif status in {"completed", "passed", "edited"}:
            states[stage] = "done"
        elif status == "skipped":
            states[stage] = "skipped"
        elif status == "failed":
            states[stage] = "failed"
    if run_status != "running":
        for key, value in states.items():
            if value in {"running", "question"}:
                states[key] = "paused" if run_status == "paused" else "failed"
    return states


def counters(events: list[dict]) -> dict[str, object]:
    """Loop counters and traceability coverage over the project's whole history.

    Regeneration does not reset the counters: if a loop-back happened even once, it stays visible as part of the process.
    A loop-back is counted the moment it starts (revision_started / repair_started).
    """
    result: dict[str, object] = {"repairs": 0, "test_runs": 0, "review_rounds": 0, "revisions": 0, "questions": 0, "coverage": None}
    for event in events:
        stage, status = event["stage"], event["status"]
        if stage == "development" and status == "repair_started":
            result["repairs"] += 1
        elif stage == "testing" and status in {"passed", "failed"}:
            result["test_runs"] += 1
        elif stage == "review" and status in {"approved", "changes_requested"}:
            result["review_rounds"] += 1
        elif stage == "design" and status == "revision_started":
            result["revisions"] += 1
        elif status == "question":
            result["questions"] += 1
        elif stage == "testing" and status == "traceability":
            # "FR coverage 80%" (current) or "FR kapsamı %80" (runs recorded before the details were stored in English).
            found = re.search(r"(\d+)\s*%|%\s*(\d+)", event["detail"])
            result["coverage"] = (found.group(1) or found.group(2)) if found else None
    return result


SEGMENT_STARTS = {"started", "repair_started", "revision_started", "answered"}
SEGMENT_ENDS = {"completed", "passed", "failed", "repair_completed", "revision_completed", "question", "changes_requested"}
# After a repair the test, after a design revision the review runs again without a new "started" event.
RERUNS = {("development", "repair_completed"): "testing", ("design", "revision_completed"): "review"}
RUN_STARTS = {"started", "repair_started", "revision_started"}


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def stage_metrics(events: list[dict], now: datetime | None = None) -> dict[str, dict]:
    """Per-stage run count and active time in seconds (time spent waiting for an answer excluded)."""
    now = now or datetime.now(timezone.utc)
    metrics = {key: {"runs": 0, "seconds": 0.0, "open": None, "last_started": None} for key in STAGE_KEYS}
    for event in events:
        item = metrics.get(event["stage"])
        status = event["status"]
        if event["stage"] == "workflow" and status in {"paused", "failed", "interrupted"}:
            for value in metrics.values():
                if value["open"] is not None:
                    value["seconds"] += (_parse_time(event["created_at"]) - value["open"]).total_seconds()
                    value["open"] = None
            continue
        if item is None:
            continue
        moment = _parse_time(event["created_at"])
        if status in RUN_STARTS:
            item["runs"] += 1
            item["last_started"] = event["created_at"]
        if status in SEGMENT_STARTS and item["open"] is None:
            item["open"] = moment
        elif status in SEGMENT_ENDS and item["open"] is not None:
            item["seconds"] += (moment - item["open"]).total_seconds()
            item["open"] = None
        follower = RERUNS.get((event["stage"], status))
        if follower:
            metrics[follower]["runs"] += 1
            metrics[follower]["open"] = metrics[follower]["open"] or moment
    for item in metrics.values():
        if item["open"] is not None:
            item["seconds"] += max(0.0, (now - item["open"]).total_seconds())
        item["running"] = item.pop("open") is not None
    return metrics


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return "—"
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return t("{hours}h {minutes}m", hours=hours, minutes=minutes)
    return t("{minutes}m {seconds:02d}s", minutes=minutes, seconds=secs) if minutes else t("{seconds}s", seconds=secs)


PALETTE = {
    "pending": ("#f1f5f9", "#94a3b8", "#64748b", "Pending"),
    "running": ("#dbeafe", "#2563eb", "#1e3a8a", "Running"),
    "question": ("#fef3c7", "#d97706", "#78350f", "Waiting for answer"),
    "done": ("#dcfce7", "#16a34a", "#14532d", "Completed"),
    "failed": ("#fee2e2", "#dc2626", "#7f1d1d", "Failed"),
    "paused": ("#fff7ed", "#ea580c", "#7c2d12", "Paused"),
    "stale": ("#fef9c3", "#ca8a04", "#713f12", "Out of date"),
    "skipped": ("#f8fafc", "#16a34a", "#475569", "Skipped (not affected)"),
}
GLYPHS = {"done": "✓", "failed": "✕", "question": "?", "paused": "❚❚", "stale": "↻", "skipped": "↷"}


def _node_x(index: int) -> int:
    # Four phases × two stages; nodes of the same phase share a background band.
    return 15 + (index // 2) * 245 + (60 if index % 2 == 0 else 170)


def render_svg(states: dict[str, str] | None = None, loop_counts: dict[str, object] | None = None,
               selected: str | None = None, durations: dict[str, str] | None = None,
               runs: dict[str, int] | None = None) -> str:
    """Draws the lifecycle as a single SVG; without states it produces the introductory view."""
    states = states or {key: "pending" for key in STAGE_KEYS}
    loop_counts = loop_counts or {}
    y = 128
    parts = [
        '<svg viewBox="0 0 1000 262" width="100%" role="img" aria-label="' + escape(t("SDLC lifecycle")) + '" '
        'xmlns="http://www.w3.org/2000/svg" style="font-family:Inter,Segoe UI,Helvetica,Arial,sans-serif">',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#64748b"/></marker>',
        '<marker id="loop-on" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#ea580c"/></marker>',
        '<marker id="loop-off" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#cbd5e1"/></marker>',
        "</defs>",
        "<style>.sdlc-halo{animation:sdlc-pulse 1.6s ease-out infinite}"
        "@keyframes sdlc-pulse{0%{opacity:.85;stroke-width:2}100%{opacity:0;stroke-width:16}}</style>",
    ]
    phases = list(dict.fromkeys(stage.phase for stage in STAGES))
    for index, phase in enumerate(phases):
        x = 15 + index * 245
        parts.append(f'<rect x="{x}" y="72" width="230" height="150" rx="14" fill="#f8fafc" stroke="#e2e8f0"/>')
        parts.append(f'<text x="{x + 115}" y="246" text-anchor="middle" font-size="13" font-weight="600" fill="#475569">{escape(t(phase))}</text>')
    for index in range(len(STAGES) - 1):
        x1, x2 = _node_x(index) + 31, _node_x(index + 1) - 33
        parts.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>')
    loops = (
        ("review", "design", int(loop_counts.get("revisions") or 0), t("revision")),
        ("testing", "development", int(loop_counts.get("repairs") or 0), t("repair")),
    )
    for source, target, count, label in loops:
        sx, tx = _node_x(STAGE_KEYS.index(source)), _node_x(STAGE_KEYS.index(target))
        active = count > 0
        color, marker = ("#ea580c", "loop-on") if active else ("#cbd5e1", "loop-off")
        dash = "" if active else ' stroke-dasharray="5 5"'
        width = 3.5 if active else 2
        parts.append(
            f'<path d="M{sx - 8},{y - 31} C{sx - 8},{y - 92} {tx + 8},{y - 92} {tx + 8},{y - 33}" fill="none" '
            f'stroke="{color}" stroke-width="{width}"{dash} marker-end="url(#{marker})"/>'
        )
        middle = (sx + tx) / 2
        if active:
            # Once a loop-back happened the label becomes a filled badge, so even a single one is clearly visible.
            text = f"↺ {label} ×{count}"
            parts.append(f'<rect x="{middle - 56}" y="{y - 114}" width="112" height="22" rx="11" fill="#ea580c"/>')
            parts.append(f'<text x="{middle}" y="{y - 99}" text-anchor="middle" font-size="12.5" fill="#ffffff" font-weight="700">{text}</text>')
        else:
            parts.append(f'<text x="{middle}" y="{y - 97}" text-anchor="middle" font-size="12" fill="{color}" font-weight="600">{escape(t("{label} loop", label=label))}</text>')
    for index, stage in enumerate(STAGES):
        x = _node_x(index)
        state = states.get(stage.key, "pending")
        fill, stroke, ink, state_label = PALETTE[state]
        state_label = t(state_label)
        parts.append(f"<g><title>{escape(t(stage.agent))}: {escape(t(stage.description))} — {state_label}</title>")
        if state in {"running", "question"}:
            parts.append(f'<circle class="sdlc-halo" cx="{x}" cy="{y}" r="31" fill="none" stroke="{stroke}"/>')
        if stage.key == selected:
            parts.append(f'<circle cx="{x}" cy="{y}" r="36" fill="none" stroke="#0f172a" stroke-width="2.5"/>')
        dash = ' stroke-dasharray="4 3"' if state in {"paused", "skipped"} else ""
        parts.append(f'<circle cx="{x}" cy="{y}" r="29" fill="{fill}" stroke="{stroke}" stroke-width="2.5"{dash}/>')
        count = (runs or {}).get(stage.key, 0)
        if count > 1:
            # A stage that ran more than once (revision, repair, re-test) carries a ×N badge on the node corner.
            parts.append(f'<circle cx="{x + 24}" cy="{y - 23}" r="12" fill="#ea580c" stroke="#ffffff" stroke-width="2"/>')
            parts.append(f'<text x="{x + 24}" y="{y - 19}" text-anchor="middle" font-size="11" font-weight="700" fill="#ffffff">×{count}</text>')
        glyph = GLYPHS.get(state, str(index + 1))
        parts.append(f'<text x="{x}" y="{y + 7}" text-anchor="middle" font-size="20" font-weight="700" fill="{ink}">{glyph}</text>')
        parts.append(f'<text x="{x}" y="{y + 52}" text-anchor="middle" font-size="14" font-weight="700" fill="#0f172a">{escape(t(stage.label))}</text>')
        parts.append(f'<text x="{x}" y="{y + 69}" text-anchor="middle" font-size="11" fill="#64748b">{escape(t(stage.artifact))}</text>')
        timing = f" · {durations[stage.key]}" if durations and durations.get(stage.key) not in {None, "—"} else ""
        parts.append(f'<text x="{x}" y="{y + 85}" text-anchor="middle" font-size="11" fill="{stroke}" font-weight="600">{state_label}{escape(timing)}</text>')
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)
