"""AutoSDLC: proje oluşturma, canlı yaşam döngüsü, ajan chat'i, belge düzenleme ve proje yönetimi için yerel arayüz."""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from html import escape
from pathlib import Path

import streamlit as st
import yaml
from dotenv import load_dotenv

from autosdlc.crewai_engine import CrewAIEngine
from autosdlc.intake import choice_label, questions, templates, validate_decisions
from autosdlc.lifecycle import (
    STAGE_KEYS, STAGE_NAMES, STAGES, counters, format_duration, render_svg, stage_metrics, stage_states,
)
from autosdlc.models import available_models, default_model
from autosdlc.project_management import delete_project
from autosdlc.runner import (
    CHANGE_KINDS, DOC_BUDGET, STAGE_INPUTS, RunPaused, backup_versions, completed_from_events, run_project,
)
from autosdlc.sections import (
    delete_section, insert_after, replace_section, split_sections, strip_fences, unified_diff,
)
from autosdlc.storage import History
from autosdlc.ui_jobs import WORKER_ERRORS


# AUTOSDLC_WORKSPACE testlerde geçici klasör kullanmak içindir; normalde depo kökü.
ROOT = Path(os.getenv("AUTOSDLC_WORKSPACE") or Path(__file__).resolve().parent).resolve()
load_dotenv(Path(__file__).resolve().parent / ".env")
st.set_page_config(page_title="AutoSDLC", page_icon="🧭", layout="wide")

STATUS_LABELS = {
    "running": ("🔵", "Çalışıyor", "blue"), "paused": ("🟠", "Duraklatıldı", "orange"),
    "completed": ("🟢", "Tamamlandı", "green"), "failed": ("🔴", "Başarısız", "red"),
}
STATE_ICONS = {"pending": "○", "running": "🔵", "question": "❔", "done": "✅", "failed": "❌", "paused": "⏸", "stale": "↻"}
STATE_LABELS = {"pending": "Sırada", "running": "Çalışıyor", "question": "Yanıt bekliyor", "done": "Tamamlandı",
                "failed": "Başarısız", "paused": "Duraklatıldı", "stale": "Güncel değil"}
SECTION_ACTIONS = {"question": "💬 Sor", "rewrite": "✍️ Yeniden yaz", "add": "➕ Ekle", "delete": "🗑️ Sil", "manual": "✏️ Elle"}
CHANGE_STATUS = {"draft": "📝 Taslak", "analyzed": "🔍 Analiz edildi", "in_progress": "🔵 Uygulanıyor",
                 "done": "✅ Tamamlandı", "failed": "❌ Başarısız", "rejected": "✖ Reddedildi"}
START_STAGE = re.compile(r"BAŞLANGIÇ:\s*\**\s*(analysis|design|development)", re.IGNORECASE)
EVENT_NAMES = {
    "started": "Başladı", "completed": "Tamamlandı", "passed": "Testler geçti",
    "failed": "Başarısız", "repair_started": "Onarım başladı", "repair_completed": "Onarım tamamlandı",
    "question": "Kullanıcı kararı bekleniyor", "answered": "Yanıtlandı",
    "gate_failed": "Kalite kapısı: yeniden yazılıyor", "traceability": "İzlenebilirlik matrisi",
    "tests_protected": "Test dosyası değişikliği reddedildi", "approved": "İnceleme: ONAY",
    "changes_requested": "İnceleme: REVİZYON", "revision_started": "Revizyon başladı",
    "revision_completed": "Revizyon tamamlandı", "paused": "Duraklatıldı", "interrupted": "Kesintiye uğradı",
    "reset": "Yeniden üretim istendi", "edited": "Elle düzenlendi", "stale": "Sonraki aşamalar güncel değil",
    "section_question": "Bölüm sorusu", "change_started": "Değişiklik başladı", "change_completed": "Değişiklik tamamlandı",
    "change_failed": "Değişiklik başarısız", "bug_reproduced": "Hata testle yeniden üretildi",
    "bug_not_reproduced": "Hata yeniden üretilemedi", "regression": "Regresyon testi", "tests_not_covered": "Çalıştırılmayan test",
}
# Aşama → (dosya, etiket, düzenlenince yeniden üretime başlanacak aşama). None: salt okunur.
STAGE_DOCUMENTS = {
    "planning": [("planning.md", "Proje planı", "analysis")],
    "analysis": [("requirements.md", "Gereksinimler", "design")],
    "design": [("architecture.md", "Mimari tasarım", "review")],
    "review": [("design_review.md", "Mimari inceleme", "development")],
    "testing": [("test_report.md", "Test raporu", None), ("traceability.md", "İzlenebilirlik matrisi", None)],
    "deployment": [("deployment.md", "Dağıtım kılavuzu", "maintenance")],
    "maintenance": [("maintenance_report.md", "Bakım planı", None)],
}
SKIPPED_DIRS = {".autosdlc-venv", ".venv", "node_modules", ".git", "__pycache__", ".pytest_cache", "test-results", "dist", "build", "target"}
LANGUAGES = {".py": "python", ".js": "javascript", ".ts": "typescript", ".go": "go", ".rs": "rust", ".java": "java",
             ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".html": "html", ".css": "css", ".md": "markdown",
             ".toml": "toml", ".sql": "sql", ".sh": "bash"}
MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
MERMAID_SCRIPT = (
    '<script type="module">import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs";'
    'mermaid.initialize({startOnLoad:true,theme:"neutral",securityLevel:"strict"});</script>'
)
FORM_FIELDS = {"project_type", "stack", "constraints"}

st.html("""<style>
.block-container {padding-top:3.2rem; padding-bottom:6rem; max-width:100%;}
div[data-testid="stMetric"] {background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:4px 12px;}
div[data-testid="stMetricValue"] {font-size:1.15rem;}
div[data-testid="stMetricLabel"] p {font-size:.78rem;}
h1 {font-size:1.7rem !important; padding-bottom:.2rem !important;}
h4 {padding:.1rem 0 !important;}
div[data-testid="stChatMessage"] {padding:.3rem .55rem; gap:.55rem; margin-bottom:.1rem;}
div[data-testid="stChatMessage"] p {margin-bottom:.05rem; font-size:.88rem; line-height:1.35;}
div[data-testid="stChatMessageAvatarUser"], div[data-testid="stChatMessageAvatarAssistant"] {width:1.6rem;height:1.6rem;}
.sdlc-card {border:1px solid #e2e8f0;border-radius:10px;padding:8px 12px;background:#fff;height:100%;font-size:.88rem;}
.sdlc-card b {color:#0f172a;} .sdlc-card small {color:#64748b;}
</style>""")


# ---------------------------------------------------------------- arka plan işleri

def history() -> History:
    db = History(ROOT / "runs" / "history.sqlite3")
    # Arayüz yeniden başlatıldıysa yarım kalan çalışmalar kilitli kalmasın.
    db.recover_stale()
    return db


@st.cache_data(ttl=3600)
def model_options() -> list[str]:
    return available_models()


def wait_for_answer(current_id: str, stage: str, prompt: str) -> str:
    db = History(ROOT / "runs" / "history.sqlite3")
    question_id = db.ask(current_id, stage, prompt)
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        if db.pause_requested(current_id):
            raise RunPaused()
        item = db.question(question_id)
        if item and item["answer"] is not None:
            return item["answer"]
        time.sleep(0.5)
    raise TimeoutError("Kullanıcı yanıtı bir saat içinde gelmedi")


def _spawn(job_id: str, name: str, **kwargs) -> None:
    # job_id, run_project'e giden run_id/resume_run_id ile aynı değerdir ama çakışmasın diye ayrı adlandırılır.
    WORKER_ERRORS.pop(job_id, None)

    def worker() -> None:
        try:
            run_project(workspace=ROOT, on_question=wait_for_answer, **kwargs)
        except Exception as exc:
            WORKER_ERRORS[job_id] = str(exc)

    threading.Thread(target=worker, daemon=True, name=f"autosdlc-{name}-{job_id}").start()


def start_run(idea: str, decisions: dict[str, str], output: Path | None, model: str, api_key: str | None) -> str:
    run_id = uuid.uuid4().hex[:12]
    _spawn(run_id, "run", idea=idea, model=model, api_key=api_key, decisions=decisions, output=output, run_id=run_id)
    return run_id


def resume_run(run_id: str, api_key: str | None) -> None:
    _spawn(run_id, "resume", idea="", resume_run_id=run_id, api_key=api_key)


def regenerate(db: History, run_id: str, stage: str, api_key: str | None) -> None:
    if not db.reset_from(run_id, stage):
        raise ValueError("Çalışan bir proje yeniden üretilemez; önce durdur.")
    resume_run(run_id, api_key)


# ---------------------------------------------------------------- küçük yardımcılar

def api_key_field(prefix: str) -> tuple[bool, str | None]:
    """(anahtar hazır mı, kullanıcı anahtarı). .env anahtarı tarayıcıya hiç gönderilmez."""
    if os.getenv("OPENCODE_API_KEY") and not st.toggle("Farklı API anahtarı kullan", key=f"{prefix}_override"):
        st.caption("🔐 API anahtarı yerel `.env` dosyasından okunacak.")
        return True, None
    key = st.text_input("OpenCode Go API anahtarı", type="password", key=f"{prefix}_key", placeholder="oc_sk_…") or None
    return bool(key), key


def show_diagram(svg: str, height: int = 232) -> None:
    # st.html SVG'yi temizlediği için diyagram yalıtılmış bir iframe içinde çizilir.
    sized = svg.replace('width="100%"', f'width="100%" style="max-height:{height - 6}px"', 1)
    # Yalnızca kendi ürettiğimiz (metinleri kaçışlanmış) SVG verilir; LLM çıktısı ham HTML olarak iframe'e girmez.
    st.iframe(f'<body style="margin:0;background:transparent"><div style="display:flex;justify-content:center">{sized}</div></body>',
              height=height)


def render_markdown(content: str) -> None:
    """Markdown'ı gösterir; ```mermaid blokları gerçek diyagram olarak çizilir (internet gerekir, yoksa metin kalır)."""
    position = 0
    for match in MERMAID_BLOCK.finditer(content):
        if content[position:match.start()].strip():
            st.markdown(content[position:match.start()])
        # Mermaid kodu LLM çıktısıdır: escape() ile yalnızca metin olarak verilir, Mermaid de "strict" güvenlik modunda çizer.
        st.iframe(f'<body style="margin:0">{MERMAID_SCRIPT}<pre class="mermaid">{escape(match.group(1))}</pre></body>',
                  height="content")
        position = match.end()
    if content[position:].strip():
        st.markdown(content[position:])


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def active_stage(states: dict[str, str]) -> str:
    for wanted in ("question", "running", "failed", "paused"):
        found = next((key for key in STAGE_KEYS if states[key] == wanted), None)
        if found:
            return found
    done = [key for key in STAGE_KEYS if states[key] == "done"]
    return done[-1] if done else STAGE_KEYS[0]


def describe_activity(row: dict, states: dict[str, str]) -> str:
    stale = [STAGE_NAMES[key] for key in STAGE_KEYS if states[key] == "stale"]
    if stale and row["status"] != "running":
        return f"Belgeler düzenlendi; {len(stale)} aşama güncel değil. Aşağıdaki uyarıdan güncelleyebilirsin."
    if row["status"] == "completed":
        return "Tüm aşamalar tamamlandı ve testler geçti."
    if row["status"] == "failed":
        return row["error"] or "Çalışma tamamlanamadı; olayları ve test raporunu incele."
    if row["status"] == "paused":
        interrupted = any(event["status"] == "interrupted" for event in row["events"][-3:])
        return "Süreç kesintiye uğradı; kaldığı yerden devam ettirebilirsin." if interrupted else "Çalışma duraklatıldı; tamamlanan aşamalar korunuyor."
    if row["pause_requested"]:
        return "Durdurma istendi; süren ajan çağrısı bitince çalışma duraklayacak."
    waiting = next((key for key in STAGE_KEYS if states[key] == "question"), None)
    if waiting:
        return f"{STAGE_NAMES[waiting]} ajanı senden bir karar bekliyor — aşağıdaki chat kutusuna yanıtını yaz."
    last = row["events"][-1] if row["events"] else None
    if last is None:
        return "Çalışma başlatılıyor…"
    stage = STAGE_NAMES.get(last["stage"], last["stage"])
    return f"{stage} · {EVENT_NAMES.get(last['status'], last['status'])}" + (f" — {last['detail'][:140]}" if last["detail"] else "")


# ---------------------------------------------------------------- chat

def chat_messages(row: dict) -> list[tuple[str, str, str]]:
    """(zaman, rol, metin) listesi: ajan olayları, sorular, kullanıcı yanıtları ve notları kronolojik."""
    agents = {stage.key: stage.agent for stage in STAGES}
    messages = []
    for event in row["events"]:
        stage, status, detail = event["stage"], event["status"], event["detail"]
        agent = agents.get(stage, "İş akışı")
        text = {
            "started": f"▶ **{STAGE_NAMES.get(stage, stage)}** aşamasına başladım.",
            "completed": f"✅ Hazır: `{detail}`" if detail and len(detail) < 80 else "✅ Aşama tamamlandı.",
            "gate_failed": f"⚠️ Kalite kapısından geçemedi: {detail} — yeniden yazıyorum.",
            "changes_requested": f"🔁 Tasarımda düzeltilmesi gereken bulgular var ({detail}); Tasarım Ajanı'na geri gönderiyorum.",
            "approved": f"✅ Tasarımı onayladım ({detail}).",
            "revision_started": "✏️ İnceleme bulgularına göre tasarımı revize ediyorum.",
            "passed": f"✅ Testler geçti ({detail}).",
            "failed": f"❌ Testler başarısız ({detail})." if stage == "testing" else f"❌ {detail}",
            "repair_started": "🛠️ Test hatalarına göre kodu düzeltiyorum.",
            "tests_protected": f"🛡️ Test dosyalarını değiştirme girişimi reddedildi: {detail}",
            "traceability": f"🔗 İzlenebilirlik matrisi çıkarıldı: {detail}.",
            "question": f"❔ **Sorum var:** {detail}",
            "paused": "⏸ Çalışma duraklatıldı.",
            "interrupted": "⚠️ Süreç kesintiye uğradı; kaldığı yerden devam ettirilebilir.",
            "reset": f"🔁 **{STAGE_NAMES.get(detail, detail)}** aşamasından itibaren yeniden üretim istendi.",
            "edited": f"✏️ `{detail}` düzenlendi.",
            "stale": f"↻ **{STAGE_NAMES.get(detail, 'Mimari kararlar')}** değişti; sonraki aşamalar güncel değil.",
            "section_question": f"💬 {detail}",
            "change_started": f"🔁 Değişiklik iterasyonu başladı: **{detail}**",
            "change_completed": f"✅ Değişiklik tamamlandı: **{detail}**",
            "change_failed": f"❌ Değişiklik başarısız: **{detail}**",
            "bug_reproduced": "🐞 Hatayı yeniden üreten regresyon testi yazdım; test şu an başarısız (beklenen).",
            "bug_not_reproduced": "⚠️ Yazdığım regresyon testi hatayı yeniden üretemedi; yine de düzeltme deneniyor.",
            "regression": f"🔁 Regresyon: {detail}.",
            "tests_not_covered": f"⚠️ project.yaml komutlarınca çalıştırılmayan test dosyaları: {detail}",
        }.get(status)
        if text:
            messages.append((event["created_at"], agent, text))
    for item in row["questions"]:
        if item["answer"] and item["answered_at"] and item["answer"] != "Önceki soru yenilendi":
            messages.append((item["answered_at"], "user", item["answer"]))
    for note in row.get("notes", []):
        messages.append((note["created_at"], "user", f"📝 {note['content']}"))
    return sorted(messages, key=lambda item: item[0])


def chat_feed(run_id: str) -> None:
    """Ajan akışı: kronolojik; Streamlit sabit yükseklikli chat kutusunu en alta (en yeni mesaja) kaydırır."""
    db = history()
    row = db.get(run_id)
    if row is None:
        return
    pending = db.pending_question(run_id) if row["status"] == "running" else None
    if pending:
        st.warning(f"**{STAGE_NAMES.get(pending['stage'], pending['stage'])} ajanı soruyor:** {pending['prompt']}  \n"
                   "↓ Yanıtını aşağıdaki kutuya yaz.", icon="❔")
    st.caption("💬 Ajan akışı")
    with st.container(height=300 if pending else 400, border=True):
        messages = chat_messages(row)[-80:]
        if not messages:
            st.caption("Ajanların mesajları burada akacak.")
        for moment, role, text in messages:
            user = role == "user"
            with st.chat_message("user" if user else "assistant", avatar="🧑" if user else "🤖"):
                who = "Sen" if user else escape(role)
                st.markdown(f"<small><b>{who}</b> · {moment[11:19]}</small>  \n{text}", unsafe_allow_html=True)


def chat_placeholder(db: History, row: dict) -> tuple[dict | None, str]:
    pending = db.pending_question(row["id"]) if row["status"] == "running" and not row["pause_requested"] else None
    placeholder = (f"❔ {STAGE_NAMES.get(pending['stage'], pending['stage'])} ajanının sorusunu yanıtla…" if pending
                   else "Ajanlara not yaz (ör. 'arayüz koyu tema olsun'); sonraki aşamalarda dikkate alınır…")
    return pending, placeholder


def handle_chat(db: History, row: dict) -> None:
    """Gönderilen chat mesajını sayfa çizilmeden işler: bekleyen soru varsa yanıt, yoksa yönlendirme notu.

    Chat kutusu sayfa altına sabitlenmesin (sabitlenirse Streamlit sayfayı hep en alta kaydırır) diye
    satır içi çizilir; değeri ise burada, akış bölümleri çizilmeden önce okunur.
    """
    text = st.session_state.get(f"chat_{row['id']}")
    if not text:
        return
    pending, _ = chat_placeholder(db, row)
    try:
        if pending:
            if not db.answer(row["id"], pending["id"], text):
                st.toast("Bu soru zaten yanıtlandı.")
        else:
            db.add_note(row["id"], text)
            st.toast("Not kaydedildi; sonraki aşamaların bağlamına eklenecek." if row["status"] == "running"
                     else "Not kaydedildi; yeniden üretimde dikkate alınacak.")
    except ValueError as exc:
        st.toast(str(exc))


# ---------------------------------------------------------------- aşama detayı

def project_files(project_dir: Path) -> list[Path]:
    files = []
    # Nokta dosyaları sona: ilk açılan dosya .dockerignore değil, gerçek kaynak olsun.
    for path in sorted(project_dir.rglob("*"), key=lambda item: (item.name.startswith("."), str(item))):
        relative = path.relative_to(project_dir)
        if path.is_file() and not SKIPPED_DIRS.intersection(relative.parts):
            files.append(relative)
        if len(files) >= 300:
            break
    return files


def project_zip(project_dir: Path, files: list[Path]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(project_dir / relative, Path(project_dir.name) / relative)
    return buffer.getvalue()


def save_document(db: History, row: dict, filename: str, content: str, stage: str) -> None:
    if filename == "decisions.yaml":
        validate_decisions(yaml.safe_load(content))
    output = Path(row["output_dir"])
    if read_text(output / filename) not in {None, content}:
        backup_versions(output, filename)
    (output / filename).write_text(content, encoding="utf-8")
    docs = Path(row["project_dir"]) / "docs"
    if docs.is_dir():
        (docs / filename).write_text(content, encoding="utf-8")
    db.event(row["id"], stage, "edited", filename)


def pending_edits_path(row: dict) -> Path:
    return Path(row["output_dir"]) / "pending_edits.json"


def accept_edit(db: History, row: dict, stage: str, filename: str, before: str, after: str, summary: str,
                stale_from: str | None = None) -> None:
    """Düzenlemeyi kaydeder, sonraki aşamaları 'güncel değil' işaretler ve delta güncelleme için not alır.

    stale_from: hangi aşamadan sonrası etkilenir; "decisions" verilirse bütün aşamalar (varsayılan: stage).
    """
    save_document(db, row, filename, after, stage)
    path = pending_edits_path(row)
    edits = json.loads(read_text(path) or "[]")
    edits.append({"stage": stage, "file": filename, "summary": summary, "diff": unified_diff(before, after, filename)[:3000]})
    path.write_text(json.dumps(edits, ensure_ascii=False, indent=1), encoding="utf-8")
    db.event(row["id"], "workflow", "stale", stale_from or stage)


def propagate(db: History, row: dict, start: str, delta: bool, api_key: str | None) -> None:
    """Güncel olmayan aşamaları yeniden çalıştırır; delta ise önceki belgeler korunup yalnızca etkilenen kısımlar değişir."""
    path = pending_edits_path(row)
    edits = json.loads(read_text(path) or "[]")
    if delta:
        description = "Kullanıcı şu belge düzenlemelerini yaptı; sonraki aşamaları bunlara göre güncelle:\n" + "\n".join(
            f"- {edit['file']} · {edit['summary']}\n{edit['diff'][:1500]}" for edit in edits
        )
        change_id = db.create_change(row["id"], "edit", f"Belge düzenlemeleri ({len(edits) or 1})", description[:3990], status="in_progress")
        db.update_change(row["id"], change_id, start_stage=start)
    regenerate(db, row["id"], start, api_key)
    path.unlink(missing_ok=True)


def stage_context(row: dict, stage: str) -> str:
    """Bölüm işlemleri için ajan bağlamı: kararlar ve aşamanın girdisi olan önceki belgeler."""
    output = Path(row["output_dir"])
    parts = [f"Mimari kararlar:\n{read_text(output / 'decisions.yaml') or ''}"]
    names = {"planning": "planning.md", "analysis": "requirements.md", "design": "architecture.md", "review": "design_review.md",
             "deployment": "deployment.md", "maintenance": "maintenance_report.md"}
    for name in STAGE_INPUTS.get(stage, ()):
        text = read_text(output / names.get(name, "")) if name in names else None
        if text:
            parts.append(f"--- {name} belgesi ---\n{text[:DOC_BUDGET]}")
    return "\n\n".join(parts)


def make_engine(row: dict, api_key: str | None) -> CrewAIEngine:
    return CrewAIEngine(api_key=api_key, model=row["model"] or None)


def open_focus(row: dict, path: Path, title: str) -> None:
    st.session_state["focus"] = {"run": row["id"], "path": str(path), "title": title}
    st.rerun()


def focus_view(focus: dict) -> None:
    """Belgeyi ya da README'yi tam genişlikte, kutu yüksekliği sınırı olmadan gösterir."""
    back, title = st.columns([1, 8], vertical_alignment="center")
    if back.button("← Geri", width="stretch"):
        st.session_state.pop("focus")
        st.rerun()
    title.markdown(f"#### {escape(focus['title'])}")
    st.caption(f"`{focus['path']}`")
    content = read_text(Path(focus["path"]))
    if content is None:
        st.warning("Dosya bulunamadı.")
    else:
        render_markdown(content)


def section_panel(db: History, row: dict, stage: str, filename: str, content: str, can_edit: bool) -> None:
    """Belgenin bir bölümü (başlık ya da FR/NFR) üzerinde soru sorma ve öneri-diff-onay ile düzenleme."""
    sections = split_sections(content)
    if not sections:
        st.caption("Belgede başlık ya da gereksinim bulunamadı.")
        return
    prefix = f"{row['id']}_{filename}"
    labels = {item.key: "\u2003" * (item.level - 1) + ("📌 " if item.kind == "requirement" else "§ ") + item.title for item in sections}
    key = st.selectbox("Bölüm", [item.key for item in sections], format_func=labels.get, key=f"section_{prefix}")
    section = next(item for item in sections if item.key == key)
    lines = content.split("\n")
    with st.container(height=130, border=True):
        st.markdown(section.text(lines))
    actions = ["question"] + (["rewrite", "add", "delete", "manual"] if can_edit else [])
    action = st.segmented_control("İşlem", actions, format_func=SECTION_ACTIONS.get, default="question",
                                  key=f"action_{prefix}", label_visibility="collapsed") or "question"
    proposal_key, answer_key = f"proposal_{prefix}", f"answer_{prefix}"
    if action == "manual":
        new_text = st.text_area("Bölümün yeni hâli", section.text(lines), height=160, key=f"manual_{prefix}_{key}")
        if st.button("Değişikliği önizle", key=f"preview_manual_{prefix}"):
            st.session_state[proposal_key] = {"before": content, "after": replace_section(content, section, new_text),
                                              "summary": f"Elle düzenlendi · {section.title}"}
    elif action == "delete":
        if st.button("Silmeyi önizle", key=f"preview_delete_{prefix}"):
            st.session_state[proposal_key] = {"before": content, "after": delete_section(content, section),
                                              "summary": f"Silindi · {section.title}"}
    else:
        placeholders = {"question": "Ör: Bu gereksinim neden gerekli? Kabul kriteri yeterli mi?",
                        "rewrite": "Ör: Kabul kriterlerini Given/When/Then biçiminde yeniden yaz.",
                        "add": "Ör: Bu bölümün altına ürün barkoduyla arama gereksinimi ekle."}
        instruction = st.text_area("Soru" if action == "question" else "Talimat", key=f"instruction_{prefix}_{action}",
                                   height=70, placeholder=placeholders[action])
        ready, api_key = api_key_field(f"section_{prefix}")
        label = "Sor" if action == "question" else "Öneri al"
        if st.button(label, key=f"ask_{prefix}", type="primary", disabled=not (ready and instruction.strip())):
            agent = next(item.agent for item in STAGES if item.key == stage)
            try:
                with st.spinner(f"{agent} çalışıyor…"):
                    text = make_engine(row, api_key).section(stage, row["idea"], stage_context(row, stage), content,
                                                             section.text(lines), instruction, action)
            except (RuntimeError, ValueError) as exc:
                st.error(str(exc))
                return
            if action == "question":
                st.session_state[answer_key] = text
                db.event(row["id"], stage, "section_question", f"**{section.title}** — {instruction.strip()}\n\n{text[:2500]}")
            else:
                new_text = strip_fences(text)
                after = replace_section(content, section, new_text) if action == "rewrite" else insert_after(content, section, new_text)
                st.session_state[proposal_key] = {"before": content, "after": after,
                                                  "summary": f"{SECTION_ACTIONS[action][2:]} · {section.title}: {instruction.strip()[:200]}"}
    if action == "question" and st.session_state.get(answer_key):
        with st.container(height=200, border=True):
            st.markdown(st.session_state[answer_key])
    proposal = st.session_state.get(proposal_key)
    if proposal and action != "question":
        if proposal["before"] != content:
            st.session_state.pop(proposal_key)  # Belge bu arada değişti; eski öneri geçersiz.
            return
        diff = unified_diff(proposal["before"], proposal["after"], filename)
        st.caption("Önerilen değişiklik — kabul edersen kaydedilir ve sonraki aşamalar 'güncel değil' olarak işaretlenir.")
        with st.container(height=220, border=True):
            st.code(diff or "(değişiklik yok)", language="diff")
        accept, reject = st.columns(2)
        if accept.button("✅ Kabul et", key=f"accept_{prefix}", type="primary", width="stretch", disabled=not diff):
            accept_edit(db, row, stage, filename, proposal["before"], proposal["after"], proposal["summary"])
            st.session_state.pop(proposal_key)
            st.toast("Değişiklik kaydedildi; sonraki aşamalar 'güncel değil' olarak işaretlendi.")
            st.rerun()
        if reject.button("✖ Reddet", key=f"reject_{prefix}", width="stretch"):
            st.session_state.pop(proposal_key)
            st.rerun()


def live_output(row: dict, stage: str) -> None:
    text = read_text(Path(row["output_dir"]) / "live" / f"{stage}.md")
    st.caption("🟢 Ajanın çıktısı canlı akıyor (son kısım gösteriliyor)")
    with st.container(height=360, border=True):
        if not text:
            st.caption("Ajan düşünüyor… ilk sözcükler birazdan burada görünecek.")
        elif stage in {"development", "testing"}:
            st.code(text[-5000:], language="json", wrap_lines=True)
        else:
            st.markdown(text[-6000:])


def document_view(db: History, row: dict, stage: str) -> None:
    output = Path(row["output_dir"])
    available = [item for item in STAGE_DOCUMENTS.get(stage, []) if (output / item[0]).is_file()]
    if not available:
        st.caption("Bu aşamanın belgesi henüz üretilmedi.")
        return
    if len(available) > 1:
        choice = st.segmented_control("Belge", [name for name, _, _ in available], key=f"doc_{row['id']}_{stage}",
                                      format_func=lambda name: next(label for n, label, _ in available if n == name),
                                      default=available[0][0], label_visibility="collapsed") or available[0][0]
    else:
        choice = available[0][0]
    filename, label, regen_from = next(item for item in available if item[0] == choice)
    content = read_text(output / filename) or ""
    can_edit = regen_from is not None and row["status"] != "running"
    modes = {"read": "📖 Oku", "section": "🎯 Bölüm", **({"full": "✏️ Tamamı"} if can_edit else {})}
    info, mode_col, full = st.columns([2, 2.6, 0.5], vertical_alignment="center")
    info.caption(f"📄 `{filename}`" + ("" if can_edit else " · salt okunur" if regen_from is None else " · çalışma sürerken düzenlenemez"))
    mode = mode_col.segmented_control("Mod", list(modes), format_func=modes.get, default="read",
                                      key=f"mode_{row['id']}_{filename}", label_visibility="collapsed") or "read"
    if full.button("⛶", key=f"focus_{row['id']}_{filename}", help="Tam ekran"):
        open_focus(row, output / filename, f"{label} · {row['idea'][:60]}")
    if mode == "section":
        section_panel(db, row, stage, filename, content, can_edit)
    elif mode == "full":
        edited = st.text_area(label, content, height=320, key=f"editor_{row['id']}_{filename}", label_visibility="collapsed")
        if st.button("💾 Kaydet", key=f"save_{row['id']}_{filename}", disabled=edited == content):
            accept_edit(db, row, stage, filename, content, edited, "Belgenin tamamı elle düzenlendi")
            st.toast("Kaydedildi; sonraki aşamalar 'güncel değil' olarak işaretlendi.")
            st.rerun()
    else:
        with st.container(height=360, border=True):
            render_markdown(content)


def code_view(row: dict) -> None:
    project_dir = Path(row["project_dir"])
    files = project_files(project_dir) if project_dir.is_dir() else []
    if not files:
        st.caption("Henüz kaynak dosya yok.")
        return
    pick, download = st.columns([3, 1], vertical_alignment="bottom")
    selected = pick.selectbox(f"Dosyalar ({len(files)})", files, format_func=lambda path: path.as_posix(), key=f"file_{row['id']}")
    download.download_button("⬇️ ZIP", project_zip(project_dir, files), file_name=f"{project_dir.name}.zip",
                             mime="application/zip", width="stretch")
    text = read_text(project_dir / selected)
    with st.container(height=330, border=True):
        if text is None:
            st.caption("Metin olmayan dosya.")
        else:
            language = LANGUAGES.get(selected.suffix, "docker" if selected.name == "Dockerfile" else None)
            st.code(text[:60_000], language=language, line_numbers=True)


def stage_detail(db: History, row: dict, stage_key: str, states: dict[str, str], metrics: dict[str, dict]) -> None:
    index = STAGE_KEYS.index(stage_key)
    stage = STAGES[index]
    item = metrics[stage_key]
    state = states[stage_key]
    head, m1, m2 = st.columns([3, 1, 1], vertical_alignment="center")
    head.markdown(f"#### {index + 1}. {stage.label} <small style='color:#64748b;font-weight:400'>· {stage.agent}</small>", unsafe_allow_html=True)
    m1.metric("Süre", format_duration(item["seconds"]), help="Ajanın aktif çalıştığı süre; senin yanıtını beklediği süre dahil değil.")
    m2.metric("Çalışma", f"{item['runs']}×", help="Aşamanın kaç kez çalıştığı (onarım ve revizyon turları dahil).")
    st.caption(f"{STATE_ICONS[state]} {STATE_LABELS[state]} · {stage.description}")
    if state in {"running", "question"} and row["status"] == "running":
        live_output(row, stage_key)
    elif stage_key == "development":
        code_view(row)
    else:
        document_view(db, row, stage_key)
    events = [event for event in row["events"] if event["stage"] == stage_key]
    if events:
        with st.expander(f"Bu aşamanın olayları ({len(events)})"):
            st.dataframe([{"Zaman": event["created_at"][11:19], "Olay": EVENT_NAMES.get(event["status"], event["status"]),
                           "Ayrıntı": event["detail"]} for event in reversed(events)], hide_index=True, width="stretch")


# ---------------------------------------------------------------- çalışma alanı

def selected_stage(run_id: str, states: dict[str, str]) -> tuple[str, bool]:
    picked = st.session_state.get(f"stage_{run_id}")
    return picked or active_stage(states), picked is not None


def overview(run_id: str) -> None:
    """Diyagram, tıklanabilir aşama seçici ve metrikler. Çalışırken kendi kendine yenilenir."""
    db = history()
    row = db.get(run_id)
    if row is None:
        st.info("Çalışma başlatılıyor…")
        return
    states = stage_states(row["events"], row["status"])
    loops = counters(row["events"])
    metrics = stage_metrics(row["events"])
    selected, picked = selected_stage(run_id, states)
    # Çalışan aşamanın süresi diyagrama yazılmaz; böylece diyagram yalnızca durum değişince yeniden çizilir.
    durations = {key: format_duration(value["seconds"]) for key, value in metrics.items() if not value["running"]}
    runs = {key: value["runs"] for key, value in metrics.items()}
    show_diagram(render_svg(states, loops, selected, durations, runs))
    choice = st.segmented_control(
        "Aşama", STAGE_KEYS, key=f"stage_{run_id}", label_visibility="collapsed", width="stretch",
        format_func=lambda key: f"{STATE_ICONS[states[key]]} {STAGE_NAMES[key]}",
    )
    done = sum(value == "done" for value in states.values())
    cells = st.columns([1, 1.3, 1, 1, 1, 1.2, 4])
    cells[0].metric("Aşama", f"{done}/{len(STAGES)}")
    cells[1].metric("Ajan süresi", format_duration(sum(value["seconds"] for value in metrics.values())))
    cells[2].metric("İnceleme", f"{loops['review_rounds']} tur")
    cells[3].metric("Test", f"{loops['test_runs']}×")
    cells[4].metric("Onarım", loops["repairs"])
    cells[5].metric("FR kapsamı", f"%{loops['coverage']}" if loops["coverage"] is not None else "—")
    with cells[6]:
        activity = describe_activity(row, states)
        {"completed": st.success, "failed": st.error, "paused": st.warning}.get(row["status"], st.info)(
            activity + ("" if picked else "  \n_Bir aşamaya tıkla; seçmezsen görünüm aktif aşamayı izler._"))
    stale = [key for key in STAGE_KEYS if states[key] == "stale"]
    if stale and row["status"] != "running":
        stale_banner(db, row, stale)
    # Aşama tıklanınca, durum ya da bekleyen soru değişince detay, chat ve üst bar da güncellensin.
    pending = db.pending_question(run_id) if row["status"] == "running" else None
    signature = (row["status"], row["pause_requested"], pending and pending["id"], run_id in WORKER_ERRORS, choice)
    key = f"signature_{run_id}"
    previous = st.session_state.get(key)
    st.session_state[key] = signature
    if previous is not None and previous != signature:
        st.rerun()


def stale_banner(db: History, row: dict, stale: list[str]) -> None:
    text, action = st.columns([4, 1.4], vertical_alignment="center")
    text.warning(f"↻ **Güncel değil:** {', '.join(STAGE_NAMES[key] for key in stale)}. Önceki bir belge değişti; "
                 "bu aşamaları güncelle ki kod ve testler belgelerle tutarlı olsun.")
    with action.popover("Etkilenenleri güncelle", width="stretch", type="primary"):
        mode = st.radio("Nasıl?", ["delta", "full"], key=f"propagate_mode_{row['id']}", format_func={
            "delta": "Delta — mevcut belgeleri ve kodu koru, yalnızca etkilenen kısımları güncelle",
            "full": "Baştan üret — bu aşamaları sıfırdan yeniden yaz",
        }.get)
        ready, key = api_key_field(f"propagate_{row['id']}")
        if st.button("Başlat", key=f"propagate_{row['id']}", type="primary", disabled=not ready):
            try:
                propagate(db, row, stale[0], mode == "delta", key)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))


def detail_panel(run_id: str) -> None:
    db = history()
    row = db.get(run_id)
    if row is None:
        return
    states = stage_states(row["events"], row["status"])
    stage_detail(db, row, selected_stage(run_id, states)[0], states, stage_metrics(row["events"]))


def impact_context(row: dict) -> str:
    output = Path(row["output_dir"])
    parts = [f"Mimari kararlar:\n{read_text(output / 'decisions.yaml') or ''}"]
    for name, budget in (("requirements.md", 9000), ("architecture.md", 7000), ("traceability.md", 3000)):
        text = read_text(output / name)
        if text:
            parts.append(f"--- {name} ---\n{text[:budget]}")
    project_dir = Path(row["project_dir"])
    if project_dir.is_dir():
        parts.append("Proje dosyaları:\n" + "\n".join(path.as_posix() for path in project_files(project_dir)))
    return "\n\n".join(parts)


@st.dialog("📝 Değişiklik talepleri", width="large")
def changes_dialog(run_id: str) -> None:
    """Yeni özellik / hata / değişiklik: oluştur → etki analizi → onayla ve uygula (delta iterasyonu)."""
    db = history()
    row = db.get(run_id)
    with st.form(f"new_change_{run_id}", clear_on_submit=True):
        st.markdown("**➕ Yeni talep**")
        kind = st.radio("Tür", ["feature", "bug", "change"], format_func=CHANGE_KINDS.get, horizontal=True)
        title = st.text_input("Başlık", placeholder="Ör: Ürünlere barkod alanı eklensin / Stok eksiye düşebiliyor")
        description = st.text_area("Açıklama", height=90, placeholder=(
            "Ne isteniyor ve neden? Hata ise: adımlar, beklenen ve gerçekleşen davranış."))
        if st.form_submit_button("Talep oluştur"):
            try:
                st.toast(f"{db.create_change(run_id, kind, title, description)} oluşturuldu.")
            except ValueError as exc:
                st.error(str(exc))
    changes = [item for item in db.changes(run_id) if item["kind"] != "edit"]
    if not changes:
        st.caption("Henüz talep yok. Talep oluşturunca Etki Analizi Ajanı hangi gereksinim, tasarım, dosya ve testlerin "
                   "etkileneceğini çıkarır; onaylarsan yalnızca etkilenen aşamalar delta modunda güncellenir.")
        return
    busy = row["status"] == "running" or db.active_change(run_id) is not None
    ready, api_key = api_key_field(f"changes_{run_id}")
    for change in reversed(changes):
        open_now = change["status"] in {"draft", "analyzed"}
        with st.expander(f"{CHANGE_STATUS.get(change['status'], change['status'])} · **{change['id']}** · "
                         f"{CHANGE_KINDS[change['kind']]} · {change['title']}", expanded=open_now):
            st.write(change["description"] or "_Açıklama yok_")
            if change["impact"]:
                with st.container(height=240, border=True):
                    st.markdown(change["impact"])
            if change["status"] == "draft":
                if st.button("🔍 Etki analizi yap", key=f"impact_{change['id']}", disabled=not ready):
                    try:
                        with st.spinner("Etki Analizi Ajanı çalışıyor…"):
                            report = make_engine(row, api_key).impact(row["idea"], impact_context(row), (
                                f"{change['id']} · {CHANGE_KINDS[change['kind']]}: {change['title']}\n{change['description']}"))
                        found = START_STAGE.search(report)
                        start = found.group(1).lower() if found else ("development" if change["kind"] == "bug" else "analysis")
                        db.update_change(run_id, change["id"], impact=report, start_stage=start, status="analyzed")
                        st.rerun(scope="fragment")
                    except (RuntimeError, ValueError) as exc:
                        st.error(str(exc))
            elif change["status"] == "analyzed":
                options = ["analysis", "design", "development"]
                start = st.selectbox("Başlangıç aşaması", options, index=options.index(change["start_stage"] or "analysis"),
                                     format_func=STAGE_NAMES.get, key=f"start_{change['id']}",
                                     help="Bu aşama ve sonrası delta modunda güncellenir; öncekiler olduğu gibi kalır.")
                apply, reject = st.columns(2)
                if apply.button("▶ Onayla ve uygula", key=f"apply_{change['id']}", type="primary",
                                disabled=busy or not ready, help="Çalışan bir iş ya da uygulanan başka bir talep varken başlatılamaz."):
                    db.update_change(run_id, change["id"], status="in_progress", start_stage=start)
                    regenerate(db, run_id, start, api_key)
                    st.rerun()
                if reject.button("✖ Reddet", key=f"reject_{change['id']}"):
                    db.update_change(run_id, change["id"], status="rejected")
                    st.rerun(scope="fragment")


def header(db: History, row: dict) -> None:
    run_id = row["id"]
    title, status, actions = st.columns([3.4, 1.3, 5.3], vertical_alignment="center")
    with title:
        st.markdown(f"#### {escape(row['idea'][:140])}")
        # Tam yol başlığı birkaç satıra yayıyor; yalnızca klasör adı (tam yol README görünümünde ve Yönet menüsünde).
        st.caption(f"`{run_id}` · model `{row['model'] or '—'}` · 📁 `{Path(row['project_dir']).name}`")
    with status:
        icon, label, color = STATUS_LABELS.get(row["status"], ("⚪", row["status"], "gray"))
        st.badge(label, icon=icon, color=color)
    with actions:
        primary, changes, readme, manage = st.columns([1.3, 1.45, 1.25, 1.1])
        if changes.button("📝 Değişiklik", key=f"changes_{run_id}", width="stretch", help="Yeni özellik, hata ya da değişiklik talebi"):
            changes_dialog(run_id)
        readme_path = Path(row["project_dir"]) / "README.md"
        if readme.button("📘 README", key=f"readme_{run_id}", width="stretch", disabled=not readme_path.is_file()):
            open_focus(row, readme_path, f"README · {row['idea'][:60]}")
        if row["status"] == "running":
            if primary.button("⏸ Durdur", key=f"pause_{run_id}", width="stretch", disabled=bool(row["pause_requested"])):
                db.request_pause(run_id)
                st.rerun()
        elif row["status"] == "paused":
            with primary.popover("▶ Devam et", width="stretch"):
                ready, key = api_key_field(f"resume_{run_id}")
                if st.button("Kaldığı yerden devam et", key=f"resume_{run_id}", type="primary", disabled=not ready):
                    resume_run(run_id, key)
                    st.rerun()
        elif row["status"] == "failed":
            # Başarısız iş: tamamlanmamış ilk aşamadan sıfırlayıp sürdür; biten aşamalar yeniden üretilmez.
            failed_at = next((stage for stage in STAGE_KEYS if stage not in completed_from_events(row["events"])), STAGE_KEYS[-1])
            with primary.popover("↻ Tekrar dene", width="stretch"):
                st.caption(f"{STAGE_NAMES[failed_at]} aşamasından devam edilir; tamamlanan aşamalar korunur.")
                ready, key = api_key_field(f"retry_{run_id}")
                if st.button("Tekrar dene", key=f"retry_{run_id}", type="primary", disabled=not ready):
                    regenerate(db, run_id, failed_at, key)
                    st.rerun()
        with manage.popover("⚙️ Yönet", width="stretch"):
            manage_menu(db, row)
    active = db.active_change(run_id)
    if active:
        st.info(f"🔁 **{active['id']}** uygulanıyor ({CHANGE_KINDS[active['kind']]}): {active['title']}")
    if run_id in WORKER_ERRORS:
        st.error(WORKER_ERRORS[run_id])


def manage_menu(db: History, row: dict) -> None:
    run_id = row["id"]
    if row["status"] == "running":
        st.info("Düzenleme, yeniden üretme ve silme için önce çalışmayı durdur.")
        return
    edit, regen, remove = st.tabs(["✏️ Fikir", "🔁 Yeniden üret", "🗑️ Sil"])
    decisions_path = Path(row["output_dir"]) / "decisions.yaml"
    decisions = yaml.safe_load(decisions_path.read_text(encoding="utf-8")) if decisions_path.is_file() else {}
    with edit:
        idea = st.text_area("Proje fikri", row["idea"], key=f"idea_{run_id}", height=90)
        constraints = st.text_area("Kısıtlar", decisions.get("constraints", ""), key=f"constraints_{run_id}", height=70)
        if st.button("Kaydet", key=f"save_idea_{run_id}"):
            try:
                if not idea.strip() or len(idea) > 1000:
                    raise ValueError("Proje fikri 1–1000 karakter olmalı")
                updated = validate_decisions({**decisions, "constraints": constraints})
                db.update_idea(run_id, idea.strip())
                previous = yaml.safe_dump(decisions, allow_unicode=True, sort_keys=False)
                accept_edit(db, row, "workflow", "decisions.yaml", previous,
                            yaml.safe_dump(updated, allow_unicode=True, sort_keys=False), "Fikir/kısıtlar düzenlendi",
                            stale_from="decisions")
                st.toast("Kaydedildi. Bütün aşamalar 'güncel değil' işaretlendi; üstteki uyarıdan güncelleyebilirsin.")
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(str(exc))
    with regen:
        stage = st.selectbox("Başlangıç aşaması", STAGE_KEYS, format_func=lambda key: STAGE_NAMES[key], key=f"regen_stage_{run_id}")
        st.caption("Seçilen aşama ve sonrası yeniden çalışır; önceki belgeler girdi olarak kullanılır.")
        ready, key = api_key_field(f"regen_{run_id}")
        if st.button("Yeniden üret", key=f"regen_{run_id}", type="primary", disabled=not ready):
            try:
                regenerate(db, run_id, stage, key)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with remove:
        st.caption(f"Proje: `{row['project_dir']}`  \nRaporlar: `{row['output_dir']}`")
        confirmed = st.checkbox("Kalıcı olarak silmeyi onaylıyorum", key=f"delete_confirm_{run_id}")
        if st.button("Projeyi sil", key=f"delete_{run_id}", disabled=not confirmed):
            try:
                delete_project(ROOT, run_id)
                st.session_state.pop("selected_run", None)
                st.toast("Proje silindi.")
                st.rerun()
            except (OSError, ValueError) as exc:
                st.error(f"Silinemedi: {exc}")


def wait_for_start(run_id: str) -> None:
    st.info("Çalışma başlatılıyor…")
    if history().get(run_id) or run_id in WORKER_ERRORS:
        st.rerun()


def projects_page() -> None:
    db = history()
    items = db.list(100)
    ids = [item["id"] for item in items]
    starting = st.session_state.get("starting_run")
    if starting in ids:
        st.session_state.pop("starting_run")
        st.session_state["selected_run"] = starting
    with st.sidebar:
        st.markdown("**Projeler**")
        if items:
            if st.session_state.get("selected_run") not in ids:
                st.session_state["selected_run"] = ids[0]
            labels = {item["id"]: f"{STATUS_LABELS.get(item['status'], ('⚪',))[0]} {item['idea'][:42]}" for item in items}
            st.radio("Proje", ids, key="selected_run", format_func=lambda run_id: labels[run_id], label_visibility="collapsed")
    if starting and starting not in ids:
        # İş parçacığı kaydı oluşturana kadar kısa bir bekleme ekranı.
        if starting in WORKER_ERRORS:
            st.error(WORKER_ERRORS.pop(starting))
            st.session_state.pop("starting_run")
            return
        st.fragment(wait_for_start, run_every=1)(starting)
        return
    if not items:
        st.info("Henüz oluşturulmuş proje yok. **Yeni proje** sayfasından başlayabilirsin.")
        return
    row = db.get(st.session_state["selected_run"])
    header(db, row)
    focus = st.session_state.get("focus")
    if focus and focus["run"] == row["id"]:
        focus_view(focus)
        return
    handle_chat(db, row)
    every = 1.5 if row["status"] == "running" else None
    st.fragment(overview, run_every=every)(row["id"])
    left, right = st.columns([3, 2], gap="medium")
    with left:
        st.fragment(detail_panel, run_every=every)(row["id"])
    with right:
        st.fragment(chat_feed, run_every=every)(row["id"])
        _, placeholder = chat_placeholder(db, row)
        # Satır içi (container içinde) chat kutusu sayfanın en alta kaymasını engeller.
        with st.container():
            st.chat_input(placeholder, key=f"chat_{row['id']}")


# ---------------------------------------------------------------- yeni proje

def apply_template(template_id: str | None) -> None:
    """Seçilen şablonun değerlerini form alanlarına yazar (düğme geri çağrısı: widget'lardan önce çalışır)."""
    template = next((item for item in templates() if item["id"] == template_id), None)
    if template is None:
        return
    chosen = template["decisions"]
    st.session_state.update({
        "idea_input": template["idea"],
        "project_type_input": chosen["project_type"],
        "stack_input": chosen["stack"],
        "constraints_input": chosen.get("constraints", ""),
    })
    for name, item in questions().items():
        if name not in FORM_FIELDS:
            st.session_state[f"choice_{name}"] = chosen.get(name, item["options"][0])


def template_picker() -> None:
    catalog = {item["id"]: item for item in templates()}
    pick, apply = st.columns([4, 1], vertical_alignment="bottom")
    selected = pick.selectbox(
        "📋 Örnek proje şablonu", [None, *catalog], key="template_choice",
        format_func=lambda key: "— Şablon seçerek formu doldur —" if key is None else catalog[key]["title"],
    )
    apply.button("Uygula", key="apply_template", disabled=selected is None, width="stretch",
                 on_click=apply_template, args=(selected,))


@st.dialog("Proje klasörünü seç")
def choose_folder() -> None:
    current = Path(st.session_state.get("browse_dir", ROOT.parent)).expanduser().resolve()
    if not current.is_dir():
        current = ROOT.parent
    st.session_state["browse_dir"] = str(current)
    st.caption(f"Üst klasör: `{current}`")
    if st.button("⬆️ Üst klasöre git", disabled=current == current.parent):
        st.session_state["browse_dir"] = str(current.parent)
        st.rerun()
    if os.name == "nt" and current == current.parent:
        drives = [f"{letter}:/" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:/").is_dir()]
        drive = st.selectbox("Sürücü", drives)
        if st.button("Sürücüye git"):
            st.session_state["browse_dir"] = drive
            st.rerun()
    try:
        folders = sorted((path for path in current.iterdir() if path.is_dir() and not path.name.startswith(".")), key=lambda path: path.name.casefold())
    except OSError as exc:
        st.error(f"Klasör okunamadı: {exc}")
        folders = []
    if folders:
        selected = st.selectbox("Alt klasörler", folders, format_func=lambda path: path.name)
        if st.button("📂 Seçili klasöre gir"):
            st.session_state["browse_dir"] = str(selected)
            st.rerun()
    else:
        st.caption("Bu konumda görüntülenebilir alt klasör yok.")
    name = st.text_input("Oluşturulacak yeni klasörün adı", key="new_folder_name", placeholder="stok-uygulamasi")
    if st.button("Bu konumu kullan", type="primary"):
        if not name.strip() or Path(name).name != name or name in {".", ".."} or "/" in name or "\\" in name:
            st.error("Tek bir yeni klasör adı yaz.")
        elif (current / name).exists():
            st.error("Bu klasör zaten mevcut; yeni bir ad yaz.")
        else:
            st.session_state["output_path"] = str(current / name)
            st.rerun()


def home_page() -> None:
    st.title("🧭 AutoSDLC — Yeni proje")
    st.caption("Bir fikri sekiz ajanla yazılım yaşam döngüsünün tüm aşamalarından geçir. Başlatınca **Projeler** sayfasında canlı izlersin.")
    left, right = st.columns([2.2, 1], gap="large")
    with left:
        template_picker()
        idea = st.text_area("Ne geliştirmek istiyorsun?", key="idea_input", height=100,
                            placeholder="Örn: Küçük işletme için stok takip uygulaması; ürün ekleme, stok hareketi ve kritik stok uyarısı")
        catalog = questions()
        type_col, stack_col = st.columns([1, 2])
        project_type = type_col.selectbox(
            catalog["project_type"]["question"], catalog["project_type"]["options"],
            key="project_type_input", format_func=lambda value: choice_label("project_type", value),
        )
        stack = stack_col.text_input(catalog["stack"]["question"], key="stack_input", placeholder="Örn: Node.js/Express, pnpm, sade HTML/CSS/JS")
        decisions = {"project_type": project_type, "stack": stack}
        options = [(name, item) for name, item in catalog.items()
                   if name not in FORM_FIELDS and (not item.get("when") or project_type in item["when"])]
        if options:
            for column, (name, item) in zip(st.columns(len(options)), options):
                decisions[name] = column.selectbox(
                    item["question"], item["options"], key=f"choice_{name}",
                    format_func=lambda value, key=name: choice_label(key, value),
                )
        decisions["constraints"] = st.text_area(catalog["constraints"]["question"], key="constraints_input", height=80,
                                                placeholder="Örn: pnpm kullan, frontend sade HTML olsun, arayüz metinleri Türkçe olsun.")
    with right:
        models = model_options()
        model = st.selectbox("OpenCode Go modeli", models, index=models.index(default_model(models)))
        ready, api_key = api_key_field("new")
        path_col, browse_col = st.columns([3, 1], vertical_alignment="bottom")
        output_path = path_col.text_input("Proje klasörü (isteğe bağlı)", key="output_path", placeholder="projects/<ad>-<kimlik>")
        if browse_col.button("Gözat", width="stretch"):
            choose_folder()
        if st.button("🚀 Projeyi oluştur", type="primary", width="stretch"):
            try:
                validated = validate_decisions(decisions)
                if not idea.strip() or len(idea) > 1000:
                    raise ValueError("Proje fikrini 1–1000 karakter arasında yaz.")
                if not ready:
                    raise ValueError("OpenCode Go API anahtarı gerekli.")
                target = Path(output_path).expanduser().resolve() if output_path.strip() else None
                if target and target.exists():
                    raise ValueError(f"Proje klasörü zaten mevcut: {target}")
                st.session_state["starting_run"] = start_run(idea.strip(), validated, target, model, api_key)
                st.switch_page(PROJECTS_PAGE)
            except (ValueError, OSError) as exc:
                st.error(str(exc))
        st.caption("Bir ajan karar isterse soru Projeler sayfasındaki chat'te görünür; yanıtını oradan yazarsın.")
    st.markdown("##### Yaşam döngüsü nasıl işler?")
    show_diagram(render_svg())
    with st.expander("Aşamalar ve ajanlar"):
        columns = st.columns(4)
        for index, stage in enumerate(STAGES):
            with columns[index % 4]:
                st.html(f'<div class="sdlc-card"><b>{index + 1}. {stage.label}</b> <small>· {stage.agent}</small>'
                        f"<p style='margin:4px 0'>{stage.description}</p><small>📄 {stage.artifact}</small></div>")
        st.caption("Turuncu oklar geri besleme döngüleridir: İnceleme REVİZYON derse Tasarım Ajanı belgeyi yeniden yazar; "
                   "testler başarısızsa Geliştirme Ajanı kodu onarır. Testler Test Ajanı'na aittir, geliştirici onları değiştiremez.")


HOME_PAGE = st.Page(home_page, title="Yeni proje", icon="🏠", default=True)
PROJECTS_PAGE = st.Page(projects_page, title="Projeler", icon="📁")
st.navigation([HOME_PAGE, PROJECTS_PAGE]).run()
