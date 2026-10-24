"""AutoSDLC: local UI for creating projects, the live lifecycle, agent chat, document editing and project management."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from html import escape
from pathlib import Path

import streamlit as st
import yaml
from dotenv import load_dotenv

from autosdlc.auth import auth_enabled, password_matches
from autosdlc.crewai_engine import CrewAIEngine
from autosdlc.exports import diagnostics_zip, export_files, export_zip, project_zip
from autosdlc.desktop import open_folder
from autosdlc.i18n import LANGUAGES, normalize, set_language, t, translate_stored
from autosdlc.intake import applies, choice_label, default_delivery, question_text, templates, questions, validate_decisions
from autosdlc.lifecycle import (
    STAGE_KEYS, STAGES, counters, format_duration, render_svg, stage_description, stage_label, stage_metrics, stage_states,
)
from autosdlc.models import available_models, default_model
from autosdlc.project_management import delete_project
from autosdlc.runner import (
    CHANGE_KINDS, DOC_BUDGET, SKIPPABLE_STAGES, STAGE_INPUTS, RunPaused, backup_versions, completed_from_events,
    open_questions, suggested_skips, parse_start_stage,
    run_project,
)
from autosdlc.sections import (
    delete_section, insert_after, replace_section, split_sections, strip_fences, unified_diff,
)
from autosdlc.storage import TITLE_LIMIT, History, project_title, suggested_title
from autosdlc.ui_jobs import WORKER_ERRORS


# AUTOSDLC_WORKSPACE lets tests use a temporary folder; normally it is the repository root.
ROOT = Path(os.getenv("AUTOSDLC_WORKSPACE") or Path(__file__).resolve().parent).resolve()
load_dotenv(Path(__file__).resolve().parent / ".env")
st.set_page_config(page_title="AutoSDLC", page_icon="🧭", layout="wide")


def use_language() -> None:
    """Applies the language chosen in this browser session; called at the top of every script run, fragment and dialog."""
    if "ui_language" not in st.session_state:
        st.session_state["ui_language"] = normalize(st.query_params.get("lang"))
    set_language(st.session_state["ui_language"])


use_language()


@st.cache_resource
def warn_auth_disabled() -> bool:
    """Logs once per server process; the cache keeps Streamlit's script reruns from repeating it."""
    logging.getLogger("autosdlc").warning("APP_PASSWORD is not set: the UI is open to everyone who can reach it")
    return True


def require_login() -> None:
    """Shows a password form instead of the app until APP_PASSWORD is entered; a no-op when no password is set."""
    if not auth_enabled():
        warn_auth_disabled()
        return
    if st.session_state.get("authenticated"):
        return
    st.title("AutoSDLC")
    with st.form("login"):
        password = st.text_input(t("Password"), type="password")
        submitted = st.form_submit_button(t("Sign in"))
    if submitted:
        if password_matches(password):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error(t("Wrong password."))
    st.stop()


STATUS_LABELS = {
    "running": ("🔵", "Running", "blue"), "paused": ("🟠", "Paused", "orange"),
    "completed": ("🟢", "Completed", "green"), "failed": ("🔴", "Failed", "red"),
}
STATE_ICONS = {"pending": "○", "running": "🔵", "question": "❔", "done": "✅", "failed": "❌", "paused": "⏸", "stale": "↻",
               "skipped": "↷"}
STATE_LABELS = {"pending": "Pending", "running": "Running", "question": "Waiting for answer", "done": "Completed",
                "failed": "Failed", "paused": "Paused", "stale": "Out of date", "skipped": "Skipped"}
SECTION_ACTIONS = {"question": "💬 Ask", "rewrite": "✍️ Rewrite", "add": "➕ Add", "delete": "🗑️ Delete", "manual": "✏️ Manual"}
CHANGE_STATUS = {"draft": "📝 Draft", "analyzed": "🔍 Analyzed", "in_progress": "🔵 In progress",
                 "done": "✅ Completed", "failed": "❌ Failed", "rejected": "✖ Rejected"}
ITERATION_ICONS = {"completed": "✅", "failed": "❌"}
EVENT_NAMES = {
    "started": "Started", "completed": "Completed", "passed": "Tests passed",
    "failed": "Failed", "repair_started": "Repair started", "repair_completed": "Repair completed",
    "question": "Waiting for user decision", "answered": "Answered",
    "gate_failed": "Quality gate: rewriting", "traceability": "Traceability matrix",
    "tests_protected": "Test file change rejected", "approved": "Review: APPROVE",
    "changes_requested": "Review: REVISE", "revision_started": "Revision started",
    "revision_completed": "Revision completed", "paused": "Paused", "interrupted": "Interrupted",
    "reset": "Regeneration requested", "edited": "Edited manually", "stale": "Later stages out of date",
    "section_question": "Section question", "change_started": "Change started", "change_completed": "Change completed",
    "change_failed": "Change failed", "bug_reproduced": "Bug reproduced by a test",
    "bug_not_reproduced": "Bug not reproduced", "regression": "Regression test", "tests_not_covered": "Test not executed",
    "delta": "Section patch", "skipped": "Stage skipped", "revision_skipped": "Revision not needed",
    "build_failed": "Does not compile", "build_fixed": "Compiles",
    "services_started": "Test services started", "services_unavailable": "Test service missing",
    "package_started": "Building executables", "packaged": "Executables built", "package_failed": "Packaging failed", "package_unavailable": "No executables",
    "package_skipped": "Packaging skipped", "files_skipped": "Files skipped",
    "tests_repair_started": "Test repair started", "tests_repaired": "Tests repaired",
}
# Stage -> (file, label, stage to regenerate from when edited). None: read-only.
STAGE_DOCUMENTS = {
    "planning": [("planning.md", "Project plan", "analysis")],
    "analysis": [("requirements.md", "Requirements", "design")],
    "design": [("architecture.md", "Architecture design", "review")],
    "review": [("design_review.md", "Architecture review", "development")],
    "testing": [("test_report.md", "Test report", None), ("traceability.md", "Traceability matrix", None)],
    "deployment": [("deployment.md", "Deployment guide", "maintenance")],
    "maintenance": [("maintenance_report.md", "Maintenance plan", None)],
}
CODE_LANGUAGES = {".py": "python", ".js": "javascript", ".ts": "typescript", ".go": "go", ".rs": "rust", ".java": "java",
                  ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".html": "html", ".css": "css", ".md": "markdown",
                  ".toml": "toml", ".sql": "sql", ".sh": "bash"}
MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
MERMAID_SCRIPT = (
    '<script type="module">import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs";'
    'mermaid.initialize({startOnLoad:false,theme:"neutral",securityLevel:"strict"});'
    'try {await mermaid.run({querySelector:".mermaid"});} catch(e) {'
    'document.querySelector(".mermaid").textContent=%s;}'
    '</script>'
)
FORM_FIELDS = {"project_type", "stack", "constraints"}
FORM_LABELS = {"database": "Database", "cache": "Cache", "observability": "Monitoring", "ui": "Web UI",
               "delivery": "Delivery", "platforms": "Platforms", "scale": "Scale", "language": "Document language"}

st.html("""<style>
.block-container {padding-top:4.5rem; padding-bottom:5rem; max-width:1600px;}
[data-testid="stAppViewContainer"] {background:#f8fafc;}
[data-testid="stHeader"] {background:rgba(248,250,252,.96);}
[data-testid="stSidebar"] {border-right:1px solid #e2e8f0;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius:16px;}
div[data-testid="stMetric"] {background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:10px 14px;}
div[data-testid="stMetricValue"] {font-size:1.3rem;}
div[data-testid="stMetricLabel"] p {font-size:.78rem;}
h1 {font-size:1.85rem !important; line-height:1.35 !important; padding:.25rem 0 .6rem !important;}
h4 {line-height:1.45 !important; padding:.35rem 0 !important;}
.project-title {font-size:1.8rem;line-height:1.4;font-weight:700;color:#102a43;overflow-wrap:anywhere;margin:0 0 .4rem;}
button {border-radius:9px !important;}
[data-testid="stButtonGroup"] [role="radiogroup"] {gap:8px;flex-wrap:wrap;}
[data-testid="stButtonGroup"] button[role="radio"] {margin:0 !important;}
[data-testid="stCaptionContainer"] {overflow-wrap:anywhere;}
@media(max-width:768px) {
 .block-container {padding:4.5rem 1rem 3rem;}
 .project-title {font-size:1.4rem;}
}
div[data-testid="stChatMessage"] {padding:.3rem .55rem; gap:.55rem; margin-bottom:.1rem;}
div[data-testid="stChatMessage"] p {margin-bottom:.05rem; font-size:.88rem; line-height:1.35;}
div[data-testid="stChatMessageAvatarUser"], div[data-testid="stChatMessageAvatarAssistant"] {width:1.6rem;height:1.6rem;}
.sdlc-card {border:1px solid #e2e8f0;border-radius:10px;padding:8px 12px;background:#fff;height:100%;font-size:.88rem;}
.sdlc-card b {color:#0f172a;} .sdlc-card small {color:#64748b;}
</style>""")


# ---------------------------------------------------------------- background jobs

def history() -> History:
    db = History(ROOT / "runs" / "history.sqlite3")
    # If the UI was restarted, runs left half-way must not stay locked.
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
    raise TimeoutError("The user's answer did not arrive within one hour")


def _spawn(job_id: str, name: str, **kwargs) -> None:
    # job_id is the same value as the run_id/resume_run_id passed to run_project but is named apart to avoid a clash.
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
        raise ValueError(t("A running project cannot be regenerated; stop it first."))
    resume_run(run_id, api_key)


# ---------------------------------------------------------------- small helpers

def api_key_field(prefix: str) -> tuple[bool, str | None]:
    """(key ready?, user key). The .env key is never sent to the browser."""
    if os.getenv("OPENCODE_API_KEY") and not st.toggle(t("Use a different API key"), key=f"{prefix}_override"):
        st.caption(t("🔐 The API key will be read from the local `.env` file."))
        return True, None
    key = st.text_input(t("OpenCode Go API key"), type="password", key=f"{prefix}_key", placeholder="oc_sk_…") or None
    return bool(key), key


def show_diagram(svg: str, *, zoom: int = 100) -> None:
    # Keep the SVG aspect ratio. Content height is measured with width/zoom so bottom labels are not clipped.
    st.iframe(
        '<style>html,body{margin:0;padding:0;background:transparent} '
        'svg{display:block;width:100%;height:auto} .graph{min-width:860px}</style>'
        f'<div class="graph" style="width:{zoom}%">{svg}</div>',
        height="content", alt=t("SDLC lifecycle node graph"),
    )


def render_markdown(content: str) -> None:
    """Shows Markdown; ```mermaid blocks are drawn as real diagrams (needs internet, otherwise the text stays)."""
    position = 0
    failure = json.dumps(t("The diagram could not be drawn. Download the Markdown file to inspect the source."))
    for match in MERMAID_BLOCK.finditer(content):
        if content[position:match.start()].strip():
            st.markdown(content[position:match.start()])
        # Mermaid code is LLM output: escape() passes it as text only and Mermaid renders in "strict" security mode.
        st.iframe('<style>html,body{margin:0;padding:0} .mermaid{margin:0;padding:12px 0;white-space:pre-wrap;'
                  'font-family:system-ui;box-sizing:border-box} .mermaid svg{display:block;width:100%;'
                  'height:auto;max-height:none!important;margin:auto}</style>'
                  f'<pre class="mermaid">{escape(match.group(1))}</pre>{MERMAID_SCRIPT % failure}',
                  height="content", alt=t("Document Mermaid diagram"))
        position = match.end()
    if content[position:].strip():
        st.markdown(content[position:])


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def event_name(status: str) -> str:
    return t(EVENT_NAMES.get(status, status))


def active_stage(states: dict[str, str]) -> str:
    for wanted in ("question", "running", "failed", "paused"):
        found = next((key for key in STAGE_KEYS if states[key] == wanted), None)
        if found:
            return found
    done = [key for key in STAGE_KEYS if states[key] in {"done", "skipped"}]
    return done[-1] if done else STAGE_KEYS[0]


def describe_activity(row: dict, states: dict[str, str]) -> str:
    stale = [stage_label(key) for key in STAGE_KEYS if states[key] == "stale"]
    if stale and row["status"] != "running":
        return t("Documents were edited; {count} stages are out of date. Update them from the warning below.", count=len(stale))
    if row["status"] == "completed":
        return t("All stages completed and the tests passed.")
    if row["status"] == "failed":
        return translate_stored(row["error"]) if row["error"] else t("The run could not be completed; check the events and the test report.")
    if row["status"] == "paused":
        interrupted = any(event["status"] == "interrupted" for event in row["events"][-3:])
        return (t("The process was interrupted; you can resume where it stopped.") if interrupted
                else t("The run is paused; completed stages are kept."))
    if row["pause_requested"]:
        return t("Stop requested; the run will pause when the current agent call finishes.")
    waiting = next((key for key in STAGE_KEYS if states[key] == "question"), None)
    if waiting:
        return t("The {stage} agent is waiting for a decision — type your answer in the chat box below.", stage=stage_label(waiting))
    last = row["events"][-1] if row["events"] else None
    if last is None:
        return t("Starting the run…")
    stage = stage_label(last["stage"]) if last["stage"] in STAGE_KEYS else last["stage"]
    return f"{stage} · {event_name(last['status'])}" + (f" — {translate_stored(last['detail'])[:140]}" if last["detail"] else "")


# ---------------------------------------------------------------- chat

def chat_messages(row: dict) -> list[tuple[str, str, str]]:
    """(time, role, text) list: agent events, questions, user answers and notes in chronological order."""
    agents = {stage.key: t(stage.agent) for stage in STAGES}
    messages = []
    for event in row["events"]:
        stage, status = event["stage"], event["status"]
        detail = translate_stored(event["detail"])
        agent = agents.get(stage, t("Workflow"))
        name = stage_label(stage) if stage in STAGE_KEYS else stage
        detail_stage = stage_label(event["detail"]) if event["detail"] in STAGE_KEYS else None
        text = {
            "started": t("▶ I started the **{stage}** stage.", stage=name),
            "completed": t("✅ Ready: `{detail}`", detail=detail) if detail and len(detail) < 80 else t("✅ Stage completed."),
            "gate_failed": t("⚠️ It failed the quality gate: {detail} — rewriting.", detail=detail),
            "changes_requested": t("🔁 The design has findings to fix ({detail}); sending it back to the Design Agent.", detail=detail),
            "approved": t("✅ I approved the design ({detail}).", detail=detail),
            "revision_started": t("✏️ I am revising the design according to the review findings."),
            "revision_skipped": t("➡️ No revision needed: {detail}.", detail=detail),
            "delta": t("🧩 Only the changed sections: {detail}.", detail=detail),
            "skipped": t("↷ Stage skipped: {detail}; the previous document was kept.", detail=detail),
            "passed": t("✅ Tests passed ({detail}).", detail=detail),
            "failed": t("❌ Tests failed ({detail}).", detail=detail) if stage == "testing" else f"❌ {detail}",
            "repair_started": {
                "from the compile check": t("🛠️ I am fixing the code according to the compile errors."),
                "from the packaging build": t("🛠️ I am fixing the code according to the release build errors."),
            }.get(event["detail"], t("🛠️ I am fixing the code according to the test failures.")),
            "tests_protected": t("🛡️ An attempt to change the test files was rejected: {detail}", detail=detail),
            "traceability": t("🔗 Traceability matrix produced: {detail}.", detail=detail),
            "question": t("❔ **I have a question:** {detail}", detail=detail),
            "paused": t("⏸ The run was paused."),
            "interrupted": t("⚠️ The process was interrupted; it can be resumed where it stopped."),
            "reset": t("🔁 Regeneration was requested from the **{stage}** stage.", stage=detail_stage or detail),
            "edited": t("✏️ `{detail}` was edited.", detail=detail),
            "stale": t("↻ **{stage}** changed; later stages are out of date.", stage=detail_stage or t("Architecture decisions")),
            "section_question": f"💬 {detail}",
            "change_started": t("🔁 Change iteration started: **{detail}**", detail=detail),
            "change_completed": t("✅ Change completed: **{detail}**", detail=detail),
            "change_failed": t("❌ Change failed: **{detail}**", detail=detail),
            "bug_reproduced": t("🐞 I wrote a regression test that reproduces the bug; it fails right now (expected)."),
            "bug_not_reproduced": t("⚠️ My regression test could not reproduce the bug; trying the fix anyway."),
            "regression": t("🔁 Regression: {detail}.", detail=detail),
            "tests_not_covered": t("⚠️ Test files not executed by the project.yaml commands: {detail}", detail=detail),
            "services_started": t("🗄️ A throwaway database/cache was started for the tests ({detail}).", detail=detail),
            "services_unavailable": t("⚠️ {detail}", detail=detail),
            "build_failed": t("🧱 The code does not compile yet; it goes back to its author before the tests run (this does not use up a test attempt)."),
            "build_fixed": t("✅ The code compiles now ({detail}).", detail=detail),
            "package_started": t("🔨 Building the executables ({detail}); this can take several minutes.", detail=detail),
            "packaged": t("📦 Executables built: {detail}", detail=detail),
            "package_failed": t("⚠️ The executables could not be built ({detail}); the tests are not affected.", detail=detail),
            "package_unavailable": t("ℹ️ This stack cannot be built into executables; launcher scripts are delivered instead."),
            "package_skipped": t("↷ No executables were built because the tests did not pass."),
            "files_skipped": t("ℹ️ Files in the reserved docs/ folder were not written: {detail}", detail=detail),
            "tests_repair_started": t("🛠️ The tests themselves do not compile; I am fixing my own test files."),
            "tests_repaired": t("✅ Test files fixed: {detail}", detail=detail),
        }.get(status)
        if text:
            messages.append((event["created_at"], agent, text))
    for item in row["questions"]:
        if item["answer"] and item["answered_at"] and item["answer"] not in {"Previous question was superseded", "Önceki soru yenilendi"}:
            messages.append((item["answered_at"], "user", item["answer"]))
    for note in row.get("notes", []):
        messages.append((note["created_at"], "user", f"📝 {note['content']}"))
    return sorted(messages, key=lambda item: item[0])


def chat_feed(run_id: str) -> None:
    """Agent feed: chronological; Streamlit scrolls a fixed-height chat box to the bottom (the newest message)."""
    use_language()
    db = history()
    row = db.get(run_id)
    if row is None:
        return
    pending = db.pending_question(run_id) if row["status"] == "running" else None
    if pending:
        st.warning(t("**The {stage} agent is asking:** {prompt}", stage=stage_label(pending["stage"]), prompt=pending["prompt"])
                   + "  \n" + t("↓ Type your answer in the box below."), icon="❔")
    st.caption(t("💬 Agent feed"))
    with st.container(height=300 if pending else 400, border=True):
        messages = chat_messages(row)[-80:]
        if not messages:
            st.caption(t("The agents' messages will flow here."))
        for moment, role, text in messages:
            user = role == "user"
            with st.chat_message("user" if user else "assistant", avatar="🧑" if user else "🤖"):
                who = t("You") if user else escape(role)
                st.markdown(f"<small><b>{who}</b> · {moment[11:19]}</small>  \n{text}", unsafe_allow_html=True)


def chat_placeholder(db: History, row: dict) -> tuple[dict | None, str]:
    pending = db.pending_question(row["id"]) if row["status"] == "running" and not row["pause_requested"] else None
    placeholder = (t("❔ Answer the {stage} agent's question…", stage=stage_label(pending["stage"])) if pending
                   else t("Write a note to the agents (e.g. 'the UI should use a dark theme'); later stages take it into account…"))
    return pending, placeholder


def handle_chat(db: History, row: dict) -> None:
    """Processes a submitted chat message before the page is drawn: an answer if a question is pending, else a steering note.

    The chat box is drawn inline (a bottom-pinned one makes Streamlit always scroll the page down), so its value is
    read here, before the feed sections are drawn.
    """
    text = st.session_state.get(f"chat_{row['id']}")
    if not text:
        return
    pending, _ = chat_placeholder(db, row)
    try:
        if pending:
            if not db.answer(row["id"], pending["id"], text):
                st.toast(t("This question was already answered."))
        else:
            db.add_note(row["id"], text)
            st.toast(t("Note saved; it will be added to the context of later stages.") if row["status"] == "running"
                     else t("Note saved; it will be taken into account on regeneration."))
    except ValueError as exc:
        st.toast(translate_stored(str(exc)))


# ---------------------------------------------------------------- stage detail

def project_files(project_dir: Path) -> list[Path]:
    # Dotfiles last: the first file opened should be real source, not .dockerignore.
    return sorted(export_files(project_dir), key=lambda item: (item.name.startswith("."), str(item)))


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
    """Saves the edit, marks later stages 'out of date' and records it for the delta update.

    stale_from: the stage after which everything is affected; "decisions" affects all stages (default: stage).
    """
    save_document(db, row, filename, after, stage)
    path = pending_edits_path(row)
    edits = json.loads(read_text(path) or "[]")
    edits.append({"stage": stage, "file": filename, "summary": summary, "diff": unified_diff(before, after, filename)[:3000]})
    path.write_text(json.dumps(edits, ensure_ascii=False, indent=1), encoding="utf-8")
    db.event(row["id"], "workflow", "stale", stale_from or stage)


def propagate(db: History, row: dict, start: str, delta: bool, api_key: str | None) -> None:
    """Re-runs out-of-date stages; in delta mode earlier documents are kept and only affected parts change."""
    path = pending_edits_path(row)
    edits = json.loads(read_text(path) or "[]")
    if delta:
        description = "The user made these document edits; update the later stages accordingly:\n" + "\n".join(
            f"- {edit['file']} · {edit['summary']}\n{edit['diff'][:1500]}" for edit in edits
        )
        change_id = db.create_change(row["id"], "edit", f"Document edits ({len(edits) or 1})", description[:3990], status="in_progress")
        db.update_change(row["id"], change_id, start_stage=start)
    regenerate(db, row["id"], start, api_key)
    path.unlink(missing_ok=True)


def stage_context(row: dict, stage: str) -> str:
    """Agent context for section operations: the decisions and the earlier documents that are this stage's input."""
    output = Path(row["output_dir"])
    parts = [f"Architecture decisions:\n{read_text(output / 'decisions.yaml') or ''}"]
    names = {"planning": "planning.md", "analysis": "requirements.md", "design": "architecture.md", "review": "design_review.md",
             "deployment": "deployment.md", "maintenance": "maintenance_report.md"}
    for name in STAGE_INPUTS.get(stage, ()):
        text = read_text(output / names.get(name, "")) if name in names else None
        if text:
            parts.append(f"--- {name} document ---\n{text[:DOC_BUDGET]}")
    return "\n\n".join(parts)


def make_engine(row: dict, api_key: str | None) -> CrewAIEngine:
    return CrewAIEngine(api_key=api_key, model=row["model"] or None)


def document_language(row: dict) -> str:
    path = Path(row["output_dir"]) / "decisions.yaml"
    decisions = yaml.safe_load(read_text(path) or "") or {}
    return normalize(decisions.get("language"))


def open_focus(row: dict, path: Path, title: str) -> None:
    st.session_state["focus"] = {"run": row["id"], "path": str(path), "title": title}
    st.rerun()


def focus_view(focus: dict) -> None:
    """Shows a document or the README at full width, without the box height limit."""
    use_language()
    back, title, download = st.columns([1.2, 6, 1.5], vertical_alignment="center")
    if back.button(t("← Back"), width="stretch"):
        st.session_state.pop("focus")
        st.rerun()
    title.markdown(f"#### {escape(t(focus['title']))}")
    if focus.get("kind") == "graph":
        zoom = st.slider(t("Graph size"), 100, 200, 125, 25, format="%%%d")

        def live_graph():
            use_language()
            row = history().get(focus["run"])
            svg = lifecycle_svg(row)
            st.download_button(t("Download SVG"), svg, file_name="lifecycle.svg", mime="image/svg+xml",
                               icon=":material/download:", on_click="ignore")
            show_diagram(svg, zoom=zoom)
            st.caption(describe_activity(row, stage_states(row["events"], row["status"])))
        st.fragment(live_graph, run_every=2)()
        return
    st.caption(f"`{focus['path']}`")
    path = Path(focus["path"])
    content = read_text(path)
    if content is None:
        st.warning(t("File not found."))
    else:
        download.download_button(t("Download"), content, file_name=path.name, mime="text/markdown",
                                 icon=":material/download:", width="stretch", on_click="ignore")
        render_markdown(content)


def section_panel(db: History, row: dict, stage: str, filename: str, content: str, can_edit: bool) -> None:
    """Asking questions about and editing one section (heading or FR/NFR) of a document with proposal → diff → approval."""
    sections = split_sections(content)
    if not sections:
        st.caption(t("No heading or requirement found in the document."))
        return
    prefix = f"{row['id']}_{filename}"
    labels = {item.key: " " * (item.level - 1) + ("📌 " if item.kind == "requirement" else "§ ") + item.title for item in sections}
    key = st.selectbox(t("Section"), [item.key for item in sections], format_func=labels.get, key=f"section_{prefix}")
    section = next(item for item in sections if item.key == key)
    lines = content.split("\n")
    with st.container(height=130, border=True):
        st.markdown(section.text(lines))
    actions = ["question"] + (["rewrite", "add", "delete", "manual"] if can_edit else [])
    action = st.segmented_control(t("Action"), actions, format_func=lambda name: t(SECTION_ACTIONS[name]), default="question",
                                  key=f"action_{prefix}", label_visibility="collapsed") or "question"
    proposal_key, answer_key = f"proposal_{prefix}", f"answer_{prefix}"
    if action == "manual":
        new_text = st.text_area(t("New version of the section"), section.text(lines), height=160, key=f"manual_{prefix}_{key}")
        if st.button(t("Preview change"), key=f"preview_manual_{prefix}"):
            st.session_state[proposal_key] = {"before": content, "after": replace_section(content, section, new_text),
                                              "summary": f"Edited manually · {section.title}"}
    elif action == "delete":
        if st.button(t("Preview deletion"), key=f"preview_delete_{prefix}"):
            st.session_state[proposal_key] = {"before": content, "after": delete_section(content, section),
                                              "summary": f"Deleted · {section.title}"}
    else:
        placeholders = {"question": t("E.g. Why is this requirement needed? Are the acceptance criteria enough?"),
                        "rewrite": t("E.g. Rewrite the acceptance criteria in Given/When/Then form."),
                        "add": t("E.g. Add a requirement under this section for searching products by barcode.")}
        instruction = st.text_area(t("Question") if action == "question" else t("Instruction"), key=f"instruction_{prefix}_{action}",
                                   height=70, placeholder=placeholders[action])
        ready, api_key = api_key_field(f"section_{prefix}")
        label = t("Ask") if action == "question" else t("Get proposal")
        if st.button(label, key=f"ask_{prefix}", type="primary", disabled=not (ready and instruction.strip())):
            agent = t(next(item.agent for item in STAGES if item.key == stage))
            try:
                with st.spinner(t("{agent} is working…", agent=agent)):
                    text = make_engine(row, api_key).section(stage, row["idea"], stage_context(row, stage), content,
                                                             section.text(lines), instruction, action,
                                                             language=document_language(row))
            except (RuntimeError, ValueError) as exc:
                st.error(translate_stored(str(exc)))
                return
            if action == "question":
                st.session_state[answer_key] = text
                db.event(row["id"], stage, "section_question", f"**{section.title}** — {instruction.strip()}\n\n{text[:2500]}")
            else:
                new_text = strip_fences(text)
                after = replace_section(content, section, new_text) if action == "rewrite" else insert_after(content, section, new_text)
                st.session_state[proposal_key] = {"before": content, "after": after,
                                                  "summary": f"{action.capitalize()} · {section.title}: {instruction.strip()[:200]}"}
    if action == "question" and st.session_state.get(answer_key):
        with st.container(height=200, border=True):
            st.markdown(st.session_state[answer_key])
    proposal = st.session_state.get(proposal_key)
    if proposal and action != "question":
        if proposal["before"] != content:
            st.session_state.pop(proposal_key)  # The document changed meanwhile; the old proposal is invalid.
            return
        diff = unified_diff(proposal["before"], proposal["after"], filename)
        st.caption(t("Proposed change — if you accept it, it is saved and later stages are marked 'out of date'."))
        with st.container(height=220, border=True):
            st.code(diff or t("(no change)"), language="diff")
        accept, reject = st.columns(2)
        if accept.button(t("✅ Accept"), key=f"accept_{prefix}", type="primary", width="stretch", disabled=not diff):
            accept_edit(db, row, stage, filename, proposal["before"], proposal["after"], proposal["summary"])
            st.session_state.pop(proposal_key)
            st.toast(t("Change saved; later stages were marked 'out of date'."))
            st.rerun()
        if reject.button(t("✖ Reject"), key=f"reject_{prefix}", width="stretch"):
            st.session_state.pop(proposal_key)
            st.rerun()


def live_output(row: dict, stage: str) -> None:
    text = read_text(Path(row["output_dir"]) / "live" / f"{stage}.md")
    st.caption(t("🟢 The agent's output is streaming live (the latest part is shown)"))
    with st.container(height=360, border=True):
        if not text:
            st.caption(t("The agent is thinking… the first words will appear here shortly."))
        elif stage in {"development", "testing"}:
            st.code(text[-5000:], language="json", wrap_lines=True)
        else:
            st.markdown(text[-6000:])


def document_view(db: History, row: dict, stage: str) -> None:
    output = Path(row["output_dir"])
    available = [item for item in STAGE_DOCUMENTS.get(stage, []) if (output / item[0]).is_file()]
    if not available:
        st.caption(t("This stage's document has not been produced yet."))
        return
    if len(available) > 1:
        choice = st.segmented_control(t("Document"), [name for name, _, _ in available], key=f"doc_{row['id']}_{stage}",
                                      format_func=lambda name: t(next(label for n, label, _ in available if n == name)),
                                      default=available[0][0], label_visibility="collapsed") or available[0][0]
    else:
        choice = available[0][0]
    filename, label, regen_from = next(item for item in available if item[0] == choice)
    label = t(label)
    content = read_text(output / filename) or ""
    can_edit = regen_from is not None and row["status"] != "running"
    modes = {"read": t("📖 Read"), "section": t("🎯 Section"), **({"full": t("✏️ Whole")} if can_edit else {})}
    info, mode_col = st.columns([2, 3], vertical_alignment="center")
    info.caption(f"📄 `{filename}`" + ("" if can_edit else t(" · read-only") if regen_from is None else t(" · cannot be edited while running")))
    mode = mode_col.segmented_control(t("Mode"), list(modes), format_func=modes.get, default="read",
                                      key=f"mode_{row['id']}_{filename}", label_visibility="collapsed") or "read"
    with st.container(horizontal=True):
        if st.button(t("Enlarge"), icon=":material/open_in_full:", key=f"focus_{row['id']}_{filename}", help=t("Open the document at full width")):
            open_focus(row, output / filename, label)
        st.download_button(t("Download"), content, file_name=filename, mime="text/markdown",
                           icon=":material/download:", key=f"download_{row['id']}_{filename}", on_click="ignore")
    if mode == "section":
        section_panel(db, row, stage, filename, content, can_edit)
    elif mode == "full":
        edited = st.text_area(label, content, height=320, key=f"editor_{row['id']}_{filename}", label_visibility="collapsed")
        if st.button(t("💾 Save"), key=f"save_{row['id']}_{filename}", disabled=edited == content):
            accept_edit(db, row, stage, filename, content, edited, "The whole document was edited manually")
            st.toast(t("Saved; later stages were marked 'out of date'."))
            st.rerun()
    else:
        with st.container(border=True):
            render_markdown(content)


def code_view(row: dict) -> None:
    project_dir = Path(row["project_dir"])
    files = project_files(project_dir) if project_dir.is_dir() else []
    if not files:
        st.caption(t("There are no source files yet."))
        return
    pick, download = st.columns([3, 1], vertical_alignment="bottom")
    selected = pick.selectbox(t("Files ({count})", count=len(files)), files, format_func=lambda path: path.as_posix(), key=f"file_{row['id']}")
    download.download_button(t("Code ZIP"), lambda: export_zip(project_dir, Path(row["output_dir"]), "code"),
                            file_name=f"{row['id']}-code.zip", icon=":material/download:",
                            mime="application/zip", width="stretch", on_click="ignore")
    st.download_button(t("Download file"), lambda: (project_dir / selected).read_bytes(), file_name=selected.name,
                       icon=":material/download:", key=f"source_download_{row['id']}", on_click="ignore")
    text = read_text(project_dir / selected)
    with st.container(border=True):
        if text is None:
            st.caption(t("Not a text file."))
        else:
            language = CODE_LANGUAGES.get(selected.suffix, "docker" if selected.name == "Dockerfile" else None)
            st.code(text[:60_000], language=language, line_numbers=True)


def stage_detail(db: History, row: dict, stage_key: str, states: dict[str, str], metrics: dict[str, dict]) -> None:
    index = STAGE_KEYS.index(stage_key)
    stage = STAGES[index]
    item = metrics[stage_key]
    state = states[stage_key]
    head, m1, m2 = st.columns([3, 1, 1], vertical_alignment="center")
    head.markdown(f"#### {index + 1}. {stage_label(stage_key)} <small style='color:#64748b;font-weight:400'>· {t(stage.agent)}</small>", unsafe_allow_html=True)
    m1.metric(t("Duration"), format_duration(item["seconds"]), help=t("Time the agent was actively working; the time it waited for your answer is not included."))
    m2.metric(t("Runs"), f"{item['runs']}×", help=t("How many times the stage ran (including repair and revision rounds)."))
    st.caption(f"{STATE_ICONS[state]} {t(STATE_LABELS[state])} · {stage_description(stage_key)}")
    if state in {"running", "question"} and row["status"] == "running":
        live_output(row, stage_key)
    elif stage_key == "development":
        code_view(row)
    else:
        document_view(db, row, stage_key)
    events = [event for event in row["events"] if event["stage"] == stage_key]
    if events:
        with st.expander(t("Events of this stage ({count})", count=len(events))):
            st.dataframe([{t("Time"): event["created_at"][11:19], t("Event"): event_name(event["status"]),
                           t("Detail"): translate_stored(event["detail"])} for event in reversed(events)], hide_index=True, width="stretch")


# ---------------------------------------------------------------- workspace

def selected_stage(run_id: str, states: dict[str, str]) -> tuple[str, bool]:
    picked = st.session_state.get(f"stage_{run_id}")
    return picked or active_stage(states), picked is not None


def lifecycle_svg(row: dict, selected: str | None = None) -> str:
    metrics = stage_metrics(row["events"])
    durations = {key: format_duration(value["seconds"]) for key, value in metrics.items() if not value["running"]}
    runs = {key: value["runs"] for key, value in metrics.items()}
    return render_svg(stage_states(row["events"], row["status"]), counters(row["events"]), selected, durations, runs)


def overview(run_id: str) -> None:
    """Diagram, clickable stage picker and metrics. Refreshes itself while running."""
    use_language()
    db = history()
    row = db.get(run_id)
    if row is None:
        st.info(t("Starting the run…"))
        return
    states = stage_states(row["events"], row["status"])
    loops = counters(row["events"])
    metrics = stage_metrics(row["events"])
    selected, picked = selected_stage(run_id, states)
    svg = lifecycle_svg(row, selected)
    with st.container(border=True):
        heading, actions = st.columns([3, 2], vertical_alignment="center")
        heading.markdown("#### " + t("Lifecycle"))
        with actions.container(horizontal=True, horizontal_alignment="right"):
            if st.button(t("Enlarge"), icon=":material/open_in_full:", key=f"graph_focus_{run_id}"):
                st.session_state["focus"] = {"run": run_id, "kind": "graph", "title": "Lifecycle"}
                st.rerun()
            st.download_button(t("Download SVG"), svg, file_name="lifecycle.svg", mime="image/svg+xml",
                               icon=":material/download:", key=f"graph_download_{run_id}", on_click="ignore")
        show_diagram(svg)
    choice = st.segmented_control(
        t("Stage"), STAGE_KEYS, key=f"stage_{run_id}", label_visibility="collapsed", width="stretch",
        format_func=lambda key: f"{STATE_ICONS[states[key]]} {stage_label(key)}",
    )
    done = sum(value in {"done", "skipped"} for value in states.values())
    cells = st.columns(6)
    cells[0].metric(t("Stage"), f"{done}/{len(STAGES)}")
    cells[1].metric(t("Agent time"), format_duration(sum(value["seconds"] for value in metrics.values())))
    cells[2].metric(t("Review"), t("{count} rounds", count=loops["review_rounds"]))
    cells[3].metric(t("Tests"), f"{loops['test_runs']}×")
    cells[4].metric(t("Repairs"), loops["repairs"])
    cells[5].metric(t("FR coverage"), f"{loops['coverage']}%" if loops["coverage"] is not None else "—")
    activity = describe_activity(row, states)
    {"completed": st.success, "failed": st.error, "paused": st.warning}.get(row["status"], st.info)(
        activity + ("" if picked else "  \n_" + t("Click a stage; if you do not pick one, the view follows the active stage.") + "_"))
    stale = [key for key in STAGE_KEYS if states[key] == "stale"]
    if stale and row["status"] != "running":
        stale_banner(db, row, stale)
    # When a stage is clicked, or the status or a pending question changes, the detail, chat and top bar refresh too.
    pending = db.pending_question(run_id) if row["status"] == "running" else None
    signature = (row["status"], row["pause_requested"], pending and pending["id"], run_id in WORKER_ERRORS, choice)
    key = f"signature_{run_id}"
    previous = st.session_state.get(key)
    st.session_state[key] = signature
    if previous is not None and previous != signature:
        st.rerun()


def stale_banner(db: History, row: dict, stale: list[str]) -> None:
    text, action = st.columns([4, 1.4], vertical_alignment="center")
    text.warning(t("↻ **Out of date:** {stages}. An earlier document changed; update these stages so the code and tests stay consistent with the documents.",
                   stages=", ".join(stage_label(key) for key in stale)))
    with action.popover(t("Update affected stages"), width="stretch", type="primary"):
        mode = st.radio(t("How?"), ["delta", "full"], key=f"propagate_mode_{row['id']}", format_func={
            "delta": t("Delta — keep the existing documents and code, update only the affected parts"),
            "full": t("Regenerate — rewrite these stages from scratch"),
        }.get)
        ready, key = api_key_field(f"propagate_{row['id']}")
        if st.button(t("Start"), key=f"propagate_{row['id']}", type="primary", disabled=not ready):
            try:
                propagate(db, row, stale[0], mode == "delta", key)
                st.rerun()
            except ValueError as exc:
                st.error(translate_stored(str(exc)))


def detail_panel(run_id: str) -> None:
    use_language()
    db = history()
    row = db.get(run_id)
    if row is None:
        return
    states = stage_states(row["events"], row["status"])
    stage_detail(db, row, selected_stage(run_id, states)[0], states, stage_metrics(row["events"]))


def impact_context(row: dict) -> str:
    output = Path(row["output_dir"])
    parts = [f"Architecture decisions:\n{read_text(output / 'decisions.yaml') or ''}"]
    for name, budget in (("requirements.md", 9000), ("architecture.md", 7000), ("traceability.md", 3000)):
        text = read_text(output / name)
        if text:
            parts.append(f"--- {name} ---\n{text[:budget]}")
    project_dir = Path(row["project_dir"])
    if project_dir.is_dir():
        parts.append("Project files:\n" + "\n".join(path.as_posix() for path in project_files(project_dir)))
    return "\n\n".join(parts)


@st.dialog(t("📝 Change requests"), width="large")
def changes_dialog(run_id: str) -> None:
    """New feature / bug / change: create → impact analysis → approve and apply (delta iteration)."""
    use_language()
    db = history()
    row = db.get(run_id)
    with st.form(f"new_change_{run_id}", clear_on_submit=True):
        st.markdown(t("**➕ New request**"))
        kind = st.radio(t("Type"), ["feature", "bug", "change"], format_func=lambda name: t(CHANGE_KINDS[name]), horizontal=True)
        title = st.text_input(t("Title"), placeholder=t("E.g. Add a barcode field to products / Stock can go negative"))
        description = st.text_area(t("Description"), height=90, placeholder=t(
            "What is wanted and why? For a bug: steps, expected and actual behavior."))
        if st.form_submit_button(t("Create request")):
            try:
                st.toast(t("{id} created.", id=db.create_change(run_id, kind, title, description)))
            except ValueError as exc:
                st.error(translate_stored(str(exc)))
    changes = [item for item in db.changes(run_id) if item["kind"] != "edit"]
    if not changes:
        st.caption(t("No requests yet. When you create one, the Impact Analysis Agent works out which requirements, design, files and tests "
                     "are affected; if you approve, only the affected stages are updated in delta mode."))
        return
    busy = row["status"] == "running" or db.active_change(run_id) is not None
    ready, api_key = api_key_field(f"changes_{run_id}")
    for change in reversed(changes):
        open_now = change["status"] in {"draft", "analyzed"}
        with st.expander(f"{t(CHANGE_STATUS.get(change['status'], change['status']))} · **{change['id']}** · "
                         f"{t(CHANGE_KINDS[change['kind']])} · {change['title']}", expanded=open_now):
            st.write(change["description"] or "_" + t("No description") + "_")
            if change.get("clarifications"):
                st.markdown("**" + t("Clarifications") + "**\n\n" + change["clarifications"])
            if change["impact"]:
                with st.container(height=240, border=True):
                    st.markdown(change["impact"])
            if change["status"] == "draft":
                if st.button(t("🔍 Run impact analysis"), key=f"impact_{change['id']}", disabled=not ready):
                    try:
                        with st.spinner(t("The Impact Analysis Agent is working…")):
                            report = make_engine(row, api_key).impact(row["idea"], impact_context(row), (
                                f"{change['id']} · {CHANGE_KINDS[change['kind']]}: {change['title']}\n{change['description']}"),
                                language=document_language(row))
                        start = parse_start_stage(report) or ("development" if change["kind"] == "bug" else "analysis")
                        db.update_change(run_id, change["id"], impact=report, start_stage=start, status="analyzed",
                                         skip_stages=",".join(suggested_skips(report)) or None)
                        st.rerun(scope="fragment")
                    except (RuntimeError, ValueError) as exc:
                        st.error(translate_stored(str(exc)))
            elif change["status"] == "analyzed":
                questions_asked = open_questions(change["impact"] or "")
                answers = {}
                if questions_asked:
                    st.markdown("**" + t("❓ Open questions") + "** — " + t("clarify them before applying; your answers go to all stages as firm decisions."))
                    for number, question in enumerate(questions_asked, 1):
                        answers[question] = st.text_input(f"{number}. {question}", key=f"clarify_{change['id']}_{number}")
                unanswered = [question for question, answer in answers.items() if not answer.strip()]
                options = ["analysis", "design", "development"]
                start = st.selectbox(t("Starting stage"), options, index=options.index(change["start_stage"] or "analysis"),
                                     format_func=stage_label, key=f"start_{change['id']}",
                                     help=t("This stage and the ones after it are updated in delta mode; earlier ones stay as they are."))
                skippable = [key for key in SKIPPABLE_STAGES if STAGE_KEYS.index(key) > STAGE_KEYS.index(start)]
                skipped = st.multiselect(
                    t("Stages to skip"), skippable, format_func=stage_label, key=f"skip_{change['id']}",
                    default=[key for key in (change.get("skip_stages") or "").split(",") if key in skippable],
                    help=t("The impact analysis says these stages are not affected by the change; their previous documents are kept. "
                           "Development and testing always run."))
                apply, reject = st.columns(2)
                if apply.button(t("▶ Approve and apply"), key=f"apply_{change['id']}", type="primary",
                                disabled=busy or not ready or bool(unanswered),
                                help=(t("Answer the open questions first.") if unanswered else
                                      t("Cannot start while a job is running or another request is being applied."))):
                    clarifications = "\n".join(f"- Question: {question}\n  Answer: {answer.strip()}" for question, answer in answers.items())
                    db.update_change(run_id, change["id"], status="in_progress", start_stage=start,
                                     clarifications=clarifications or None, skip_stages=",".join(skipped) or None)
                    regenerate(db, run_id, start, api_key)
                    st.rerun()
                if reject.button(t("✖ Reject"), key=f"reject_{change['id']}"):
                    db.update_change(run_id, change["id"], status="rejected")
                    st.rerun(scope="fragment")


def iteration_label(db: History, run_id: str, item: dict) -> str:
    """Version label built at display time so it follows the UI language."""
    if item["kind"] == "initial":
        return t("v{number} · Initial version", number=item["number"])
    if item["kind"] == "rerun" or not item["change_id"]:
        return t("v{number} · Regeneration", number=item["number"])
    change = db.change(run_id, item["change_id"]) or {}
    return f"v{item['number']} · {item['change_id']} · {t(CHANGE_KINDS.get(item['kind'], item['kind']))}: {change.get('title', '')}"


@st.dialog(t("🕓 Version history"), width="large")
def iterations_dialog(run_id: str) -> None:
    """Every finished iteration (first version, CR, bugfix): the diagram, metrics, documents and diff against the previous one."""
    use_language()
    db = history()
    row = db.get(run_id)
    items = db.iterations(run_id)
    if not items:
        st.caption(t("No finished iteration yet. When the first generation or a change request finishes, it appears here as a version."))
        return
    by_number = {item["number"]: item for item in items}
    number = st.selectbox(t("Version"), list(reversed(by_number)), key=f"iteration_{run_id}",
                          format_func=lambda n: f"{ITERATION_ICONS.get(by_number[n]['status'], '⚪')} {iteration_label(db, run_id, by_number[n])}"
                                                f" · {by_number[n]['created_at'][:16].replace('T', ' ')}")
    item = by_number[number]
    until = [event for event in row["events"] if event["id"] <= item["last_event_id"]]
    within = [event for event in until if event["id"] >= item["first_event_id"]]
    states = stage_states(until, item["status"])
    metrics = stage_metrics(within)
    show_diagram(render_svg(states, counters(until), None,
                            {key: format_duration(value["seconds"]) for key, value in metrics.items()},
                            {key: value["runs"] for key, value in stage_metrics(until).items()}))
    cells = st.columns(4)
    cells[0].metric(t("Result"), t("Completed") if item["status"] == "completed" else t("Failed"))
    cells[1].metric(t("Test attempts"), item["attempts"])
    cells[2].metric(t("Agent time in this version"), format_duration(sum(value["seconds"] for value in metrics.values())))
    cells[3].metric(t("Stages run in this version"), sum(value["runs"] > 0 for value in metrics.values()))
    if item["change_id"]:
        change = db.change(run_id, item["change_id"]) or {}
        with st.expander(t("Request that produced this version: {id}", id=item["change_id"]), expanded=True):
            st.write(change.get("description") or "")
            if change.get("clarifications"):
                st.markdown("**" + t("Clarifications") + "**\n\n" + change["clarifications"])
    snapshot = Path(item["snapshot_dir"])
    docs = sorted((snapshot / "docs").glob("*")) if (snapshot / "docs").is_dir() else []
    if docs:
        pick, compare = st.columns([3, 1.4], vertical_alignment="bottom")
        name = pick.selectbox(t("Documents of this version"), [doc.name for doc in docs], key=f"iteration_doc_{run_id}_{number}")
        previous = by_number.get(number - 1)
        show_diff = compare.toggle(t("Diff against previous version"), key=f"iteration_diff_{run_id}_{number}", disabled=previous is None)
        content = read_text(snapshot / "docs" / name) or ""
        with st.container(height=320, border=True):
            if show_diff and previous:
                before = read_text(Path(previous["snapshot_dir"]) / "docs" / name) or ""
                st.code(unified_diff(before, content, name) or t("(this document did not change)"), language="diff")
            elif name.endswith(".yaml"):
                st.code(content, language="yaml")
            else:
                render_markdown(content)
    if snapshot.is_dir():
        files = [path.relative_to(snapshot) for path in sorted(snapshot.rglob("*")) if path.is_file()]
        st.download_button(t("⬇️ {label} — documents + code (ZIP)", label=iteration_label(db, run_id, item)), project_zip(snapshot, files),
                           file_name=f"{run_id}-v{number}.zip", mime="application/zip", key=f"iteration_zip_{run_id}_{number}",
                           on_click="ignore")


def header(db: History, row: dict) -> None:
    run_id = row["id"]
    title, status = st.columns([8, 1.5], vertical_alignment="center")
    with title:
        rename_key = f"renaming_{run_id}"
        if st.session_state.get(rename_key):
            with st.form(f"rename_form_{run_id}", border=False):
                name = st.text_input(t("Project name"), row.get("title") or suggested_title(row["idea"]), max_chars=TITLE_LIMIT,
                                     label_visibility="collapsed",
                                     help=t("Leave it empty to show the project idea instead."))
                save, cancel = st.columns(2)
                saved = save.form_submit_button(t("Save"), type="primary", width="stretch")
                cancelled = cancel.form_submit_button(t("Cancel"), width="stretch")
            if saved or cancelled:
                if saved:
                    # An unchanged suggestion stays unset, so the title keeps following the idea.
                    db.rename(run_id, "" if name.strip() == suggested_title(row["idea"]) and not row.get("title") else name)
                st.session_state.pop(rename_key)
                st.rerun()
        else:
            st.markdown(f'<div class="project-title" role="heading" aria-level="1">{escape(project_title(row))}</div>', unsafe_allow_html=True)
            if st.button(t("Rename"), icon=":material/edit:", key=f"rename_{run_id}", help=t("Rename project"), type="tertiary"):
                st.session_state[rename_key] = True
                st.rerun()
        # The full path spreads the header over several lines; only the folder name (the full path is in the README view and Manage menu).
        iterations = db.iterations(run_id)
        version = iteration_label(db, run_id, iterations[-1]) if iterations else t("no version yet")
        st.caption(f"🏷️ **{version}** · `{run_id}` · {t('model')} `{row['model'] or '—'}` · 📁 `{Path(row['project_dir']).name}`")
    with status:
        icon, label, color = STATUS_LABELS.get(row["status"], ("⚪", row["status"], "gray"))
        st.badge(t(label), icon=icon, color=color)
    with st.container(horizontal=True):
        primary = st.container(width="content")
        changes = st.container(width="content")
        readme = st.container(width="content")
        versions = st.container(width="content")
        folder = st.container(width="content")
        downloads = st.container(width="content")
        manage = st.container(width="content")
        if changes.button(t("📝 Changes"), key=f"changes_{run_id}", width="stretch", help=t("New feature, bug or change request")):
            changes_dialog(run_id)
        readme_path = Path(row["project_dir"]) / "README.md"
        if readme.button("📘 README", key=f"readme_{run_id}", width="stretch", disabled=not readme_path.is_file()):
            open_focus(row, readme_path, "README")
        if versions.button(t("🕓 Versions ({count})", count=len(iterations)), key=f"iterations_{run_id}", width="stretch",
                           help=t("First version, every CR and bugfix: the state, documents, code and diffs at that point")):
            iterations_dialog(run_id)
        project_dir = Path(row["project_dir"])
        if folder.button(t("📂 Folder"), key=f"open_folder_{run_id}", width="stretch", disabled=not project_dir.is_dir(),
                         help=t("Open the project folder in the file manager (Explorer / Finder): {path}", path=project_dir)):
            try:
                open_folder(project_dir)
            except ValueError as exc:
                st.error(translate_stored(str(exc)))
        with downloads.popover(t("Download"), icon=":material/download:", width="stretch"):
            st.markdown("**" + t("Project outputs") + "**")
            st.caption(t("The files produced so far are downloaded. Dependencies and local .env files are not included."))
            for kind, label in [("all", t("Whole project + SDLC outputs")), ("documents", t("All documents and reports")), ("code", t("Source code"))]:
                st.download_button(label, lambda kind=kind: export_zip(Path(row["project_dir"]), Path(row["output_dir"]), kind),
                                   file_name=f"{run_id}-{kind}.zip", mime="application/zip", icon=":material/download:",
                                   key=f"bundle_{run_id}_{kind}", width="stretch", on_click="ignore")
            st.markdown("**" + t("Diagnostics") + "**")
            st.caption(t("Where and why the run broke: the error traceback, every model call, build/test logs and the event timeline. API keys and passwords are masked."))
            st.download_button(t("Failure report (ZIP)"), lambda: diagnostics_zip(db.get(run_id), db.changes(run_id), db.iterations(run_id)),
                               file_name=f"{run_id}-diagnostics.zip", mime="application/zip", icon=":material/bug_report:",
                               key=f"diagnostics_{run_id}", width="stretch", on_click="ignore",
                               type="primary" if row["status"] == "failed" else "secondary")
            if readme_path.is_file():
                st.download_button("README.md", lambda: readme_path.read_bytes(), file_name="README.md", mime="text/markdown",
                                   key=f"readme_download_{run_id}", width="stretch", on_click="ignore")
            dist = Path(row["project_dir"]) / "dist"
            executables = sorted(path for path in dist.iterdir() if path.is_file()) if dist.is_dir() else []
            if executables:
                st.markdown("**" + t("Executables") + "**")
                st.caption(t("Built for each operating system; pick yours (.exe is for Windows)."))
                for item in executables:
                    st.download_button(item.name, lambda item=item: item.read_bytes(), file_name=item.name,
                                       mime="application/octet-stream", icon=":material/terminal:",
                                       key=f"dist_{run_id}_{item.name}", width="stretch", on_click="ignore")
        if row["status"] == "running":
            if primary.button(t("⏸ Stop"), key=f"pause_{run_id}", width="stretch", disabled=bool(row["pause_requested"])):
                db.request_pause(run_id)
                st.rerun()
        elif row["status"] == "paused":
            with primary.popover(t("▶ Resume"), width="stretch"):
                ready, key = api_key_field(f"resume_{run_id}")
                if st.button(t("Resume where it stopped"), key=f"resume_{run_id}", type="primary", disabled=not ready):
                    resume_run(run_id, key)
                    st.rerun()
        elif row["status"] == "failed":
            # Failed job: reset from the first incomplete stage and continue; finished stages are not regenerated.
            failed_at = next((stage for stage in STAGE_KEYS if stage not in completed_from_events(row["events"])), STAGE_KEYS[-1])
            with primary.popover(t("↻ Retry"), width="stretch"):
                st.caption(t("Continues from the {stage} stage; completed stages are kept.", stage=stage_label(failed_at)))
                ready, key = api_key_field(f"retry_{run_id}")
                if st.button(t("Retry"), key=f"retry_{run_id}", type="primary", disabled=not ready):
                    regenerate(db, run_id, failed_at, key)
                    st.rerun()
        with manage.popover(t("⚙️ Manage"), width="stretch"):
            manage_menu(db, row)
    active = db.active_change(run_id)
    if active:
        st.info(t("🔁 **{id}** is being applied ({kind}): {title}", id=active["id"], kind=t(CHANGE_KINDS[active["kind"]]), title=active["title"]))
    if run_id in WORKER_ERRORS:
        st.error(translate_stored(WORKER_ERRORS[run_id]))


def manage_menu(db: History, row: dict) -> None:
    run_id = row["id"]
    if row["status"] == "running":
        st.info(t("Stop the run first to edit, regenerate or delete."))
        return
    edit, regen, remove = st.tabs([t("✏️ Idea"), t("🔁 Regenerate"), t("🗑️ Delete")])
    decisions_path = Path(row["output_dir"]) / "decisions.yaml"
    decisions = yaml.safe_load(decisions_path.read_text(encoding="utf-8")) if decisions_path.is_file() else {}
    with edit:
        idea = st.text_area(t("Project idea"), row["idea"], key=f"idea_{run_id}", height=90)
        constraints = st.text_area(t("Constraints"), decisions.get("constraints", ""), key=f"constraints_{run_id}", height=70)
        if st.button(t("Save"), key=f"save_idea_{run_id}"):
            try:
                if not idea.strip() or len(idea) > 1000:
                    raise ValueError(t("The project idea must be 1–1000 characters"))
                updated = validate_decisions({**decisions, "constraints": constraints})
                db.update_idea(run_id, idea.strip())
                previous = yaml.safe_dump(decisions, allow_unicode=True, sort_keys=False)
                accept_edit(db, row, "workflow", "decisions.yaml", previous,
                            yaml.safe_dump(updated, allow_unicode=True, sort_keys=False), "Idea/constraints edited",
                            stale_from="decisions")
                st.toast(t("Saved. All stages were marked 'out of date'; you can update them from the warning above."))
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(translate_stored(str(exc)))
    with regen:
        stage = st.selectbox(t("Starting stage"), STAGE_KEYS, format_func=stage_label, key=f"regen_stage_{run_id}")
        st.caption(t("The selected stage and the ones after it run again; earlier documents are used as input."))
        ready, key = api_key_field(f"regen_{run_id}")
        if st.button(t("Regenerate"), key=f"regen_{run_id}", type="primary", disabled=not ready):
            try:
                regenerate(db, run_id, stage, key)
                st.rerun()
            except ValueError as exc:
                st.error(translate_stored(str(exc)))
    with remove:
        st.caption(t("Project: `{project}`  \nReports: `{reports}`", project=row["project_dir"], reports=row["output_dir"]))
        confirmed = st.checkbox(t("I confirm the permanent deletion"), key=f"delete_confirm_{run_id}")
        if st.button(t("Delete project"), key=f"delete_{run_id}", disabled=not confirmed):
            try:
                delete_project(ROOT, run_id)
                st.session_state.pop("selected_run", None)
                st.toast(t("Project deleted."))
                st.rerun()
            except (OSError, ValueError) as exc:
                st.error(t("Could not delete: {error}", error=translate_stored(str(exc))))


def wait_for_start(run_id: str) -> None:
    use_language()
    st.info(t("Starting the run…"))
    if history().get(run_id) or run_id in WORKER_ERRORS:
        st.rerun()


def language_selector() -> None:
    """UI language of this browser session; the project's document language is chosen separately in the new-project form."""
    st.selectbox(t("Interface language"), list(LANGUAGES), format_func=LANGUAGES.get, key="ui_language")
    st.query_params["lang"] = st.session_state["ui_language"]


def projects_page() -> None:
    db = history()
    items = db.list(100)
    ids = [item["id"] for item in items]
    starting = st.session_state.get("starting_run")
    if starting in ids:
        st.session_state.pop("starting_run")
        st.session_state["selected_run"] = starting
    with st.sidebar:
        st.markdown("**" + t("Projects") + "**")
        if items:
            if st.session_state.get("selected_run") not in ids:
                st.session_state["selected_run"] = ids[0]
            labels = {item["id"]: f"{STATUS_LABELS.get(item['status'], ('⚪',))[0]} {project_title(item)[:42]}" for item in items}
            st.radio(t("Project"), ids, key="selected_run", format_func=lambda run_id: labels[run_id], label_visibility="collapsed")
    if starting and starting not in ids:
        # A short waiting screen until the worker thread creates the record.
        if starting in WORKER_ERRORS:
            st.error(translate_stored(WORKER_ERRORS.pop(starting)))
            st.session_state.pop("starting_run")
            return
        st.fragment(wait_for_start, run_every=1)(starting)
        return
    if not items:
        st.info(t("No project has been created yet. You can start from the **New project** page."))
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
        # An inline chat box (inside a container) keeps the page from scrolling to the bottom.
        with st.container():
            st.chat_input(placeholder, key=f"chat_{row['id']}")


# ---------------------------------------------------------------- new project

def apply_template(template_id: str | None) -> None:
    """Writes the selected template's values into the form fields (button callback: runs before the widgets)."""
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
        if name not in FORM_FIELDS and name != "language":
            st.session_state[f"choice_{name}"] = chosen.get(name, item["options"][0])
    st.session_state["choice_delivery"] = chosen.get("delivery", default_delivery(chosen["project_type"]))
    st.session_state["delivery_for"] = chosen["project_type"]


def template_picker() -> None:
    catalog = {item["id"]: item for item in templates()}
    pick, apply = st.columns([4, 1], vertical_alignment="bottom")
    selected = pick.selectbox(
        t("📋 Example project template"), [None, *catalog], key="template_choice",
        format_func=lambda key: t("— Pick a template to fill the form —") if key is None else catalog[key]["title"],
    )
    apply.button(t("Apply"), key="apply_template", disabled=selected is None, width="stretch",
                 on_click=apply_template, args=(selected,))


@st.dialog(t("Choose the project folder"))
def choose_folder() -> None:
    use_language()
    current = Path(st.session_state.get("browse_dir", ROOT.parent)).expanduser().resolve()
    if not current.is_dir():
        current = ROOT.parent
    st.session_state["browse_dir"] = str(current)
    st.caption(t("Parent folder: `{path}`", path=current))
    if st.button(t("⬆️ Go to parent folder"), disabled=current == current.parent):
        st.session_state["browse_dir"] = str(current.parent)
        st.rerun()
    if os.name == "nt" and current == current.parent:
        drives = [f"{letter}:/" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{letter}:/").is_dir()]
        drive = st.selectbox(t("Drive"), drives)
        if st.button(t("Go to drive")):
            st.session_state["browse_dir"] = drive
            st.rerun()
    try:
        folders = sorted((path for path in current.iterdir() if path.is_dir() and not path.name.startswith(".")), key=lambda path: path.name.casefold())
    except OSError as exc:
        st.error(t("Could not read the folder: {error}", error=exc))
        folders = []
    if folders:
        selected = st.selectbox(t("Subfolders"), folders, format_func=lambda path: path.name)
        if st.button(t("📂 Enter the selected folder")):
            st.session_state["browse_dir"] = str(selected)
            st.rerun()
    else:
        st.caption(t("There are no visible subfolders here."))
    name = st.text_input(t("Name of the new folder to create"), key="new_folder_name", placeholder="inventory-app")
    if st.button(t("Use this location"), type="primary"):
        if not name.strip() or Path(name).name != name or name in {".", ".."} or "/" in name or "\\" in name:
            st.error(t("Enter a single new folder name."))
        elif (current / name).exists():
            st.error(t("This folder already exists; enter a new name."))
        else:
            st.session_state["output_path"] = str(current / name)
            st.rerun()


def home_page() -> None:
    st.title(t("🧭 AutoSDLC — New project"))
    st.caption(t("Take an idea through every stage of the software lifecycle with eight agents. After you start, you follow it live on the **Projects** page."))
    left, right = st.columns([2.2, 1], gap="large")
    with left:
        template_picker()
        idea = st.text_area(t("What do you want to build?"), key="idea_input", height=100,
                            placeholder=t("E.g. A stock tracking app for a small business; adding products, stock movements and a low-stock alert"))
        catalog = questions()
        type_col, stack_col = st.columns([1, 2], vertical_alignment="bottom")
        project_type = type_col.selectbox(
            t("Project type"), catalog["project_type"]["options"], help=question_text("project_type"),
            key="project_type_input", format_func=lambda value: choice_label("project_type", value),
        )
        stack = stack_col.text_input(t("Language and technologies"), help=question_text("stack"),
                                     key="stack_input", placeholder=t("E.g. Node.js/Express, pnpm, plain HTML/CSS/JS"))
        decisions = {"project_type": project_type, "stack": stack}
        # The delivery follows the project type (CLI -> executables, library -> none, services -> Docker) until the user
        # picks one; changing the type again resets it to that type's default.
        # Streamlit drops a widget's key after a page switch while delivery_for stays, so a missing key resets too.
        if st.session_state.get("delivery_for") != project_type or "choice_delivery" not in st.session_state:
            st.session_state["choice_delivery"] = default_delivery(project_type)
            st.session_state["delivery_for"] = project_type
        # The selectboxes are drawn in one row, so a choice that depends on the delivery reads the delivery from
        # the previous run of the script; changing the delivery reruns the page and shows or hides it.
        shown_for = {"project_type": project_type, "delivery": st.session_state["choice_delivery"]}
        options = [(name, item) for name, item in catalog.items()
                   if name not in FORM_FIELDS and applies(item, shown_for)]
        if options:
            # The document language follows the interface language until the user picks one explicitly.
            if not st.session_state.get("language_chosen"):
                st.session_state["choice_language"] = st.session_state["ui_language"]
            for column, (name, item) in zip(st.columns(len(options), vertical_alignment="bottom"), options):
                decisions[name] = column.selectbox(
                    t(FORM_LABELS.get(name, item["question"])), item["options"], key=f"choice_{name}", help=question_text(name),
                    format_func=lambda value, key=name: choice_label(key, value),
                    on_change=(lambda: st.session_state.update(language_chosen=True)) if name == "language" else None,
                )
        decisions["constraints"] = st.text_area(question_text("constraints"), key="constraints_input", height=80,
                                                placeholder=t("E.g. Use pnpm, keep the frontend plain HTML, write the UI texts in Turkish."))
    with right:
        models = model_options()
        model = st.selectbox(t("OpenCode Go model"), models, index=models.index(default_model(models)))
        ready, api_key = api_key_field("new")
        path_col, browse_col = st.columns([3, 1], vertical_alignment="bottom")
        output_path = path_col.text_input(t("Project folder (optional)"), key="output_path", placeholder="projects/<name>-<id>")
        if browse_col.button(t("Browse"), width="stretch"):
            choose_folder()
        if st.button(t("🚀 Create the project"), type="primary", width="stretch"):
            try:
                validated = validate_decisions(decisions)
                if not idea.strip() or len(idea) > 1000:
                    raise ValueError(t("Write the project idea in 1–1000 characters."))
                if not ready:
                    raise ValueError(t("An OpenCode Go API key is required."))
                target = Path(output_path).expanduser().resolve() if output_path.strip() else None
                if target and target.exists():
                    raise ValueError(t("Project folder already exists: {path}", path=target))
                st.session_state["starting_run"] = start_run(idea.strip(), validated, target, model, api_key)
                st.switch_page(PROJECTS_PAGE)
            except (ValueError, OSError) as exc:
                st.error(translate_stored(str(exc)))
        st.caption(t("If an agent needs a decision, the question appears in the chat on the Projects page; you answer there."))
    st.markdown("##### " + t("How does the lifecycle work?"))
    show_diagram(render_svg())
    with st.expander(t("Stages and agents")):
        columns = st.columns(4)
        for index, stage in enumerate(STAGES):
            with columns[index % 4]:
                st.html(f'<div class="sdlc-card"><b>{index + 1}. {escape(stage_label(stage.key))}</b> <small>· {escape(t(stage.agent))}</small>'
                        f"<p style='margin:4px 0'>{escape(stage_description(stage.key))}</p><small>📄 {escape(t(stage.artifact))}</small></div>")
        st.caption(t("The orange arrows are feedback loops: if the Review says REVISE, the Design Agent rewrites the document; "
                     "if the tests fail, the Development Agent repairs the code. The tests belong to the Test Agent; the developer cannot change them."))


HOME_PAGE = st.Page(home_page, title=t("New project"), icon="🏠", default=True)
PROJECTS_PAGE = st.Page(projects_page, title=t("Projects"), icon="📁")
navigation = st.navigation([HOME_PAGE, PROJECTS_PAGE])
with st.sidebar:
    language_selector()
# After the pages are registered: a login screen shown earlier makes Streamlit forget the URL's page and land on the home page.
require_login()
navigation.run()
