"""SDLC aşamalarının durumu ve arayüzde gösterilen yaşam döngüsü diyagramı."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape


@dataclass(frozen=True)
class Stage:
    key: str
    label: str
    agent: str
    artifact: str
    description: str
    phase: str


STAGES = (
    Stage("planning", "Planlama", "Planlama Ajanı", "planning.md", "Kapsam, paydaşlar, kilometre taşları ve riskler", "Gereksinim mühendisliği"),
    Stage("analysis", "Analiz", "Analiz Ajanı", "requirements.md", "FR/NFR, kullanıcı hikâyeleri, kabul kriterleri", "Gereksinim mühendisliği"),
    Stage("design", "Tasarım", "Tasarım Ajanı", "architecture.md", "Bileşen/sıralama diyagramları, API sözleşmesi, veri şeması", "Tasarım ve doğrulama"),
    Stage("review", "İnceleme", "Mimari İnceleme Ajanı", "design_review.md", "Tasarımın gereksinimlere göre bağımsız denetimi", "Tasarım ve doğrulama"),
    Stage("development", "Kodlama", "Geliştirme Ajanı", "kaynak kod", "Seçilen teknolojiyle çalışan kod, Dockerfile, project.yaml", "Gerçekleştirme"),
    Stage("testing", "Test", "Test Ajanı", "test_report.md", "Birim/API/arayüz testleri, izlenebilirlik matrisi", "Gerçekleştirme"),
    Stage("deployment", "Dağıtım", "Dağıtım Ajanı", "deployment.md", "Compose, ortam değişkenleri, sağlık ve geri alma", "Operasyon"),
    Stage("maintenance", "Bakım", "Bakım Ajanı", "maintenance_report.md", "İzleme, olay müdahalesi, iyileştirme listesi", "Operasyon"),
)
STAGE_KEYS = tuple(stage.key for stage in STAGES)
STAGE_NAMES = {stage.key: stage.label for stage in STAGES}
RUNNING_EVENTS = {
    "started", "repair_started", "answered", "gate_failed",
    "revision_started", "changes_requested", "approved", "traceability", "tests_protected",
}
# Onarım bitince sıra teste, tasarım revizyonu bitince sıra incelemeye geçer.
HANDOFFS = {("development", "repair_completed"): "testing", ("design", "revision_completed"): "review"}


def stage_states(events: list[dict], run_status: str) -> dict[str, str]:
    """Her aşama için pending / running / question / done / failed / paused döndürür."""
    states = {key: "pending" for key in STAGE_KEYS}
    for event in events:
        stage, status = event["stage"], event["status"]
        if stage == "workflow" and status == "reset" and event["detail"] in states:
            for key in STAGE_KEYS[STAGE_KEYS.index(event["detail"]):]:
                states[key] = "pending"
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
        elif status == "failed":
            states[stage] = "failed"
    if run_status != "running":
        for key, value in states.items():
            if value in {"running", "question"}:
                states[key] = "paused" if run_status == "paused" else "failed"
    return states


def counters(events: list[dict]) -> dict[str, object]:
    """Son sıfırlamadan bu yana döngü sayaçları ve izlenebilirlik kapsamı."""
    result: dict[str, object] = {"repairs": 0, "test_runs": 0, "review_rounds": 0, "revisions": 0, "questions": 0, "coverage": None}
    for event in events:
        stage, status = event["stage"], event["status"]
        if stage == "workflow" and status == "reset":
            result = {**result, "repairs": 0, "test_runs": 0, "review_rounds": 0, "revisions": 0}
        elif stage == "development" and status == "repair_completed":
            result["repairs"] += 1
        elif stage == "testing" and status in {"passed", "failed"}:
            result["test_runs"] += 1
        elif stage == "review" and status in {"approved", "changes_requested"}:
            result["review_rounds"] += 1
        elif stage == "design" and status == "revision_completed":
            result["revisions"] += 1
        elif status == "question":
            result["questions"] += 1
        elif stage == "testing" and status == "traceability":
            result["coverage"] = event["detail"].rsplit("%", 1)[-1]
    return result


SEGMENT_STARTS = {"started", "repair_started", "revision_started", "answered"}
SEGMENT_ENDS = {"completed", "passed", "failed", "repair_completed", "revision_completed", "question", "changes_requested"}
# Onarım bitince test, tasarım revizyonu bitince inceleme yeni bir "started" olayı olmadan yeniden çalışır.
RERUNS = {("development", "repair_completed"): "testing", ("design", "revision_completed"): "review"}
RUN_STARTS = {"started", "repair_started", "revision_started"}


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def stage_metrics(events: list[dict], now: datetime | None = None) -> dict[str, dict]:
    """Aşama başına çalışma sayısı ve aktif süre (soru bekleme süresi hariç), saniye cinsinden."""
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
        return f"{hours}sa {minutes}dk"
    return f"{minutes}dk {secs:02d}sn" if minutes else f"{secs}sn"


PALETTE = {
    "pending": ("#f1f5f9", "#94a3b8", "#64748b", "Sırada"),
    "running": ("#dbeafe", "#2563eb", "#1e3a8a", "Çalışıyor"),
    "question": ("#fef3c7", "#d97706", "#78350f", "Yanıt bekliyor"),
    "done": ("#dcfce7", "#16a34a", "#14532d", "Tamamlandı"),
    "failed": ("#fee2e2", "#dc2626", "#7f1d1d", "Başarısız"),
    "paused": ("#fff7ed", "#ea580c", "#7c2d12", "Duraklatıldı"),
}
GLYPHS = {"done": "✓", "failed": "✕", "question": "?", "paused": "❚❚"}


def _node_x(index: int) -> int:
    # Dört evre × iki aşama; aynı evredeki düğümler aynı arka plan bandında.
    return 15 + (index // 2) * 245 + (60 if index % 2 == 0 else 170)


def render_svg(states: dict[str, str] | None = None, loop_counts: dict[str, object] | None = None,
               selected: str | None = None, durations: dict[str, str] | None = None) -> str:
    """Yaşam döngüsünü tek bir SVG olarak çizer; durum verilmezse tanıtım görünümü üretir."""
    states = states or {key: "pending" for key in STAGE_KEYS}
    loop_counts = loop_counts or {}
    y = 128
    parts = [
        '<svg viewBox="0 0 1000 262" width="100%" role="img" aria-label="SDLC yaşam döngüsü" '
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
        parts.append(f'<text x="{x + 115}" y="246" text-anchor="middle" font-size="13" font-weight="600" fill="#475569">{escape(phase)}</text>')
    for index in range(len(STAGES) - 1):
        x1, x2 = _node_x(index) + 31, _node_x(index + 1) - 33
        parts.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="#94a3b8" stroke-width="2" marker-end="url(#arrow)"/>')
    loops = (
        ("review", "design", int(loop_counts.get("revisions") or 0), "revizyon"),
        ("testing", "development", int(loop_counts.get("repairs") or 0), "onarım"),
    )
    for source, target, count, label in loops:
        sx, tx = _node_x(STAGE_KEYS.index(source)), _node_x(STAGE_KEYS.index(target))
        active = count > 0
        color, marker = ("#ea580c", "loop-on") if active else ("#cbd5e1", "loop-off")
        dash = "" if active else ' stroke-dasharray="5 5"'
        parts.append(
            f'<path d="M{sx - 8},{y - 31} C{sx - 8},{y - 92} {tx + 8},{y - 92} {tx + 8},{y - 33}" fill="none" '
            f'stroke="{color}" stroke-width="2"{dash} marker-end="url(#{marker})"/>'
        )
        text = f"{label} ×{count}" if active else f"{label} döngüsü"
        parts.append(f'<text x="{(sx + tx) / 2}" y="{y - 97}" text-anchor="middle" font-size="12" fill="{color}" font-weight="600">{text}</text>')
    for index, stage in enumerate(STAGES):
        x = _node_x(index)
        state = states.get(stage.key, "pending")
        fill, stroke, ink, state_label = PALETTE[state]
        parts.append(f"<g><title>{escape(stage.agent)}: {escape(stage.description)} — {state_label}</title>")
        if state in {"running", "question"}:
            parts.append(f'<circle class="sdlc-halo" cx="{x}" cy="{y}" r="31" fill="none" stroke="{stroke}"/>')
        if stage.key == selected:
            parts.append(f'<circle cx="{x}" cy="{y}" r="36" fill="none" stroke="#0f172a" stroke-width="2.5"/>')
        dash = ' stroke-dasharray="4 3"' if state == "paused" else ""
        parts.append(f'<circle cx="{x}" cy="{y}" r="29" fill="{fill}" stroke="{stroke}" stroke-width="2.5"{dash}/>')
        glyph = GLYPHS.get(state, str(index + 1))
        parts.append(f'<text x="{x}" y="{y + 7}" text-anchor="middle" font-size="20" font-weight="700" fill="{ink}">{glyph}</text>')
        parts.append(f'<text x="{x}" y="{y + 52}" text-anchor="middle" font-size="14" font-weight="700" fill="#0f172a">{escape(stage.label)}</text>')
        parts.append(f'<text x="{x}" y="{y + 69}" text-anchor="middle" font-size="11" fill="#64748b">{escape(stage.artifact)}</text>')
        timing = f" · {durations[stage.key]}" if durations and durations.get(stage.key) not in {None, "—"} else ""
        parts.append(f'<text x="{x}" y="{y + 85}" text-anchor="middle" font-size="11" fill="{stroke}" font-weight="600">{state_label}{escape(timing)}</text>')
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)
