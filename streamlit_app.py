"""AutoSDLC: proje oluşturma, canlı yaşam döngüsü, belge düzenleme ve proje yönetimi için yerel arayüz."""

from __future__ import annotations

import io
import os
import threading
import time
import uuid
import zipfile
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
import yaml
from dotenv import load_dotenv

from autosdlc.intake import choice_label, questions, templates, validate_decisions
from autosdlc.lifecycle import STAGE_KEYS, STAGE_NAMES, STAGES, counters, render_svg, stage_states
from autosdlc.models import available_models, default_model
from autosdlc.project_management import delete_project
from autosdlc.runner import RunPaused, run_project
from autosdlc.storage import History
from autosdlc.ui_jobs import WORKER_ERRORS


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
st.set_page_config(page_title="AutoSDLC", page_icon="🧭", layout="wide")

STATUS_LABELS = {
    "running": ("🔵", "Çalışıyor", "blue"), "paused": ("🟠", "Duraklatıldı", "orange"),
    "completed": ("🟢", "Tamamlandı", "green"), "failed": ("🔴", "Başarısız", "red"),
}
EVENT_NAMES = {
    "started": "Başladı", "completed": "Tamamlandı", "passed": "Testler geçti",
    "failed": "Başarısız", "repair_started": "Onarım başladı", "repair_completed": "Onarım tamamlandı",
    "question": "Kullanıcı kararı bekleniyor", "answered": "Yanıtlandı",
    "gate_failed": "Kalite kapısı: yeniden yazılıyor", "traceability": "İzlenebilirlik matrisi",
    "tests_protected": "Test dosyası değişikliği reddedildi", "approved": "İnceleme: ONAY",
    "changes_requested": "İnceleme: REVİZYON", "revision_started": "Revizyon başladı",
    "revision_completed": "Revizyon tamamlandı", "paused": "Duraklatıldı", "interrupted": "Kesintiye uğradı",
    "reset": "Yeniden üretim istendi", "edited": "Elle düzenlendi",
}
# (dosya, etiket, düzenlenirse hangi aşamadan sonrası yeniden üretilmeli; None: salt okunur)
DOCUMENTS = (
    ("planning.md", "Proje planı", "planning"),
    ("requirements.md", "Gereksinimler", "analysis"),
    ("architecture.md", "Mimari tasarım", "design"),
    ("design_review.md", "Mimari inceleme", "review"),
    ("traceability.md", "İzlenebilirlik matrisi", None),
    ("test_report.md", "Test raporu", None),
    ("deployment.md", "Dağıtım kılavuzu", "deployment"),
    ("maintenance_report.md", "Bakım planı", "maintenance"),
    ("decisions.yaml", "Mimari kararlar", "decisions"),
    ("clarifications.yaml", "Sorular ve yanıtlar", None),
)
SKIPPED_DIRS = {".autosdlc-venv", ".venv", "node_modules", ".git", "__pycache__", ".pytest_cache", "test-results", "dist", "build", "target"}

st.html("""<style>
div[data-testid="stMetric"] {background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:10px 14px;}
div[data-testid="stMetricValue"] {font-size:1.45rem;}
.sdlc-card {border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;background:#fff;height:100%;}
.sdlc-card b {color:#0f172a;} .sdlc-card small {color:#64748b;}
</style>""")


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


FORM_FIELDS = {"project_type", "stack", "constraints"}


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
    with st.container(border=True):
        pick, apply = st.columns([4, 1], vertical_alignment="bottom")
        selected = pick.selectbox(
            "📋 Örnek proje şablonu", [None, *catalog], key="template_choice",
            format_func=lambda key: "— Şablon seçerek formu doldur —" if key is None else catalog[key]["title"],
        )
        apply.button("Uygula", key="apply_template", disabled=selected is None, width="stretch",
                     on_click=apply_template, args=(selected,))
        st.caption(catalog[selected]["description"] if selected else "Bir şablon seçip Uygula'ya bas; alanları sonra istediğin gibi değiştirebilirsin.")


def api_key_field(prefix: str) -> tuple[bool, str | None]:
    """(anahtar hazır mı, kullanıcı anahtarı). .env anahtarı tarayıcıya hiç gönderilmez."""
    if os.getenv("OPENCODE_API_KEY") and not st.toggle("Farklı API anahtarı kullan", key=f"{prefix}_override"):
        st.caption("🔐 API anahtarı yerel `.env` dosyasından okunacak.")
        return True, None
    key = st.text_input("OpenCode Go API anahtarı", type="password", key=f"{prefix}_key", placeholder="oc_sk_…") or None
    return bool(key), key


def status_badge(status: str) -> None:
    icon, label, color = STATUS_LABELS.get(status, ("⚪", status, "gray"))
    st.badge(label, icon=icon, color=color)


def show_diagram(svg: str) -> None:
    # SVG kendi oranında kalsın; dar ekranda yalnızca diyagram alanı yatay kaydırılsın.
    components.html(
        '<style>html,body{margin:0;background:transparent;overflow:hidden}'
        '.diagram-scroll{width:100%;height:310px;overflow-x:auto;overflow-y:hidden}'
        '.diagram-canvas{width:1100px;height:289px;margin:0 auto}'
        '.diagram-canvas svg{display:block;width:1100px;height:289px}'
        '.diagram-hint{display:none}'
        '@media(max-width:700px){.diagram-hint{display:block;position:absolute;top:3px;right:8px;'
        'padding:4px 8px;border-radius:6px;background:#fff;color:#475569;font:12px Segoe UI,sans-serif}}'
        '</style><div class="diagram-hint">↔ Diyagramı kaydır</div>'
        '<div class="diagram-scroll"><div class="diagram-canvas">'
        f'{svg}</div></div>',
        height=316,
    )


def describe_activity(row: dict, states: dict[str, str]) -> str:
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
        return f"{STAGE_NAMES[waiting]} ajanı senden bir karar bekliyor (aşağıda)."
    last = row["events"][-1] if row["events"] else None
    if last is None:
        return "Çalışma başlatılıyor…"
    stage = STAGE_NAMES.get(last["stage"], last["stage"])
    return f"{stage} · {EVENT_NAMES.get(last['status'], last['status'])}" + (f" — {last['detail'][:160]}" if last["detail"] else "")


def lifecycle_view(row: dict) -> None:
    """Diyagram + metrikler. Yalnızca gösterim içerir; giriş alanları dışarıdadır."""
    states = stage_states(row["events"], row["status"])
    loops = counters(row["events"])
    show_diagram(render_svg(states, loops))
    done = sum(value == "done" for value in states.values())
    columns = st.columns(5)
    columns[0].metric("Aşama", f"{done}/{len(STAGES)}")
    columns[1].metric("İnceleme turu", loops["review_rounds"], help="Mimari İnceleme Ajanı'nın tur sayısı; revizyonda tasarım yeniden yazılır.")
    columns[2].metric("Test çalıştırma", loops["test_runs"])
    columns[3].metric("Onarım", loops["repairs"], help="Başarısız test sonrası Geliştirme Ajanı'nın düzeltme sayısı.")
    columns[4].metric("FR test kapsamı", f"%{loops['coverage']}" if loops["coverage"] is not None else "—")
    activity = describe_activity(row, states)
    {"completed": st.success, "failed": st.error, "paused": st.warning}.get(row["status"], st.info)(activity)


@st.fragment(run_every="2s")
def live_lifecycle(run_id: str) -> None:
    row = history().get(run_id)
    if row is None:
        if run_id in WORKER_ERRORS:
            st.error(WORKER_ERRORS[run_id])
        else:
            st.info("Çalışma başlatılıyor…")
        return
    lifecycle_view(row)
    pending = history().pending_question(run_id) if row["status"] == "running" else None
    signature = (row["status"], row["pause_requested"], pending and pending["id"], run_id in WORKER_ERRORS)
    key = f"signature_{run_id}"
    if key in st.session_state and st.session_state[key] != signature:
        st.session_state[key] = signature
        st.rerun()
    st.session_state[key] = signature


def run_controls(run_id: str) -> None:
    """Soru yanıtlama, durdurma ve devam ettirme. Otomatik yenilenen bölümün dışında durur."""
    db = history()
    row = db.get(run_id)
    if row is None:
        return
    if run_id in WORKER_ERRORS:
        st.error(WORKER_ERRORS[run_id])
    if row["status"] == "running":
        pending = db.pending_question(run_id)
        if pending and not row["pause_requested"]:
            with st.container(border=True):
                st.markdown(f"**❔ {STAGE_NAMES.get(pending['stage'], pending['stage'])} ajanı soruyor**")
                st.write(pending["prompt"])
                with st.form(f"answer_form_{pending['id']}", border=False):
                    answer = st.text_area("Yanıtın", height=90)
                    if st.form_submit_button("Yanıtla ve devam et", type="primary"):
                        try:
                            if db.answer(run_id, pending["id"], answer):
                                st.rerun()
                            st.info("Bu soru zaten yanıtlandı.")
                        except ValueError as exc:
                            st.error(str(exc))
        if not row["pause_requested"] and st.button("⏸ Çalışmayı durdur", key=f"pause_{run_id}"):
            db.request_pause(run_id)
            st.rerun()
    elif row["status"] == "paused":
        with st.container(border=True):
            st.markdown("**▶ Kaldığı yerden devam et**")
            ready, key = api_key_field(f"resume_{run_id}")
            if st.button("Devam et", key=f"resume_{run_id}", type="primary", disabled=not ready):
                resume_run(run_id, key)
                st.session_state["active_run_id"] = run_id
                st.rerun()


def lifecycle_intro() -> None:
    show_diagram(render_svg())
    columns = st.columns(4)
    for index, stage in enumerate(STAGES):
        with columns[index % 4]:
            st.html(
                f'<div class="sdlc-card"><b>{index + 1}. {stage.label}</b><br><small>{stage.agent}</small>'
                f"<p style='margin:6px 0 4px'>{stage.description}</p><small>📄 {stage.artifact}</small></div>"
            )
    st.caption(
        "Turuncu oklar geri besleme döngüleridir: İnceleme Ajanı REVİZYON derse Tasarım Ajanı belgeyi bulgulara "
        "göre yeniden yazar; testler başarısızsa Geliştirme Ajanı kodu onarır. Testler Test Ajanı'na aittir, "
        "geliştirici onları değiştiremez."
    )


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
    st.title("🧭 AutoSDLC")
    st.caption("Bir fikri sekiz ajanla yazılım yaşam döngüsünün tüm aşamalarından geçir: plan, gereksinim, tasarım, inceleme, kod, test, dağıtım ve bakım.")
    active = st.session_state.get("active_run_id")
    if active:
        with st.container(border=True):
            head, go, new = st.columns([4, 1.2, 1.2], vertical_alignment="center")
            row = history().get(active)
            head.subheader(row["idea"][:90] if row else "Yeni çalışma")
            if go.button("📁 Projeye git", width="stretch"):
                st.session_state["selected_run"] = active
                st.switch_page(PROJECTS_PAGE)
            if new.button("➕ Yeni proje", width="stretch"):
                st.session_state.pop("active_run_id")
                st.rerun()
            live_lifecycle(active)
            run_controls(active)
        return
    left, right = st.columns([2, 1], gap="large")
    with left:
        st.subheader("1 · Proje tanımı")
        template_picker()
        idea = st.text_area("Ne geliştirmek istiyorsun?", key="idea_input", placeholder="Örn: Küçük işletme için stok takip uygulaması; ürün ekleme, stok hareketi ve kritik stok uyarısı", height=130)
        catalog = questions()
        type_col, stack_col = st.columns([1, 2])
        project_type = type_col.selectbox(
            catalog["project_type"]["question"], catalog["project_type"]["options"],
            key="project_type_input", format_func=lambda value: choice_label("project_type", value),
        )
        stack = stack_col.text_input(catalog["stack"]["question"], key="stack_input", placeholder="Örn: Node.js/Express, pnpm, sade HTML/CSS/JS")
        decisions = {"project_type": project_type, "stack": stack}
        options = [(name, item) for name, item in catalog.items()
                   if name not in {"project_type", "stack", "constraints"} and (not item.get("when") or project_type in item["when"])]
        if options:
            st.markdown("**Mimari seçimler**")
            choice_columns = st.columns(len(options))
            for column, (name, item) in zip(choice_columns, options):
                decisions[name] = column.selectbox(
                    item["question"], item["options"], key=f"choice_{name}",
                    format_func=lambda value, key=name: choice_label(key, value),
                )
        decisions["constraints"] = st.text_area(catalog["constraints"]["question"], key="constraints_input", placeholder="Örn: pnpm kullan, ön yüz sade HTML olsun, Türkçe arayüz.", height=100)
        path_col, browse_col = st.columns([5, 1], vertical_alignment="bottom")
        output_path = path_col.text_input("Yeni proje klasörü (isteğe bağlı)", key="output_path", placeholder="Boşsa projects/<proje-adı>-<kimlik>")
        if browse_col.button("Gözat…", width="stretch"):
            choose_folder()
    with right:
        st.subheader("2 · Yapay zekâ")
        models = model_options()
        model = st.selectbox("OpenCode Go modeli", models, index=models.index(default_model(models)))
        ready, api_key = api_key_field("new")
        st.subheader("3 · Başlat")
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
                st.session_state["active_run_id"] = start_run(idea.strip(), validated, target, model, api_key)
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(str(exc))
        st.caption("Bir ajan kritik bir karar isterse aşama durur ve soru burada görünür.")
    st.divider()
    st.subheader("Yaşam döngüsü nasıl işler?")
    lifecycle_intro()


def project_files(project_dir: Path) -> list[Path]:
    files = []
    for path in sorted(project_dir.rglob("*")):
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


def save_document(db: History, row: dict, filename: str, content: str) -> None:
    if filename == "decisions.yaml":
        validate_decisions(yaml.safe_load(content))
    output = Path(row["output_dir"])
    (output / filename).write_text(content, encoding="utf-8")
    docs = Path(row["project_dir"]) / "docs"
    if docs.is_dir():
        (docs / filename).write_text(content, encoding="utf-8")
    stage = next((reset for name, _, reset in DOCUMENTS if name == filename), None)
    db.event(row["id"], stage if stage in STAGE_KEYS else "workflow", "edited", filename)


def regenerate(db: History, run_id: str, stage: str, api_key: str | None) -> None:
    if not db.reset_from(run_id, stage):
        raise ValueError("Çalışan bir proje yeniden üretilemez; önce durdur.")
    resume_run(run_id, api_key)
    st.session_state["active_run_id"] = run_id


def documents_tab(db: History, row: dict) -> None:
    output = Path(row["output_dir"])
    available = [(name, label, reset) for name, label, reset in DOCUMENTS if (output / name).is_file()]
    if not available:
        st.info("Henüz belge üretilmedi.")
        return
    choice = st.radio("Belge", [name for name, _, _ in available], horizontal=True,
                      format_func=lambda name: next(label for n, label, _ in available if n == name), key=f"doc_{row['id']}")
    _, label, reset = next(item for item in available if item[0] == choice)
    content = (output / choice).read_text(encoding="utf-8")
    editable = reset is not None and row["status"] != "running"
    editing = editable and st.toggle("✏️ Düzenle", key=f"edit_{row['id']}_{choice}")
    if not editing:
        st.caption(f"`{output / choice}`" + ("" if editable else " · salt okunur" if reset is None else " · çalışma sürerken düzenlenemez"))
        if choice.endswith(".yaml"):
            st.code(content, language="yaml")
        else:
            st.markdown(content)
        return
    edited = st.text_area(label, content, height=520, key=f"editor_{row['id']}_{choice}")
    next_stage = "planning" if reset == "decisions" else (STAGE_KEYS[STAGE_KEYS.index(reset) + 1] if STAGE_KEYS.index(reset) + 1 < len(STAGE_KEYS) else None)
    save_col, regen_col = st.columns(2)
    if save_col.button("💾 Kaydet", key=f"save_{row['id']}_{choice}", width="stretch"):
        try:
            save_document(db, row, choice, edited)
            st.toast("Belge kaydedildi.")
        except (ValueError, yaml.YAMLError, OSError) as exc:
            st.error(f"Kaydedilemedi: {exc}")
    if next_stage:
        with regen_col:
            ready, key = api_key_field(f"regen_doc_{row['id']}")
            if st.button(f"💾 Kaydet ve {STAGE_NAMES[next_stage]} aşamasından yeniden üret", key=f"save_regen_{row['id']}_{choice}",
                         type="primary", width="stretch", disabled=not ready):
                try:
                    save_document(db, row, choice, edited)
                    regenerate(db, row["id"], next_stage, key)
                    st.rerun()
                except (ValueError, yaml.YAMLError, OSError) as exc:
                    st.error(str(exc))
        st.caption("Sonraki aşamalar düzenlenmiş belgeyi girdi olarak kullanarak yeniden üretilir; önceki aşamalar korunur.")


def code_tab(row: dict) -> None:
    project_dir = Path(row["project_dir"])
    if not project_dir.is_dir():
        st.info("Proje klasörü bulunamadı.")
        return
    files = project_files(project_dir)
    if not files:
        st.info("Henüz kaynak dosya yok.")
        return
    left, right = st.columns([1, 3])
    with left:
        selected = st.radio("Dosyalar", files, format_func=lambda path: path.as_posix(), key=f"file_{row['id']}", label_visibility="collapsed")
        st.download_button("⬇️ Projeyi ZIP indir", project_zip(project_dir, files), file_name=f"{project_dir.name}.zip",
                           mime="application/zip", width="stretch")
    with right:
        path = project_dir / selected
        st.caption(f"`{path}`")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            st.info("Metin olmayan dosya.")
            return
        languages = {".py": "python", ".js": "javascript", ".ts": "typescript", ".go": "go", ".rs": "rust", ".java": "java",
                     ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".html": "html", ".css": "css", ".md": "markdown",
                     ".toml": "toml", ".sql": "sql", ".sh": "bash"}
        st.code(text[:60_000], language=languages.get(path.suffix, "docker" if path.name == "Dockerfile" else None), line_numbers=True)


def events_tab(row: dict) -> None:
    st.dataframe([{
        "Zaman": event["created_at"].replace("T", " ")[:19],
        "Aşama": STAGE_NAMES.get(event["stage"], "İş akışı" if event["stage"] == "workflow" else event["stage"]),
        "Olay": EVENT_NAMES.get(event["status"], event["status"]),
        "Ayrıntı": event["detail"],
    } for event in reversed(row["events"])], hide_index=True, width="stretch")
    if row["questions"]:
        st.markdown("**Ajan soruları**")
        for item in row["questions"]:
            st.markdown(f"- **{STAGE_NAMES.get(item['stage'], item['stage'])}:** {item['prompt']}  \n  ↳ {item['answer'] or '_Yanıt bekleniyor_'}")


def manage_tab(db: History, row: dict) -> None:
    run_id = row["id"]
    if row["status"] == "running":
        st.info("Düzenleme, yeniden üretme ve silme için önce çalışmayı durdur.")
        return
    output = Path(row["output_dir"])
    decisions_path = output / "decisions.yaml"
    decisions = yaml.safe_load(decisions_path.read_text(encoding="utf-8")) if decisions_path.is_file() else {}
    with st.container(border=True):
        st.markdown("**✏️ Fikri ve kısıtları düzenle**")
        idea = st.text_area("Proje fikri", row["idea"], key=f"idea_{run_id}", height=90)
        constraints = st.text_area("Teknik/ürün kısıtları", decisions.get("constraints", ""), key=f"constraints_{run_id}", height=70)
        if st.button("Kaydet", key=f"save_idea_{run_id}"):
            try:
                if not idea.strip() or len(idea) > 1000:
                    raise ValueError("Proje fikri 1–1000 karakter olmalı")
                updated = validate_decisions({**decisions, "constraints": constraints})
                db.update_idea(run_id, idea.strip())
                save_document(db, row, "decisions.yaml", yaml.safe_dump(updated, allow_unicode=True, sort_keys=False))
                st.toast("Kaydedildi. Değişikliğin belgelere yansıması için Planlama'dan yeniden üret.")
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(str(exc))
    with st.container(border=True):
        st.markdown("**🔁 Bir aşamadan itibaren yeniden üret**")
        st.caption("Seçilen aşama ve sonrası yeniden çalışır; önceki aşamaların belgeleri girdi olarak kullanılır.")
        stage = st.selectbox("Başlangıç aşaması", STAGE_KEYS, format_func=lambda key: STAGE_NAMES[key], key=f"regen_stage_{run_id}")
        ready, key = api_key_field(f"regen_{run_id}")
        if st.button("Yeniden üret", key=f"regen_{run_id}", type="primary", disabled=not ready):
            try:
                regenerate(db, run_id, stage, key)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with st.container(border=True):
        st.markdown("**🗑️ Projeyi sil**")
        st.caption(f"Proje: `{row['project_dir']}` · Raporlar: `{row['output_dir']}`")
        confirmed = st.checkbox("Bu projeyi ve raporlarını kalıcı olarak sil", key=f"delete_confirm_{run_id}")
        if st.button("Sil", key=f"delete_{run_id}", disabled=not confirmed):
            try:
                delete_project(ROOT, run_id)
                for name in ("active_run_id", "selected_run"):
                    if st.session_state.get(name) == run_id:
                        st.session_state.pop(name)
                st.toast("Proje silindi.")
                st.rerun()
            except (OSError, ValueError) as exc:
                st.error(f"Silinemedi: {exc}")


def projects_page() -> None:
    st.title("📁 Projeler")
    db = history()
    items = db.list(100)
    if not items:
        st.info("Henüz oluşturulmuş proje yok. **Yeni proje** sayfasından başlayabilirsin.")
        return
    ids = [item["id"] for item in items]
    if st.session_state.get("selected_run") not in ids:
        st.session_state["selected_run"] = ids[0]
    labels = {item["id"]: f"{STATUS_LABELS.get(item['status'], ('⚪',))[0]}  {item['idea'][:70]}  ·  {item['created_at'][:10]}" for item in items}
    st.selectbox("Proje", ids, format_func=lambda run_id: labels[run_id], key="selected_run")
    run_id = st.session_state["selected_run"]
    row = db.get(run_id)
    with st.expander(f"Tüm projeler ({len(items)})"):
        st.dataframe([{
            "Durum": STATUS_LABELS.get(item["status"], ("", item["status"]))[1], "Fikir": item["idea"],
            "Oluşturma": item["created_at"].replace("T", " ")[:16], "Test denemesi": item["attempts"], "Kimlik": item["id"],
        } for item in items], hide_index=True, width="stretch")
    head, badge = st.columns([5, 1], vertical_alignment="center")
    head.subheader(row["idea"])
    with badge:
        status_badge(row["status"])
    st.caption(f"Kimlik `{run_id}` · Model `{row['model'] or '—'}` · Proje `{row['project_dir']}`")
    lifecycle, documents, code, events, manage = st.tabs(["🔄 Yaşam döngüsü", "📄 Belgeler", "🗂️ Kod", "🕒 Olaylar", "⚙️ Yönet"])
    with lifecycle:
        if row["status"] == "running":
            live_lifecycle(run_id)
        else:
            lifecycle_view(row)
        run_controls(run_id)
    with documents:
        documents_tab(db, row)
    with code:
        code_tab(row)
    with events:
        events_tab(row)
    with manage:
        manage_tab(db, row)


HOME_PAGE = st.Page(home_page, title="Yeni proje", icon="🏠", default=True)
PROJECTS_PAGE = st.Page(projects_page, title="Projeler", icon="📁")
st.navigation([HOME_PAGE, PROJECTS_PAGE]).run()
