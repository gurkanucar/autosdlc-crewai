"""SDLC aşamalarının durumu ve arayüzde gösterilen yaşam döngüsü diyagramı."""

from __future__ import annotations

from dataclasses import dataclass
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
    "started", "repair_started", "repair_completed", "answered", "gate_failed",
    "revision_started", "revision_completed", "changes_requested", "approved", "traceability", "tests_protected",
}


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
        if status in RUNNING_EVENTS:
            states[stage] = "running"
        elif status == "question":
            states[stage] = "question"
        elif status in {"completed", "passed", "edited"}:
            states[stage] = "done"
        elif status == "failed":
            states[stage] = "failed"
    # Tasarım revizyonu biter bitmez aşama yeniden "tamamlandı" sayılır.
    if states["design"] == "running" and states["review"] != "pending":
        states["design"] = "done"
    # Onarım tamamlanınca kodlama, testin sonucunu bekler.
    if states["development"] == "running" and states["testing"] != "pending":
        states["development"] = "done"
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


def render_svg(states: dict[str, str] | None = None, loop_counts: dict[str, object] | None = None) -> str:
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
        dash = ' stroke-dasharray="4 3"' if state == "paused" else ""
        parts.append(f'<circle cx="{x}" cy="{y}" r="29" fill="{fill}" stroke="{stroke}" stroke-width="2.5"{dash}/>')
        glyph = GLYPHS.get(state, str(index + 1))
        parts.append(f'<text x="{x}" y="{y + 7}" text-anchor="middle" font-size="20" font-weight="700" fill="{ink}">{glyph}</text>')
        parts.append(f'<text x="{x}" y="{y + 52}" text-anchor="middle" font-size="14" font-weight="700" fill="#0f172a">{escape(stage.label)}</text>')
        parts.append(f'<text x="{x}" y="{y + 69}" text-anchor="middle" font-size="11" fill="#64748b">{escape(stage.artifact)}</text>')
        parts.append(f'<text x="{x}" y="{y + 85}" text-anchor="middle" font-size="11" fill="{stroke}" font-weight="600">{state_label}</text>')
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)
