"""AutoSDLC: proje oluşturma, canlı aşamalar ve geçmiş için yerel arayüz."""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from autosdlc.intake import choice_label, questions, validate_decisions
from autosdlc.models import available_models, default_model
from autosdlc.project_management import delete_project
from autosdlc.runner import RunPaused, run_project
from autosdlc.storage import History
from autosdlc.ui_jobs import WORKER_ERRORS


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="AutoSDLC", page_icon="🧭", layout="wide")

STAGES = [
    ("planning", "Planlama", "Kapsam, hedefler, iş planı ve riskler"),
    ("analysis", "Analiz", "Gereksinimler, kullanıcı hikâyeleri ve kabul ölçütleri"),
    ("design", "Tasarım", "Mimari, API sözleşmeleri, veri şeması ve diyagramlar"),
    ("development", "Kodlama", "Seçilen teknolojiyle çalışan kaynak kod"),
    ("testing", "Test", "Birim, API ve varsa Playwright arayüz testleri"),
    ("deployment", "Dağıtım", "Dockerfile, Compose ve dağıtım kılavuzu"),
    ("maintenance", "Bakım", "İzleme, sorun yönetimi ve iyileştirme planı"),
]
STAGE_NAMES = {key: label for key, label, _ in STAGES}
EVENT_NAMES = {
    "started": "Başladı", "completed": "Tamamlandı", "passed": "Geçti",
    "failed": "Başarısız", "repair_started": "Düzeltme başladı",
    "repair_completed": "Düzeltme tamamlandı", "question": "Yanıt bekliyor",
    "answered": "Yanıtlandı",
}


def history() -> History:
    return History(ROOT / "runs" / "history.sqlite3")


@st.cache_data(ttl=3600)
def model_options() -> list[str]:
    return available_models()


def wait_for_answer(current_id: str, stage: str, prompt: str) -> str:
    db = history()
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


def start_run(idea: str, decisions: dict[str, str], output: Path | None,
              model: str, api_key: str | None) -> str:
    run_id = uuid.uuid4().hex[:12]

    def worker() -> None:
        try:
            run_project(
                idea, workspace=ROOT, model=model, api_key=api_key,
                decisions=decisions, output=output, run_id=run_id,
                on_question=wait_for_answer,
            )
        except Exception as exc:
            WORKER_ERRORS[run_id] = str(exc)

    threading.Thread(target=worker, daemon=True, name=f"autosdlc-{run_id}").start()
    return run_id


def resume_run(run_id: str, api_key: str | None) -> None:
    WORKER_ERRORS.pop(run_id, None)

    def worker() -> None:
        try:
            run_project("", workspace=ROOT, resume_run_id=run_id,
                        api_key=api_key, on_question=wait_for_answer)
        except Exception as exc:
            WORKER_ERRORS[run_id] = str(exc)

    threading.Thread(target=worker, daemon=True, name=f"autosdlc-resume-{run_id}").start()


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
        folders = sorted((path for path in current.iterdir() if path.is_dir()), key=lambda path: path.name.casefold())
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


def stage_states(events: list[dict]) -> dict[str, str]:
    states = {key: "pending" for key, _, _ in STAGES}
    for event in events:
        stage, status = event["stage"], event["status"]
        if stage not in states:
            continue
        if status in {"started", "repair_started", "repair_completed", "answered"}:
            states[stage] = "running"
        elif status == "question":
            states[stage] = "question"
        elif status in {"completed", "passed"}:
            states[stage] = "done"
        elif status == "failed":
            states[stage] = "failed"
    return states


def show_question(db: History, run_id: str) -> None:
    pending = db.pending_question(run_id)
    if not pending:
        return
    st.warning(f"{STAGE_NAMES.get(pending['stage'], pending['stage'])} ajanı kararını bekliyor")
    st.write(pending["prompt"])
    answer = st.text_area("Yanıtın", key=f"answer_{pending['id']}", height=85)
    if st.button("Yanıtla ve devam et", key=f"reply_{pending['id']}", type="primary"):
        try:
            if db.answer(run_id, pending["id"], answer):
                st.rerun()
            else:
                st.info("Bu soru zaten yanıtlandı.")
        except ValueError as exc:
            st.error(str(exc))


def render_run(run_id: str) -> None:
    db = history()
    row = db.get(run_id)
    if row is None:
        st.error(WORKER_ERRORS[run_id]) if run_id in WORKER_ERRORS else st.info("Çalışma başlatılıyor…")
        return
    states = stage_states(row["events"])
    done = sum(value == "done" for value in states.values())
    st.progress(done / len(STAGES), text=f"{done}/{len(STAGES)} aşama tamamlandı")
    if row["status"] == "running":
        if row["pause_requested"]:
            st.warning("Durdurma istendi. Süren ajan çağrısı tamamlanınca çalışma duraklayacak.")
        else:
            waiting = next((label for key, label, _ in STAGES if states[key] == "question"), None)
            active = next((label for key, label, _ in STAGES if states[key] == "running"), None)
            st.info(f"{waiting} aşamasında yanıt bekleniyor." if waiting else f"{active or 'İlk aşama'} çalışıyor. Bu görünüm otomatik yenilenir.")
            if st.button("Çalışmayı durdur", key=f"pause_{run_id}"):
                db.request_pause(run_id)
                st.rerun()
    elif row["status"] == "paused":
        st.warning("Çalışma duraklatıldı. Tamamlanan aşamalar korunuyor.")
        has_env_key = bool(os.getenv("OPENCODE_API_KEY"))
        override = st.checkbox("Farklı API anahtarı kullan", key=f"resume_override_{run_id}") if has_env_key else True
        resume_key = None
        if override:
            resume_key = st.text_input("Devam için API anahtarı", type="password", key=f"resume_key_{run_id}") or None
        if st.button("Kaldığı yerden devam et", key=f"resume_{run_id}", type="primary"):
            if resume_key or (has_env_key and not override):
                resume_run(run_id, resume_key)
                st.session_state["active_run_id"] = run_id
                st.rerun()
            else:
                st.error("Devam etmek için API anahtarı gerekli.")
    elif row["status"] == "completed":
        st.success(f"Proje hazır: {row['project_dir']}")
    else:
        st.error(row["error"] or "Çalışma tamamlanamadı; aşama olaylarını ve test raporunu incele.")
    for key, label, description in STAGES:
        icon, state_label = {
            "pending": ("○", "Sırada"), "running": ("◉", "Çalışıyor"),
            "question": ("❔", "Yanıt bekliyor"), "done": ("✓", "Tamamlandı"),
            "failed": ("!", "Başarısız"),
        }[states[key]]
        left, middle, right = st.columns([0.6, 5, 1.7], vertical_alignment="center")
        left.markdown(f"### {icon}")
        middle.markdown(f"**{label}** · {description}")
        right.caption(state_label)
    if row["status"] == "running" and not row["pause_requested"]:
        show_question(db, run_id)
    if run_id in WORKER_ERRORS:
        st.error(WORKER_ERRORS[run_id])
    if row["status"] != "running":
        st.caption(f"Çalışma kimliği: {run_id} · Proje: {row['project_dir']}")


@st.fragment(run_every="2s")
def live_run(run_id: str) -> None:
    render_run(run_id)


def home_page() -> None:
    st.title("Yeni proje")
    st.caption("Fikrini ve teknik kararlarını gir. Ajanların yedi SDLC aşamasındaki ilerlemesini canlı izle.")
    if st.session_state.get("active_run_id"):
        st.subheader("Çalışma durumu")
        live_run(st.session_state["active_run_id"])
        st.divider()
    left, right = st.columns([2, 1], gap="large")
    with left:
        st.subheader("Proje tanımı")
        idea = st.text_area("Ne geliştirmek istiyorsun?", placeholder="Örn: Küçük işletme için stok takip uygulaması", height=110)
        path_col, browse_col = st.columns([5, 1], vertical_alignment="bottom")
        with path_col:
            output_path = st.text_input("Yeni proje klasörü", key="output_path", placeholder="Boşsa projects/<proje-adı>-<kimlik>")
        with browse_col:
            if st.button("Gözat…", use_container_width=True):
                choose_folder()
        st.caption("Gözat ile üst klasörü seçip yeni proje adını yazabilirsin. Hedef klasör henüz mevcut olmamalı.")
        catalog = questions()
        project_type = st.selectbox(
            catalog["project_type"]["question"], catalog["project_type"]["options"],
            format_func=lambda value: choice_label("project_type", value),
        )
        stack = st.text_input(catalog["stack"]["question"], placeholder="Örn: Node.js/Express, pnpm, sade HTML/CSS/JS")
        decisions = {"project_type": project_type, "stack": stack}
        with st.expander("Veritabanı, önbellek ve diğer kararlar", expanded=True):
            for name, item in catalog.items():
                if name in {"project_type", "stack", "constraints"}:
                    continue
                if item.get("when") and project_type not in item["when"]:
                    continue
                decisions[name] = st.selectbox(
                    item["question"], item["options"], key=f"choice_{name}",
                    format_func=lambda value, key=name: choice_label(key, value),
                )
            decisions["constraints"] = st.text_area(
                catalog["constraints"]["question"],
                placeholder="Örn: pnpm kullan, ön yüz sade HTML olsun.", height=75,
            )
    with right:
        st.subheader("Yapay zekâ")
        models = model_options()
        model = st.selectbox("OpenCode Go modeli", models, index=models.index(default_model(models)))
        has_env_key = bool(os.getenv("OPENCODE_API_KEY"))
        override = st.checkbox("Farklı API anahtarı kullan") if has_env_key else True
        if has_env_key and not override:
            st.text_input("API anahtarı", value="••••••••••••", type="password", disabled=True)
            st.caption("Anahtar yerel .env dosyasından okunacak; gerçek değeri tarayıcıya gönderilmez.")
            api_key = None
        else:
            api_key = st.text_input("API anahtarı", type="password", placeholder="oc_sk_…") or None
            st.caption("Bu alan maskelidir; anahtar proje dosyalarına yazılmaz.")
        st.divider()
        st.subheader("Nasıl ilerler?")
        st.write("Planlama → Analiz → Tasarım → Kodlama → Test → Dağıtım → Bakım")
        st.caption("Her aşama belge veya çalışan dosya üretir. Test başarısızsa kodlama aşamasına geri döner. Bir ajan kritik karar isterse çalışmayı duraklatır; soruyu burada yanıtlayıp devam ettirirsin.")
        if st.button("Projeyi oluştur", type="primary", use_container_width=True):
            try:
                validated = validate_decisions(decisions)
                if not idea.strip() or len(idea) > 1000:
                    raise ValueError("Proje fikrini 1–1000 karakter arasında yaz.")
                if not (api_key or (has_env_key and not override)):
                    raise ValueError("OpenCode Go API anahtarı gerekli.")
                target = Path(output_path).expanduser().resolve() if output_path.strip() else None
                if target and target.exists():
                    raise ValueError(f"Proje klasörü zaten mevcut: {target}")
                run_id = start_run(idea, validated, target, model, api_key)
                st.session_state["active_run_id"] = run_id
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(str(exc))


@st.fragment(run_every="3s")
def project_history() -> None:
    db = history()
    items = db.list(50)
    if not items:
        st.info("Henüz oluşturulmuş proje yok.")
        return
    ids = [row["id"] for row in items]
    labels = {row["id"]: f"{row['idea'][:65]} · {row['id']} · " + {
        "running": "Çalışıyor", "paused": "Duraklatıldı", "completed": "Tamamlandı", "failed": "Başarısız",
    }.get(row["status"], row["status"]) for row in items}
    selected = st.selectbox("Projeyi seç", ids, format_func=lambda run_id: labels[run_id])
    row = db.get(selected)
    st.write(f"**Fikir:** {row['idea']}")
    st.write(f"**Proje klasörü:** `{row['project_dir'] or Path(row['output_dir']) / 'project'}`")
    st.write(f"**Rapor klasörü:** `{row['output_dir']}`")
    st.write(f"**Başlangıç:** {row['created_at']}")
    if row["status"] in {"running", "paused"}:
        render_run(selected)
    if row["status"] != "running":
        with st.expander("Projeyi sil"):
            st.write(f"Proje: `{row['project_dir']}`")
            st.write(f"Raporlar: `{row['output_dir']}`")
            confirmed = st.checkbox("Bu projeyi ve raporlarını kalıcı olarak sil", key=f"delete_confirm_{selected}")
            if st.button("Projeyi sil", key=f"delete_{selected}", disabled=not confirmed):
                try:
                    delete_project(ROOT, selected)
                    if st.session_state.get("active_run_id") == selected:
                        st.session_state.pop("active_run_id")
                    st.rerun()
                except (OSError, ValueError) as exc:
                    st.error(str(exc))
    with st.expander("Aşama olayları", expanded=row["status"] == "failed"):
        st.dataframe([{
            "Aşama": STAGE_NAMES.get(event["stage"], event["stage"]),
            "Durum": EVENT_NAMES.get(event["status"], event["status"]),
            "Ayrıntı": event["detail"], "Zaman": event["created_at"],
        } for event in row["events"]], hide_index=True)
    if row["questions"]:
        with st.expander("Sorular ve yanıtlar"):
            for item in row["questions"]:
                st.write(f"**{STAGE_NAMES.get(item['stage'], item['stage'])}:** {item['prompt']}")
                st.caption(item["answer"] or "Yanıt bekleniyor")
    output = Path(row["output_dir"])
    documents = (
        "decisions.yaml", "clarifications.yaml", "planning.md", "requirements.md", "architecture.md",
        "test_report.md", "deployment.md", "maintenance_report.md",
    )
    for name in documents:
        artifact = output / name
        if artifact.exists():
            with st.expander(name):
                content = artifact.read_text(encoding="utf-8")
                if name.endswith(".yaml"):
                    st.code(content, language="yaml")
                else:
                    st.markdown(content)


def projects_page() -> None:
    st.title("Projeler")
    st.caption("Önceki ve devam eden çalışmaların belgeleri, test raporları ve aşama olayları burada. Liste otomatik yenilenir.")
    project_history()


page = st.navigation([
    st.Page(home_page, title="Yeni proje", icon="🏠", default=True),
    st.Page(projects_page, title="Projeler", icon="📁"),
])
page.run()
