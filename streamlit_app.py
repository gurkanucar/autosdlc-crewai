"""Simple presenter-facing UI. Run with: streamlit run streamlit_app.py"""

from pathlib import Path

import streamlit as st

from autosdlc.intake import questions
from autosdlc.runner import run_project
from autosdlc.storage import History


ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="AutoSDLC", layout="wide")
st.title("AutoSDLC")
st.caption("Planning → Analysis → Design → Development → Test → Deployment → Maintenance")

idea = st.text_area(
    "Project idea",
    value="A task tracking API with create, list, update and delete operations",
    height=90,
)
model = st.text_input("OpenCode Go model", value="glm-5.3-flash")
output_path = st.text_input("Project output directory (optional)", placeholder="D:\\projects\\my-new-app")
api_key = st.text_input(
    "OpenCode Go API key (or set OPENCODE_API_KEY)", type="password"
) or None
st.subheader("Architecture choices")
catalog = questions()
project_type = st.selectbox(catalog["project_type"]["question"], catalog["project_type"]["options"])
decisions = {"project_type": project_type, "stack": st.text_input(catalog["stack"]["question"], value="Python / FastAPI")}
for name, item in catalog.items():
    if name in {"project_type", "stack"}:
        continue
    if item.get("when") and project_type not in item["when"]:
        continue
    decisions[name] = st.selectbox(item["question"], item["options"], key=f"choice_{name}")

if st.button("Run SDLC", type="primary"):
    try:
        with st.spinner("Running SDLC stages and API/UI tests..."):
            result = run_project(
                idea,
                workspace=ROOT,
                model=model,
                api_key=api_key,
                decisions=decisions,
                output=Path(output_path) if output_path.strip() else None,
            )
        st.session_state["last_run_id"] = result.id
        if result.status == "completed":
            st.success(f"Completed in {result.attempts} test attempt(s)")
        else:
            st.error(result.error or "Tests did not pass")
    except (RuntimeError, ValueError) as exc:
        st.error(str(exc))

history = History(ROOT / "runs" / "history.sqlite3")
items = history.list(20)
if items:
    st.subheader("Run history")
    st.dataframe(
        [{key: row[key] for key in ("id", "provider", "status", "attempts", "created_at")}
         for row in items],
        hide_index=True,
    )
    ids = [row["id"] for row in items]
    selected = st.selectbox(
        "Inspect run", ids,
        index=ids.index(st.session_state["last_run_id"])
        if st.session_state.get("last_run_id") in ids else 0,
    )
    row = history.get(selected)
    st.write(f"**Idea:** {row['idea']}")
    st.write(f"**Artifacts:** `{row['output_dir']}`")
    st.write(f"**Project:** `{row['project_dir'] or Path(row['output_dir']) / 'project'}`")
    st.subheader("Stage events")
    st.dataframe(row["events"], hide_index=True)
    output = Path(row["output_dir"])
    for name in (
        "decisions.yaml", "planning.md", "requirements.md", "architecture.md", "test_report.md",
        "deployment.md", "maintenance_report.md",
    ):
        artifact = output / name
        if artifact.exists():
            with st.expander(name):
                st.markdown(artifact.read_text(encoding="utf-8"))

