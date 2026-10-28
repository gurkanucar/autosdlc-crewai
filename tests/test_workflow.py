import json
import re
import json as json_module
from pathlib import Path

import pytest

from autosdlc.crewai_engine import ClarificationNeeded, CrewAIEngine
from autosdlc.models import CHAT_MODELS, available_models
from autosdlc.project_management import delete_project
from autosdlc.files import write_generated_files
from autosdlc.runner import RunPaused, run_project
from autosdlc.intake import validate_decisions
from autosdlc.deployment import render_compose
from autosdlc.storage import History
from autosdlc.cli import main as cli_main
from autosdlc.runner import RunResult
from autosdlc.crewai_engine import _parse_file_bundle
from autosdlc.traceability import build_matrix, requirement_ids
from autosdlc.lifecycle import stage_states


class StubEngine:
    """Local test double for the workflow; never used by the application UI or CLI."""

    def document(self, stage: str, idea: str, context: str) -> str:
        if stage == "analysis":
            return "# Requirements\n\nFR-01: provide an HTTP response.\nFR-02: list items.\nNFR-01: respond quickly.\n"
        if stage == "design":
            return "# Design\n\n```mermaid\nflowchart LR\n A --> B\n```\n\n```mermaid\nsequenceDiagram\n A->>B: Request\n```\n\n```mermaid\nerDiagram\n TASK { int id }\n```\n"
        if stage == "review":
            return "# Review\n\n| 1 | Low | Name | Suggestion |\n\nVERDICT: APPROVE\n"
        return f"# {stage}\n\n{idea}\n"

    def code(self, idea: str, docs: str, feedback: str | None = None) -> dict[str, str]:
        answer = 1 if feedback else 0
        return {
            "app/__init__.py": "",
            "app/main.py": f"from fastapi import FastAPI\napp = FastAPI()\n@app.get('/')\ndef root():\n    return {{'answer': {answer}}}\n",
            "project.yaml": "port: 8000\napi_test: [python, -m, pytest, -q, tests]\n",
            "Dockerfile": "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nCMD [\"python\", \"-m\", \"http.server\", \"8000\"]\n",
            "requirements.txt": "fastapi\nuvicorn\n",
        }

    def tests(self, idea: str, docs: str, files: dict[str, str]) -> dict[str, str]:
        return {"tests/test_api.py": "from fastapi.testclient import TestClient\nfrom app.main import app\n\ndef test_fr01_http_response():\n    assert TestClient(app).get('/').json()['answer'] == 1\n"}


BACKEND = {"project_type": "backend", "stack": "Python / FastAPI", "database": "sqlite", "cache": "none", "observability": "none", "ui": "none"}


def test_flow_creates_artifacts_and_repairs_failed_test(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    events = []
    result = run_project("Task tracking API", workspace=tmp_path, engine=StubEngine(), decisions=BACKEND, on_event=lambda stage, status, detail: events.append((stage, status)))

    assert result.status == "completed", result.error
    assert result.attempts == 2
    for name in (
        "planning.md", "requirements.md", "architecture.md", "design_review.md", "traceability.md", "test_report.md",
        "deployment.md", "maintenance_report.md", "decisions.yaml",
    ):
        assert (result.output_dir / name).is_file(), name
    for name in (
        "app/main.py", "tests/test_api.py", "Dockerfile", "compose.yaml",
        "docs/architecture.md", "README.md",
    ):
        assert (result.project_dir / name).is_file(), name
    assert result.project_dir.parent == tmp_path / "projects"
    assert result.project_dir.name.startswith("task-tracking-api-")
    stored = History(tmp_path / "runs" / "history.sqlite3").get(result.id)
    assert stored["status"] == "completed"
    assert stored["provider"] == "opencode-go"
    assert any(e["stage"] == "testing" and e["status"] == "failed" for e in stored["events"])
    assert any(e["stage"] == "development" and e["status"] == "repair_completed" for e in stored["events"])
    assert any(e["stage"] == "testing" and e["status"] == "passed" for e in stored["events"])
    assert ("planning", "started") in events
    assert ("maintenance", "completed") in events


def test_generated_file_path_cannot_escape_project(tmp_path: Path):
    with pytest.raises(ValueError):
        write_generated_files(tmp_path, {"../outside.py": "print('bad')"})
    with pytest.raises(ValueError):
        write_generated_files(tmp_path, {"app/C:/outside.py": "print('bad')"})
    assert not (tmp_path.parent / "outside.py").exists()


def test_yaml_config_and_opencode_request_header(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "OK"}}]}

    def fake_post(url, *, headers, json, timeout):
        seen.update(url=url, headers=headers, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", fake_post)
    engine = CrewAIEngine(api_key="test-key", model="glm-5.3-flash")
    assert engine.agents["analysis"]["role"] == "Analysis Agent"
    assert "acceptance criteria" in engine.tasks["analysis"]["description"]
    assert len(engine.agents) == 9 and engine.agents["impact"]["role"] == "Impact Analysis Agent"
    assert engine.agents["review"]["role"] == "Architecture Review Agent"
    assert engine.llm.call("hello") == "OK"
    assert seen["url"].endswith("/openai/v1/chat/completions")
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["headers"]["x-opencode-session"].startswith("ses_")
    assert seen["payload"]["model"] == "glm-5.3-flash"


def test_planning_agent_reads_yaml_task_and_calls_gateway(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    payloads = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "# Plan\n\nScope: one CRUD API."}}]}

    def fake_post(url, *, headers, json, timeout):
        payloads.append(json)
        return Response()

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", fake_post)
    engine = CrewAIEngine(api_key="test-key")
    result = engine.document("planning", "Book tracker", "")
    assert "Scope" in result
    assert payloads
    assert "Book tracker" in str(payloads[0]["messages"])


def test_complete_crewai_flow_with_mock_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    responses = iter([
        "# Plan\nBuild a book tracker.",
        "# Requirements\nFR-01: Create and list books.\nNFR-01: Fast.",
        "# Design\n```mermaid\nflowchart LR\nA-->B\n```\n```mermaid\nsequenceDiagram\nA->>B: Request\n```\n```mermaid\nerDiagram\nBOOK { int id }\n```",
        "# Review\nNo findings.\nVERDICT: APPROVE",
        # Development: first the file plan, then groups of four files.
        json.dumps({"plan": [{"path": path, "purpose": "x"} for path in StubEngine().code("", "", feedback="fix")]}),
        json.dumps({"files": dict(list(StubEngine().code("", "", feedback="fix").items())[:4])}),
        json.dumps({"files": dict(list(StubEngine().code("", "", feedback="fix").items())[4:])}),
        # Testing: first the test file plan, then the files.
        json.dumps({"plan": [{"path": "tests/test_api.py", "purpose": "FR-01"}]}),
        json.dumps({"files": {
            "tests/test_api.py": "from fastapi.testclient import TestClient\nfrom app.main import app\n\ndef test_api():\n    assert TestClient(app).get('/').json()['answer'] == 1\n",
        }}),
        "# Deployment\nBuild the container locally after tests pass.",
        "# Maintenance\nReview dependencies and add backups.",
    ])

    class Response:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": self.content}}]}

    def fake_post(url, *, headers, json, timeout):
        return Response(next(responses))

    streamed = []

    class StreamResponse:
        """OpenAI-compatible SSE stream: sends the response in several chunks."""
        headers = {"content-type": "text/event-stream"}
        status_code = 200

        def __init__(self, content):
            self.content = content

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            for start in range(0, len(self.content), 40):
                yield "data: " + json_module.dumps({"choices": [{"delta": {"content": self.content[start:start + 40]}}]})
                yield ""
            yield "data: [DONE]"

    def fake_stream(method, url, *, headers, json, timeout):
        assert json["stream"] is True
        streamed.append(url)
        return StreamResponse(next(responses))

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", fake_post)
    monkeypatch.setattr("autosdlc.crewai_engine.httpx.stream", fake_stream)
    engine = CrewAIEngine(api_key="test-key")
    result = run_project("Book tracker", workspace=tmp_path, engine=engine, decisions=BACKEND)
    assert result.status == "completed", result.error
    assert result.attempts == 1
    assert "PASSED" in (result.output_dir / "test_report.md").read_text(encoding="utf-8")
    # Agent responses were received as a stream and written to the live-view files.
    assert len(streamed) == 11  # 6 document stages + the development plan and two file groups + the test plan and files
    assert "book tracker" in (result.output_dir / "live" / "planning.md").read_text(encoding="utf-8").lower()


def test_architecture_choices_and_compose():
    with pytest.raises(ValueError, match="required"):
        validate_decisions({"project_type": "backend", "stack": "Go/Gin"})
    cli = validate_decisions({"project_type": "cli", "stack": "Rust"})
    assert cli["database"] == "none"
    assert cli["ui"] == "none"
    fullstack = validate_decisions({"project_type": "fullstack", "stack": "Node/Next.js", "database": "postgres", "cache": "redis", "observability": "openobserve"})
    assert fullstack["ui"] == "web"
    compose = render_compose(fullstack, 3000)
    assert compose["services"]["app"]["ports"] == ["3000:3000"]
    assert {"db", "redis", "otel-collector", "openobserve"} <= set(compose["services"])
    # Uploaded files survive a container restart: a Postgres app still gets its own data volume.
    assert compose["services"]["app"]["environment"]["UPLOADS_DIR"] == "/data/uploads"
    assert "app-data:/data" in compose["services"]["app"]["volumes"] and "app-data" in compose["volumes"]
    sqlite = render_compose(validate_decisions({"project_type": "backend", "stack": "Go", "database": "sqlite", "cache": "none",
                                                "observability": "none", "ui": "none"}))
    assert sqlite["services"]["app"]["volumes"] == ["app-data:/data"]
    assert sqlite["services"]["app"]["environment"]["UPLOADS_DIR"] == "/data/uploads"


def test_custom_output_path_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    destination = tmp_path / "my-project"
    result = run_project("An API", workspace=tmp_path, output=destination, decisions=BACKEND, engine=StubEngine())
    assert result.status == "completed", result.error
    assert result.project_dir == destination
    assert (destination / "README.md").is_file()
    assert History(tmp_path / "runs" / "history.sqlite3").get(result.id)["project_dir"] == str(destination)


def test_yaml_request_passes_stack_and_output_to_runner(tmp_path, monkeypatch):
    request = tmp_path / "request.yaml"
    destination = tmp_path / "chosen-project"
    request.write_text(
        f"idea: A command-line calculator\noutput: '{destination.as_posix()}'\n"
        "decisions:\n  project_type: cli\n  stack: Rust\n",
        encoding="utf-8",
    )
    captured = {}

    def fake_run(idea, **kwargs):
        captured.update(idea=idea, **kwargs)
        return RunResult("run123", "completed", tmp_path / "runs/run123", destination, 1)

    monkeypatch.setattr("autosdlc.cli.run_project", fake_run)
    assert cli_main(["run", "--config", str(request), "--no-prompt"]) == 0
    assert captured["idea"] == "A command-line calculator"
    assert captured["decisions"]["stack"] == "Rust"
    assert Path(captured["output"]) == destination


def test_plain_make_run_prompts_for_idea_and_decisions(tmp_path, monkeypatch):
    answers = iter(["A tiny CLI", "cli", "Rust", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    captured = {}

    def fake_run(idea, **kwargs):
        captured.update(idea=idea, **kwargs)
        return RunResult("run123", "completed", tmp_path / "runs/run123", tmp_path / "projects/a-tiny-cli-run123", 1)

    monkeypatch.setattr("autosdlc.cli.run_project", fake_run)
    assert cli_main(["run"]) == 0
    assert captured["idea"] == "A tiny CLI"
    assert captured["decisions"]["project_type"] == "cli"
    assert captured["decisions"]["stack"] == "Rust"
    assert captured["decisions"]["scale"] == "normal"
    # A blank delivery answer takes the project type's default, not Docker.
    assert captured["decisions"]["delivery"] == "binary"
    # Executables are asked for a target system too; a blank answer builds for Windows only.
    assert captured["decisions"]["platforms"] == "windows"


def test_ui_choice_runs_playwright_against_live_app(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    class UiEngine(StubEngine):
        def code(self, idea, docs, feedback=None):
            return {
                "app/__init__.py": "",
                "app/main.py": (
                    "from fastapi import FastAPI\n"
                    "from fastapi.responses import HTMLResponse\n"
                    "app = FastAPI()\n"
                    "@app.get('/')\n"
                    "def home():\n    return HTMLResponse('<h1>Hello UI</h1>')\n"
                ),
                "project.yaml": (
                    "port: 18751\n"
                    "api_test: [python, -m, pytest, -q, tests/test_api.py]\n"
                    "ui_test: [python, -m, pytest, -q, -p, pytest_playwright.pytest_playwright, tests/test_ui.py]\n"
                    "start: [python, -m, uvicorn, 'app.main:app', --host, '127.0.0.1', --port, '18751']\n"
                ),
                "Dockerfile": "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nCMD [\"python\", \"-m\", \"http.server\", \"18751\"]\n",
            }

        def tests(self, idea, docs, files):
            return {
                "tests/test_api.py": "from fastapi.testclient import TestClient\nfrom app.main import app\n\ndef test_http():\n    assert TestClient(app).get('/').status_code == 200\n",
                "tests/test_ui.py": "import os\n\ndef test_visible_heading(page):\n    page.goto(os.environ['BASE_URL'])\n    assert page.get_by_text('Hello UI').is_visible()\n",
            }

    choices = {**BACKEND, "ui": "web"}
    result = run_project("A small UI", workspace=tmp_path, decisions=choices, engine=UiEngine(), max_repairs=0)
    assert result.status == "completed", result.error
    report = (result.output_dir / "test_report.md").read_text(encoding="utf-8")
    assert "API tests" in report and "UI tests" in report


def test_stage_question_waits_for_answer_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    class AskingEngine(StubEngine):
        def document(self, stage, idea, context):
            if stage == "planning" and "Answer: small business" not in context:
                raise ClarificationNeeded("Who is the target user?")
            return super().document(stage, idea, context)

    def answer(run_id, stage, prompt):
        db = History(tmp_path / "runs" / "history.sqlite3")
        question_id = db.ask(run_id, stage, prompt)
        assert db.pending_question(run_id)["id"] == question_id
        assert db.answer(run_id, question_id, "small business")
        assert not db.answer(run_id, question_id, "second answer")
        return db.question(question_id)["answer"]

    result = run_project(
        "Stock API", workspace=tmp_path, engine=AskingEngine(),
        decisions=BACKEND, on_question=answer,
    )
    assert result.status == "completed", result.error
    row = History(tmp_path / "runs" / "history.sqlite3").get(result.id)
    assert row["questions"][0]["prompt"] == "Who is the target user?"
    assert row["questions"][0]["answer"] == "small business"
    assert "small business" in (result.output_dir / "clarifications.yaml").read_text(encoding="utf-8")
    assert [event["status"] for event in row["events"] if event["stage"] == "planning"] == [
        "started", "question", "answered", "completed",
    ]


def test_model_picker_filters_to_supported_chat_models(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "glm-5.3-flash"}, {"id": "gpt-6-luna"}]}

    monkeypatch.setattr("autosdlc.models.httpx.get", lambda *args, **kwargs: Response())
    assert available_models() == ["glm-5.3-flash"]
    assert "gpt-6-luna" not in CHAT_MODELS


@pytest.mark.parametrize("token", ["USER_QUESTION", "KULLANICI_SORUSU"])  # the second is the legacy Turkish token
def test_agent_question_marker_is_detected(monkeypatch, token):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": f"{token}: Who is the target user?"}}]}

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", lambda *args, **kwargs: Response())
    engine = CrewAIEngine(api_key="test-key")
    with pytest.raises(ClarificationNeeded, match="Who is the target user"):
        engine.document("planning", "Stock API", "")


def test_pause_resume_and_delete_project(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    run_id = "pause1234567"
    db = History(tmp_path / "runs" / "history.sqlite3")

    def request_pause(stage, status, _detail):
        if stage == "analysis" and status == "completed":
            assert db.request_pause(run_id)

    paused = run_project(
        "Stock management", workspace=tmp_path, engine=StubEngine(),
        decisions=BACKEND, run_id=run_id, on_event=request_pause,
    )
    assert paused.status == "paused"
    assert paused.project_dir.parent == tmp_path / "projects"
    assert (paused.output_dir / "requirements.md").is_file()
    assert not (paused.output_dir / "architecture.md").exists()
    assert db.get(run_id)["status"] == "paused"

    resumed = run_project("", workspace=tmp_path, engine=StubEngine(), resume_run_id=run_id)
    assert resumed.status == "completed", resumed.error
    assert resumed.project_dir == paused.project_dir
    row = db.get(run_id)
    assert sum(event["stage"] == "planning" and event["status"] == "started" for event in row["events"]) == 1
    assert sum(event["stage"] == "analysis" and event["status"] == "started" for event in row["events"]) == 1

    delete_project(tmp_path, run_id)
    assert db.get(run_id) is None
    assert not resumed.project_dir.exists()
    assert not resumed.output_dir.exists()


def test_delete_rejects_workspace_itself(tmp_path):
    db = History(tmp_path / "runs" / "history.sqlite3")
    run_id = "unsafe123456"
    reports = tmp_path / "runs" / run_id
    reports.mkdir()
    db.create(run_id, "Unsafe", reports, tmp_path)
    db.finish(run_id, "failed", 0, None)
    with pytest.raises(ValueError, match="safe deletion"):
        delete_project(tmp_path, run_id)
    assert tmp_path.is_dir()
    assert db.get(run_id) is not None


def test_pause_while_question_waits_then_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    run_id = "questionpause"
    db = History(tmp_path / "runs" / "history.sqlite3")

    class AskingEngine(StubEngine):
        def document(self, stage, idea, context):
            if stage == "planning" and "Answer: business owner" not in context:
                raise ClarificationNeeded("Who is the target user?")
            return super().document(stage, idea, context)

    def stop_at_question(current_id, stage, prompt):
        db.ask(current_id, stage, prompt)
        db.request_pause(current_id)
        raise RunPaused()

    paused = run_project(
        "Stock API", workspace=tmp_path, engine=AskingEngine(), decisions=BACKEND,
        run_id=run_id, on_question=stop_at_question,
    )
    assert paused.status == "paused"
    pending = db.pending_question(run_id)
    assert pending["prompt"] == "Who is the target user?"

    def reply(current_id, stage, prompt):
        assert db.ask(current_id, stage, prompt) == pending["id"]
        assert db.answer(current_id, pending["id"], "business owner")
        return db.question(pending["id"])["answer"]

    resumed = run_project(
        "", workspace=tmp_path, engine=AskingEngine(),
        resume_run_id=run_id, on_question=reply,
    )
    assert resumed.status == "completed", resumed.error
    assert db.pending_question(run_id) is None


def test_compose_without_database_has_no_postgres():
    for decisions in (
        validate_decisions({"project_type": "cli", "stack": "Rust"}),
        validate_decisions({**BACKEND, "database": "none"}),
    ):
        compose = render_compose(decisions)
        assert "db" not in compose["services"]
        assert "DATABASE_URL" not in compose["services"]["app"].get("environment", {})


def test_decisions_not_applicable_to_project_type_are_dropped():
    cli = validate_decisions({"project_type": "cli", "stack": "Go", "database": "postgres", "cache": "redis"})
    assert cli["database"] == "none" and cli["cache"] == "none"


def test_common_dotfiles_allowed_but_secrets_rejected(tmp_path: Path):
    write_generated_files(tmp_path, {".gitignore": "node_modules\n", ".github/workflows/ci.yml": "on: push\n", ".env.example": "PORT=3000\n"})
    assert (tmp_path / ".github/workflows/ci.yml").is_file()
    for name in (".env", ".env.local", ".git/config", "app/.venv/x"):
        with pytest.raises(ValueError):
            write_generated_files(tmp_path, {name: "x"})


def test_file_bundle_tolerates_prose_around_json():
    raw = 'Files below:\n{"files": {"main.go": "package main"}}\nGood luck.'
    assert _parse_file_bundle(raw) == {"main.go": "package main"}


def test_traceability_matrix_recognises_test_name_formats():
    assert requirement_ids("FR-1, NFR-02 ve fr03") == ["FR-01", "NFR-02", "FR-03"]
    matrix, coverage = build_matrix(
        "FR-01 add\nFR-02 list\nNFR-01 speed",
        {"tests/test_api.py": "def test_fr01_create(): ...\ndef test_FR_02_list(): ..."},
    )
    assert coverage == {"FR": 1.0, "NFR": 0.0}
    assert "| NFR-01 | — | No test |" in matrix


def test_repair_cannot_overwrite_tests_and_traceability_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    class CheatingEngine(StubEngine):
        def code(self, idea, docs, feedback=None):
            if feedback:
                return {"tests/test_api.py": "def test_fr01_cheat():\n    assert True\n"}
            return super().code(idea, docs)

    result = run_project("Cheat API", workspace=tmp_path, engine=CheatingEngine(), decisions=BACKEND, max_repairs=1)
    assert result.status == "failed"
    assert "TestClient" in (result.project_dir / "tests/test_api.py").read_text(encoding="utf-8")
    events = History(tmp_path / "runs" / "history.sqlite3").get(result.id)["events"]
    assert any(e["status"] == "tests_protected" for e in events)
    matrix = (result.project_dir / "docs" / "traceability.md").read_text(encoding="utf-8")
    assert "| FR-01 | `tests/test_api.py` | Covered |" in matrix
    assert "| FR-02 | — | No test |" in matrix


def test_quality_gate_asks_agent_to_rewrite_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    calls = []

    class SloppyEngine(StubEngine):
        def document(self, stage, idea, context):
            if stage == "analysis":
                calls.append(context)
                if len(calls) == 1:
                    return "# Requirements\nFR-01: liste."
            return super().document(stage, idea, context)

    result = run_project("Gate API", workspace=tmp_path, engine=SloppyEngine(), decisions=BACKEND)
    assert result.status == "completed", result.error
    assert len(calls) == 2 and "FAILED THE QUALITY GATE" in calls[1]


def test_late_stages_see_answers_and_test_result_despite_long_documents(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    contexts = {}

    class VerboseEngine(StubEngine):
        def document(self, stage, idea, context):
            contexts[stage] = context
            if stage == "planning" and "Answer: students" not in context:
                raise ClarificationNeeded("Who is the target user?")
            return super().document(stage, idea, context) + "x" * 50_000

    result = run_project(
        "Long API", workspace=tmp_path, engine=VerboseEngine(), decisions=BACKEND,
        on_question=lambda *_args: "students",
    )
    assert result.status == "completed", result.error
    head = contexts["maintenance"][:12000]
    assert "Answer: students" in head and "Actual test result: PASSED" in head



def test_review_requests_revision_then_approves(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    seen = []

    class StrictReviewer(StubEngine):
        def document(self, stage, idea, context):
            if stage == "review":
                seen.append("review")
                return "VERDICT: APPROVE" if "REVISED" in context else "| 1 | Critical | FR-02 is missing in the design | Add |\nVERDICT: REVISE"
            if stage == "design" and "ARCHITECTURE REVIEW FINDINGS" in context:
                seen.append("revision")
                return super().document(stage, idea, context) + "\nREVISED: FR-02 added\n"
            return super().document(stage, idea, context)

    result = run_project("Review API", workspace=tmp_path, engine=StrictReviewer(), decisions=BACKEND)
    assert result.status == "completed", result.error
    assert seen == ["review", "revision", "review"]
    assert "REVISED" in (result.project_dir / "docs" / "architecture.md").read_text(encoding="utf-8")
    report = (result.output_dir / "design_review.md").read_text(encoding="utf-8")
    assert "APPROVED" in report and "2 rounds" in report


def test_regenerate_from_stage_keeps_earlier_work(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    calls = []

    class CountingEngine(StubEngine):
        def document(self, stage, idea, context):
            calls.append(stage)
            return super().document(stage, idea, context)

    first = run_project("Regen API", workspace=tmp_path, engine=CountingEngine(), decisions=BACKEND)
    assert first.status == "completed", first.error
    db = History(tmp_path / "runs" / "history.sqlite3")
    assert db.reset_from(first.id, "design")
    calls.clear()
    again = run_project("", workspace=tmp_path, engine=CountingEngine(), resume_run_id=first.id)
    assert again.status == "completed", again.error
    assert calls == ["design", "review", "deployment", "maintenance"]


def test_stale_running_run_is_recovered_and_deletable(tmp_path):
    db = History(tmp_path / "runs" / "history.sqlite3")
    reports = tmp_path / "runs" / "stale1234567"
    project = tmp_path / "projects" / "stale"
    reports.mkdir(parents=True)
    (project / ".git").mkdir(parents=True)
    readonly = project / ".git" / "object"
    readonly.write_text("x", encoding="utf-8")
    readonly.chmod(0o444)
    db.create("stale1234567", "Left half-way", reports, project)
    with db._connect() as conn:
        conn.execute("UPDATE runs SET heartbeat_at='2000-01-01T00:00:00+00:00', pause_requested=1")
    assert db.recover_stale() == ["stale1234567"]
    row = db.get("stale1234567")
    assert row["status"] == "paused" and row["events"][-1]["status"] == "interrupted"
    delete_project(tmp_path, "stale1234567")
    assert not project.exists() and db.get("stale1234567") is None


def test_lifecycle_states_and_diagram():
    from autosdlc.lifecycle import counters, render_svg, stage_states

    events = [
        {"stage": "planning", "status": "completed", "detail": ""},
        {"stage": "design", "status": "completed", "detail": ""},
        {"stage": "review", "status": "changes_requested", "detail": "tur 1"},
        {"stage": "design", "status": "revision_started", "detail": ""},
        {"stage": "design", "status": "revision_completed", "detail": ""},
        {"stage": "testing", "status": "traceability", "detail": "FR coverage 75%"},
        {"stage": "workflow", "status": "reset", "detail": "design"},
    ]
    states = stage_states(events, "paused")
    assert states["planning"] == "done" and states["design"] == "pending" and states["review"] == "pending"
    loops = counters(events)
    # Regeneration does not erase history: once a loop-back happened the counter stays.
    assert loops["coverage"] == "75" and loops["revisions"] == 1
    svg = render_svg(stage_states(events[:5], "running"), counters(events[:5]), runs={"design": 2, "review": 1})
    assert svg.startswith("<svg") and "↺ revision ×1" in svg and "sdlc-halo" in svg
    assert svg.count(">×2<") == 1  # ×2 badge on the Design node; no badge on Review, which ran once.
    assert "repair loop" in svg  # If no repair ever happened the loop stays faded and has no counter.


def test_stale_marking_after_document_edit():
    from autosdlc.lifecycle import stage_states

    events = [{"stage": key, "status": "completed", "detail": ""} for key in ("planning", "analysis", "design", "review")]
    events.append({"stage": "workflow", "status": "stale", "detail": "analysis"})
    states = stage_states(events, "completed")
    assert states["analysis"] == "done" and states["design"] == "stale" and states["review"] == "stale"
    events.append({"stage": "design", "status": "completed", "detail": ""})
    assert stage_states(events, "completed")["design"] == "done"
    events.append({"stage": "workflow", "status": "stale", "detail": "decisions"})
    assert stage_states(events, "completed")["planning"] == "stale"



def test_streamlit_create_button_starts_a_run(monkeypatch):
    """Runs the real UI and presses "Create the project"; run_project must receive valid arguments."""
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr("autosdlc.runner.run_project", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("autosdlc.models.available_models", lambda: ["glm-5.3-flash"])
    app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "streamlit_app.py"), default_timeout=30).run()
    assert not app.exception
    app.text_area[0].set_value("Simple inventory tracking app")
    app.text_input[0].set_value("Node.js, pnpm, plain HTML")
    next(button for button in app.button if "Create the project" in button.label).click()
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    assert not app.error, [e.value for e in app.error]
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.1)
    assert calls and calls[0]["idea"] == "Simple inventory tracking app"
    assert calls[0]["run_id"] and calls[0]["decisions"]["stack"] == "Node.js, pnpm, plain HTML"


def test_templates_are_valid_decisions():
    from autosdlc.intake import templates

    items = templates()
    assert len({item["id"] for item in items}) == len(items) >= 4
    for item in items:
        assert 0 < len(item["idea"].strip()) <= 1000, item["id"]
        validate_decisions(item["decisions"])
    todo = next(item for item in items if item["id"] == "todo-web-node")["decisions"]
    assert todo["project_type"] == "fullstack" and "pnpm" in todo["stack"] and todo["delivery"] == "docker"


def test_streamlit_template_fills_the_form(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr("autosdlc.models.available_models", lambda: ["glm-5.3-flash"])
    app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "streamlit_app.py"), default_timeout=30).run()
    assert not app.exception and app.button(key="apply_template").disabled
    app.selectbox(key="template_choice").select("todo-web-node").run()
    app.button(key="apply_template").click().run()
    assert not app.exception, [e.value for e in app.exception]
    assert "to-do list" in app.text_area(key="idea_input").value
    assert app.selectbox(key="project_type_input").value == "fullstack"
    assert "pnpm" in app.text_input(key="stack_input").value
    assert app.selectbox(key="choice_database").value == "sqlite"
    assert app.selectbox(key="choice_delivery").value == "docker"
    assert "plain HTML" in app.text_area(key="constraints_input").value
    app.selectbox(key="template_choice").select("task-api").run()
    app.button(key="apply_template").click().run()
    assert app.selectbox(key="project_type_input").value == "backend"
    assert app.selectbox(key="choice_ui").value == "none"
    # Leaving the page drops the widget keys while delivery_for stays; coming back must not crash.
    back = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "streamlit_app.py"), default_timeout=30)
    back.session_state["delivery_for"] = "backend"
    back.run()
    assert not back.exception, [e.value for e in back.exception]
    assert back.selectbox(key="choice_delivery").value == "docker"


def test_stage_metrics_count_runs_and_exclude_waiting_time():
    from autosdlc.lifecycle import format_duration, stage_metrics

    def at(seconds):
        return f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}+00:00"

    events = [
        {"stage": "planning", "status": "started", "detail": "", "created_at": at(0)},
        {"stage": "planning", "status": "question", "detail": "?", "created_at": at(10)},
        {"stage": "planning", "status": "answered", "detail": "", "created_at": at(300)},
        {"stage": "planning", "status": "completed", "detail": "", "created_at": at(320)},
        {"stage": "testing", "status": "started", "detail": "", "created_at": at(400)},
        {"stage": "testing", "status": "failed", "detail": "attempt 1", "created_at": at(430)},
        {"stage": "development", "status": "repair_started", "detail": "", "created_at": at(431)},
        {"stage": "development", "status": "repair_completed", "detail": "", "created_at": at(491)},
        {"stage": "testing", "status": "passed", "detail": "attempt 2", "created_at": at(511)},
    ]
    metrics = stage_metrics(events)
    assert metrics["planning"]["seconds"] == 30 and metrics["planning"]["runs"] == 1
    assert metrics["testing"]["runs"] == 2 and metrics["testing"]["seconds"] == 50
    assert metrics["development"]["runs"] == 1 and metrics["development"]["seconds"] == 60
    assert format_duration(30) == "30s" and format_duration(125) == "2m 05s"


def test_user_notes_reach_later_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    contexts = {}
    run_id = "notes1234567"

    class NotingEngine(StubEngine):
        def document(self, stage, idea, context):
            contexts[stage] = context
            if stage == "planning":
                History(tmp_path / "runs" / "history.sqlite3").add_note(run_id, "The UI should use a dark theme")
            return super().document(stage, idea, context)

    result = run_project("Not API", workspace=tmp_path, engine=NotingEngine(), decisions=BACKEND, run_id=run_id)
    assert result.status == "completed", result.error
    assert "dark theme" not in contexts["planning"]
    assert "dark theme" in contexts["analysis"] and "dark theme" in contexts["maintenance"]


def _app(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("AUTOSDLC_WORKSPACE", str(tmp_path))
    monkeypatch.setattr("autosdlc.models.available_models", lambda: ["glm-5.3-flash"])
    return AppTest.from_file(str(Path(__file__).resolve().parent.parent / "streamlit_app.py"), default_timeout=30)



def test_streamlit_create_then_live_workspace_with_chat(tmp_path, monkeypatch):
    """Create from the form → go to the Projects page → click a stage → answer the question from the chat and leave a note."""
    import threading
    import time

    from autosdlc import runner

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    real_run = runner.run_project
    question_asked = threading.Event()

    class AskingEngine(StubEngine):
        def document(self, stage, idea, context):
            if stage == "planning" and "Answer: students" not in context:
                question_asked.set()
                raise ClarificationNeeded("Who is the target user?")
            return super().document(stage, idea, context)

    monkeypatch.setattr("autosdlc.runner.run_project", lambda idea, **kw: real_run(
        idea, engine=AskingEngine(), **{k: v for k, v in kw.items() if k not in {"model", "api_key"}}))
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    app = _app(tmp_path, monkeypatch).run()
    app.selectbox(key="template_choice").select("task-api").run()
    app.button(key="apply_template").click().run()
    next(button for button in app.button if "Create the project" in button.label).click().run()
    assert not app.exception, [e.value for e in app.exception]
    assert question_asked.wait(20)
    db = History(tmp_path / "runs" / "history.sqlite3")
    run_id = db.list()[0]["id"]
    for _ in range(100):
        if db.pending_question(run_id):
            break
        time.sleep(0.1)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    assert any("Who is the target user?" in w.value for w in app.warning)
    assert "question" in app.chat_input[0].proto.placeholder
    app.chat_input[0].set_value("students").run()
    for _ in range(200):
        if db.get(run_id)["status"] != "running":
            break
        time.sleep(0.1)
    assert db.get(run_id)["status"] == "completed"
    assert db.get(run_id)["questions"][0]["answer"] == "students"
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    app.chat_input[0].set_value("The next version should export CSV").run()
    assert db.notes(run_id)[0]["content"].startswith("The next version")
    app.session_state[f"stage_{run_id}"] = "review"
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    assert any("Architecture Review Agent" in md.value for md in app.markdown)


def test_no_resume_is_offered_while_the_worker_of_a_run_is_starting(tmp_path, monkeypatch):
    """Retry leaves the run paused until its worker has loaded; a Resume offered in that window started a second worker."""
    import time

    from autosdlc import runner
    from autosdlc.ui_jobs import ACTIVE_JOBS

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    real_run = runner.run_project
    monkeypatch.setattr("autosdlc.runner.run_project", lambda idea, **kw: real_run(
        idea, engine=StubEngine(), **{k: v for k, v in kw.items() if k not in {"model", "api_key"}}))
    app = _app(tmp_path, monkeypatch).run()
    app.selectbox(key="template_choice").select("task-api").run()
    app.button(key="apply_template").click().run()
    next(button for button in app.button if "Create the project" in button.label).click().run()
    db = History(tmp_path / "runs" / "history.sqlite3")
    run_id = db.list()[0]["id"]
    for _ in range(300):
        if db.get(run_id)["status"] != "running" and run_id not in ACTIVE_JOBS:
            break
        time.sleep(0.1)
    assert db.reset_from(run_id, "testing")
    ACTIVE_JOBS.add(run_id)  # a Retry whose worker has not claimed the run yet
    try:
        app.run()
        assert not app.exception, [e.value for e in app.exception]
        assert app.button(key=f"starting_{run_id}").disabled
        assert not any(button.key == f"resume_{run_id}" for button in app.button)
    finally:
        ACTIVE_JOBS.discard(run_id)
    app.run()
    assert any(button.key == f"resume_{run_id}" for button in app.button)


def test_gateway_400_is_retried_once_and_reason_is_reported(monkeypatch):
    import httpx

    monkeypatch.setattr("autosdlc.crewai_engine.time.sleep", lambda _seconds: None)
    statuses = iter([400, 200, 400, 400])
    request = httpx.Request("POST", "https://example.invalid")

    def fake_post(url, *, headers, json, timeout):
        status = next(statuses)
        body = {"choices": [{"message": {"content": "OK"}}]} if status == 200 else {"error": "context too long"}
        return httpx.Response(status, json=body, request=request)

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", fake_post)
    engine = CrewAIEngine(api_key="test-key")
    assert engine.llm.call("merhaba") == "OK"
    with pytest.raises(RuntimeError, match="returned HTTP 400: .*context too long"):
        engine.llm.call("merhaba")


def test_failed_run_can_be_retried_from_the_failed_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    calls = []

    class FlakyEngine(StubEngine):
        fail = True

        def document(self, stage, idea, context):
            calls.append(stage)
            if stage == "review" and FlakyEngine.fail:
                raise RuntimeError("OpenCode Go returned HTTP 400")
            return super().document(stage, idea, context)

    failed = run_project("Flaky API", workspace=tmp_path, engine=FlakyEngine(), decisions=BACKEND)
    assert failed.status == "failed"
    db = History(tmp_path / "runs" / "history.sqlite3")
    from autosdlc.runner import completed_from_events
    first_open = next(stage for stage in ("planning", "analysis", "design", "review") if stage not in completed_from_events(db.get(failed.id)["events"]))
    assert first_open == "review"
    FlakyEngine.fail = False
    calls.clear()
    assert db.reset_from(failed.id, first_open)
    again = run_project("", workspace=tmp_path, engine=FlakyEngine(), resume_run_id=failed.id)
    assert again.status == "completed", again.error
    assert calls[0] == "review" and "planning" not in calls


class ChangeAwareEngine(StubEngine):
    """Test engine that supports change requests; it records the contexts it receives."""

    def __init__(self):
        self.contexts, self.code_changes, self.test_changes = {}, [], []

    def document(self, stage, idea, context):
        self.contexts.setdefault(stage, []).append(context)
        text = super().document(stage, idea, context)
        if stage == "analysis" and "CHANGE MODE" in context:
            text += "FR-03: List the products [CR-001]\n"
        return text

    def code(self, idea, docs, feedback=None, change=None):
        if change is None:
            return super().code(idea, docs, feedback)
        self.code_changes.append((change, feedback))
        return {"app/main.py": (
            "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/')\ndef root():\n    return {'answer': 1}\n"
            "@app.get('/items')\ndef items():\n    return ['vida']\n"
        )}

    def tests(self, idea, docs, files, change=None):
        if change is None:
            return super().tests(idea, docs, files)
        self.test_changes.append(change)
        return {"tests/test_items.py": (
            "from fastapi.testclient import TestClient\nfrom app.main import app\n\n"
            "def test_fr03_items():\n    assert TestClient(app).get('/items').json() == ['vida']\n"
        )}


def _apply_change(tmp_path, run_id, kind, title, start_stage, engine):
    db = History(tmp_path / "runs" / "history.sqlite3")
    change_id = db.create_change(run_id, kind, title, "Add an /items endpoint that lists the products.")
    db.update_change(run_id, change_id, status="in_progress", start_stage=start_stage, impact="FR-03 eklenecek.")
    assert db.reset_from(run_id, start_stage)
    return change_id, run_project("", workspace=tmp_path, engine=engine, resume_run_id=run_id)


def test_feature_change_request_updates_only_affected_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    first = run_project("Inventory API", workspace=tmp_path, engine=StubEngine(), decisions=BACKEND)
    assert first.status == "completed", first.error
    engine = ChangeAwareEngine()
    change_id, result = _apply_change(tmp_path, first.id, "feature", "Product listing", "analysis", engine)
    assert result.status == "completed", result.error
    assert "planning" not in engine.contexts
    analysis = engine.contexts["analysis"][0]
    assert "CHANGE MODE" in analysis and "EXISTING DOCUMENT" in analysis and "FR-01: provide an HTTP response" in analysis
    assert engine.code_changes and "Product listing" in engine.code_changes[0][0]
    assert (result.project_dir / "tests/test_api.py").is_file() and (result.project_dir / "tests/test_items.py").is_file()
    db = History(tmp_path / "runs" / "history.sqlite3")
    assert db.change(first.id, change_id)["status"] == "done"
    assert any(e["status"] == "regression" for e in db.get(first.id)["events"])
    assert change_id in (result.project_dir / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert list((result.output_dir / "versions").glob("requirements.*.md"))


def test_bug_change_request_writes_failing_test_first(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    first = run_project("Inventory API", workspace=tmp_path, engine=StubEngine(), decisions=BACKEND)
    engine = ChangeAwareEngine()
    change_id, result = _apply_change(tmp_path, first.id, "bug", "/items returns 404", "development", engine)
    assert result.status == "completed", result.error
    events = History(tmp_path / "runs" / "history.sqlite3").get(first.id)["events"]
    assert any(e["status"] == "bug_reproduced" for e in events)
    # The developer received the failing test output that shows the bug as input.
    assert engine.code_changes[0][1] and "test_items" in engine.code_changes[0][1]


def test_sections_split_and_edit():
    from autosdlc.sections import delete_section, find_section, insert_after, replace_section, split_sections, unified_diff

    doc = "# Requirements\n\n## Functional\n\n- FR-01: Add product\n- FR-02: Delete product\n  details\n\n## Non-functional\n\n| NFR-01 | Speed |\n"
    keys = [section.key for section in split_sections(doc)]
    assert keys == ["h:0", "h:1", "r:FR-01", "r:FR-02", "h:2", "r:NFR-01"]
    fr2 = find_section(doc, "r:FR-02")
    assert fr2.text(doc.split("\n")) == "- FR-02: Delete product\n  details"
    edited = replace_section(doc, fr2, "- FR-02: Archive the product")
    assert "Archive" in edited and "details" not in edited
    added = insert_after(edited, find_section(edited, "r:FR-02"), "- FR-03: List products")
    assert added.index("FR-03") > added.index("FR-02") < added.index("## Non-functional")
    removed = delete_section(added, find_section(added, "h:2"))
    assert "NFR-01" not in removed and "FR-03" in removed
    assert "+- FR-02: Archive the product" in unified_diff(doc, edited, "requirements.md")


def test_uncovered_tests_are_detected():
    from autosdlc.runner import uncovered_tests

    names = ["tests/test_api.py", "tests/test_items.py"]
    assert uncovered_tests({"api_test": ["python", "-m", "pytest", "tests/test_api.py"]}, names) == ["tests/test_items.py"]
    assert uncovered_tests({"api_test": ["python", "-m", "pytest", "-q", "tests"]}, names) == []
    assert uncovered_tests({"unit_test": ["npm", "test"]}, names) == []


def test_uncovered_tests_ignore_subcommands():
    from autosdlc.runner import uncovered_tests

    names = ["tests/a.test.js", "test/b.go"]
    assert uncovered_tests({"unit_test": ["go", "test", "./..."]}, names) == []
    assert uncovered_tests({"unit_test": ["node", "--test", "tests/a.test.js"]}, names) == ["test/b.go"]


def test_streamlit_section_edit_marks_downstream_stale(tmp_path, monkeypatch):
    import time

    from autosdlc import runner

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    real_run = runner.run_project
    monkeypatch.setattr("autosdlc.runner.run_project", lambda idea, **kw: real_run(
        idea, engine=StubEngine(), **{k: v for k, v in kw.items() if k not in {"model", "api_key"}}))

    class FakeEngine:
        def __init__(self, api_key=None, model=None):
            pass

        def section(self, stage, idea, context, document, section, instruction, mode, language=None):
            assert stage == "analysis" and "FR-02" in section and "rchive" in instruction
            return "```markdown\nFR-02: Archive the product instead of deleting it.\n```"

    monkeypatch.setattr("autosdlc.crewai_engine.CrewAIEngine", FakeEngine)
    app = _app(tmp_path, monkeypatch).run()
    app.selectbox(key="template_choice").select("task-api").run()
    app.button(key="apply_template").click().run()
    next(button for button in app.button if "Create the project" in button.label).click().run()
    db = History(tmp_path / "runs" / "history.sqlite3")
    for _ in range(200):
        items = db.list()
        if items and items[0]["status"] == "completed":
            break
        time.sleep(0.1)
    run_id = db.list()[0]["id"]
    prefix = f"{run_id}_requirements.md"
    app.session_state[f"stage_{run_id}"] = "analysis"
    app.session_state[f"mode_{prefix}"] = "section"
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    app.selectbox(key=f"section_{prefix}").select("r:FR-02")
    app.session_state[f"action_{prefix}"] = "rewrite"
    app.run()
    app.text_area(key=f"instruction_{prefix}_rewrite").set_value("Archive instead of deleting").run()
    app.button(key=f"ask_{prefix}").click().run()
    assert not app.exception, [e.value for e in app.exception]
    assert any("+FR-02: Archive the product instead of deleting it." in code.value for code in app.code)
    app.button(key=f"accept_{prefix}").click().run()
    assert not app.exception, [e.value for e in app.exception]
    output = Path(db.get(run_id)["output_dir"])
    assert "Archive" in (output / "requirements.md").read_text(encoding="utf-8")
    assert list((output / "versions").glob("requirements.*.md"))
    assert any(e["status"] == "stale" and e["detail"] == "analysis" for e in db.get(run_id)["events"])
    assert any("Out of date" in warning.value for warning in app.warning)
    app.button(key=f"focus_{prefix}").click().run()
    assert not app.exception, [e.value for e in app.exception]
    assert any(button.label == "← Back" for button in app.button)


def test_language_rules_reach_every_agent_and_terms_are_not_translated(monkeypatch):
    from autosdlc.intake import choice_label, questions

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    prompts = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Answer"}}]}

    def fake_post(url, *, headers, json, timeout):
        prompts.append(str(json["messages"]))
        return Response()

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", fake_post)
    engine = CrewAIEngine(api_key="test-key")
    engine.document("planning", "Inventory", "")
    engine.section("analysis", "Inventory", "", "# Doc\nFR-01: x", "FR-01: x", "Why?", "question")
    assert all("LANGUAGE RULES" in prompt and "addProduct" in prompt for prompt in prompts)
    assert choice_label("project_type", "fullstack") == "Fullstack" and choice_label("project_type", "backend") == "Backend"
    assert "backend" in questions()["ui"]["question"]
    # Turkish documents: the prompt names the language and forbids translating the jargon.
    prompts.clear()
    engine.language = "tr"
    engine.document("planning", "Inventory", "")
    assert "in Turkish" in prompts[0] and "backend" in prompts[0] and '"arka uç"' in prompts[0] and "USER_QUESTION: <one clear question in Turkish>" in prompts[0]


def test_empty_manifest_commands_mean_absent(tmp_path):
    from autosdlc.project import read_project_manifest

    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (tmp_path / "project.yaml").write_text(
        'port: 1024\nsetup: []\nunit_test: "python -m pytest -q tests"\napi_test: []\nui_test: ""\nstart:\n', encoding="utf-8")
    manifest = read_project_manifest(tmp_path, validate_decisions({"project_type": "library", "stack": "Python"}))
    assert manifest["unit_test"] == ["python", "-m", "pytest", "-q", "tests"]
    assert not {"setup", "api_test", "ui_test", "start"} & set(manifest)


def test_invalid_manifest_is_sent_back_to_developer(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    feedbacks = []

    class SloppyDeveloper(StubEngine):
        def code(self, idea, docs, feedback=None, change=None):
            files = super().code(idea, docs, feedback=None)
            if feedback and "PROJECT CONFIGURATION COULD NOT BE VALIDATED" in feedback:
                feedbacks.append(feedback)
                return {"project.yaml": "port: 8000\napi_test: [python, -m, pytest, -q, tests]\n"}
            if feedback:
                return super().code(idea, docs, feedback)
            return {**files, "project.yaml": "port: 8000\napi_test: 42\n"}

    result = run_project("Sloppy API", workspace=tmp_path, engine=SloppyDeveloper(), decisions=BACKEND)
    assert result.status == "completed", result.error
    assert feedbacks and "api_test" in feedbacks[0]


def test_truncated_output_is_reported_and_development_is_batched(monkeypatch):
    from autosdlc.crewai_engine import OutputTruncated

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    replies = iter([
        ('{"files": {"a.py": "x = ', "length"),  # _ask: truncated response → OutputTruncated
        ('{"files": {"a.py": "x = ', "length"),  # _ask_files first attempt: truncated again
        ('{"files": {"a.py": "x = 1"}}', "stop"),  # _ask_files second attempt: with the "write less" warning
    ])
    prompts = []

    class Response:
        def __init__(self, content, finish):
            self.content, self.finish = content, finish

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"finish_reason": self.finish, "message": {"content": self.content}}]}

    def fake_post(url, *, headers, json, timeout):
        prompts.append(str(json["messages"]))
        return Response(*next(replies))

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", fake_post)
    engine = CrewAIEngine(api_key="test-key")
    with pytest.raises(OutputTruncated):
        engine._ask("testing", idea="x", context="", files="{}")
    assert engine._ask_files("testing", idea="x", context="", files="{}") == {"a.py": "x = 1"}
    assert "length limit" in prompts[-1] and "fewer and shorter files" in prompts[-1]

    plan_calls = []
    monkeypatch.setattr(engine, "_ask", lambda stage, extra="", **values: (plan_calls.append(extra), '{"plan": ['
        + ", ".join(f'{{"path": "f{i}.py"}}' for i in range(6)) + "]}")[1])
    batches = []

    def fake_files(stage, extra="", **values):
        names = extra.split("NOW WRITE ONLY these files, completely: ")[1].split(". ")[0].split(", ")
        batches.append(names)
        return {name: "pass" for name in names}

    monkeypatch.setattr(engine, "_ask_files", fake_files)
    files = engine.code("x", "docs")
    assert batches == [["f0.py", "f1.py", "f2.py", "f3.py"], ["f4.py", "f5.py"]]
    assert set(files) == {f"f{i}.py" for i in range(6)} and "DO NOT WRITE CODE IN THIS STEP" in plan_calls[0]


def test_tests_written_outside_tests_dir_are_sent_back_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    contexts = []

    class RootTester(StubEngine):
        def tests(self, idea, docs, files, change=None):
            contexts.append(docs)
            content = super().tests(idea, docs, files)["tests/test_api.py"]
            return {"tests/test_api.py": content} if "REJECTED" in docs else {"test_api.py": content}

    result = run_project("Root tests", workspace=tmp_path, engine=RootTester(), decisions=BACKEND)
    assert result.status == "completed", result.error
    assert len(contexts) == 2 and "test_api.py" in contexts[1].split("\n")[0]
    assert (result.project_dir / "tests/test_api.py").is_file() and not (result.project_dir / "test_api.py").exists()


@pytest.mark.parametrize("platform, expected", [("win32", "startfile"), ("darwin", ["open"]), ("linux", ["xdg-open"])])
def test_open_folder_uses_platform_file_manager(tmp_path, monkeypatch, platform, expected):
    from autosdlc import desktop

    calls = []
    monkeypatch.setattr(desktop.sys, "platform", platform)
    monkeypatch.setattr(desktop.os, "startfile", lambda path: calls.append("startfile"), raising=False)
    monkeypatch.setattr(desktop.subprocess, "run", lambda command, **kwargs: calls.append(command[:1]))
    desktop.open_folder(tmp_path)
    assert calls == [expected]
    with pytest.raises(ValueError, match="not found"):
        desktop.open_folder(tmp_path / "yok")


def test_open_questions_are_parsed_from_impact_report():
    from autosdlc.runner import open_questions

    report = "## Open questions\n- QUESTION: Can Kelvin be negative?\n2. **QUESTION:** How many digits to round to?\nQUESTION: none\n\nSTART: analysis"
    assert open_questions(report) == ["Can Kelvin be negative?", "How many digits to round to?"]
    assert open_questions("## Open questions\nNo open questions.\nSTART: design") == []


def test_each_finished_iteration_is_kept_as_a_version(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    first = run_project("Inventory API", workspace=tmp_path, engine=StubEngine(), decisions=BACKEND)
    db = History(tmp_path / "runs" / "history.sqlite3")
    engine = ChangeAwareEngine()
    change_id = db.create_change(first.id, "feature", "Kelvin support", "Add Kelvin conversion.")
    db.update_change(first.id, change_id, status="in_progress", start_stage="analysis",
                     clarifications="- Question: Can Kelvin be negative?\n  Answer: No, ValueError")
    assert db.reset_from(first.id, "analysis")
    second = run_project("", workspace=tmp_path, engine=engine, resume_run_id=first.id)
    assert second.status == "completed", second.error
    # The clarification answer reached the agents in delta mode as a firm decision.
    assert "No, ValueError" in engine.contexts["analysis"][0]
    versions = db.iterations(first.id)
    assert [item["label"] for item in versions] == ["v1 · Initial version", f"v2 · {change_id} · New feature: Kelvin support"]
    assert versions[0]["last_event_id"] < versions[1]["first_event_id"] <= versions[1]["last_event_id"]
    v1, v2 = (Path(item["snapshot_dir"]) for item in versions)
    # The v1 snapshot is not affected by the files changed after the CR.
    assert "FR-03" not in (v1 / "docs" / "requirements.md").read_text(encoding="utf-8")
    assert "FR-03" in (v2 / "docs" / "requirements.md").read_text(encoding="utf-8")
    assert not (v1 / "project" / "tests" / "test_items.py").exists() and (v2 / "project" / "tests" / "test_items.py").exists()
    assert not (v2 / "project" / ".autosdlc-venv").exists()


def test_apply_patch_replaces_matching_sections_and_inserts_new_ones():
    from autosdlc.sections import apply_patch

    base = "# Requirements\n\n## Functional\n\nFR-01: Celsius\n\n## Non-functional\n\nNFR-01: fast\n"
    patch = (
        "```markdown\n# Requirements\n\n## Functional [CR-001]\n\nFR-01: Celsius\nFR-02: Kelvin [CR-001]\n\n"
        "<!-- AFTER: Functional -->\n## Constraints\n\nKelvin cannot be negative.\n```"
    )
    merged, replaced, added = apply_patch(base, patch)
    assert (replaced, added) == (1, 1)
    assert "FR-02: Kelvin" in merged and "NFR-01: fast" in merged
    assert merged.index("## Constraints") < merged.index("## Non-functional")
    whole = "# Requirements\n\n" + "new content\n" * 30
    assert apply_patch(base, whole)[0].strip() == whole.strip()
    assert apply_patch(base, "NO CHANGES") == (base, 0, 0)


class PatchingEngine(ChangeAwareEngine):
    """Engine that returns only the changed section instead of the whole document in change mode."""

    def document(self, stage, idea, context):
        self.contexts.setdefault(stage, []).append(context)
        if "CHANGED SECTIONS ONLY" in context and stage == "analysis":
            return "# Requirements\n\nFR-01: provide an HTTP response.\nFR-02: list items.\nFR-03: List the products [CR-001]\nNFR-01: respond quickly.\n"
        if "CHANGED SECTIONS ONLY" in context:
            return "NO CHANGES"
        return StubEngine.document(self, stage, idea, context)


def test_change_request_uses_section_patches_and_skips_unaffected_stages(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    first = run_project("Inventory API", workspace=tmp_path, engine=StubEngine(), decisions=BACKEND)
    db = History(tmp_path / "runs" / "history.sqlite3")
    change_id = db.create_change(first.id, "feature", "Product listing", "Add an /items endpoint that lists the products.")
    db.update_change(first.id, change_id, status="in_progress", start_stage="analysis",
                     skip_stages="review,deployment,maintenance")
    db.reset_from(first.id, "analysis")
    engine = PatchingEngine()
    result = run_project("", workspace=tmp_path, engine=engine, resume_run_id=first.id)

    assert result.status == "completed", result.error
    assert {"review", "deployment", "maintenance"}.isdisjoint(engine.contexts)
    assert "FR-03" in (result.output_dir / "requirements.md").read_text(encoding="utf-8")
    events = db.get(first.id)["events"]
    assert [e["stage"] for e in events if e["status"] == "skipped"] == ["review", "deployment", "maintenance"]
    assert any(e["stage"] == "design" and e["status"] == "delta" and "no change needed" in e["detail"] for e in events)
    deployment = (result.output_dir / "deployment.md").read_text(encoding="utf-8")
    assert deployment.count("Verified test result") == 1
    states = stage_states(events, "completed")
    assert states["review"] == "skipped" and states["testing"] == "done"


class CriticalReviewEngine(StubEngine):
    def __init__(self, severity):
        self.severity, self.design_calls, self.prompts = severity, 0, []

    def document(self, stage, idea, context):
        self.prompts.append((stage, context))
        if stage == "design":
            self.design_calls += 1
        if stage == "review":
            return f"# Review\n\n| No | Severity | Finding | Recommendation |\n| 1 | {self.severity} | Missing | Add |\n\nVERDICT: REVISE\n"
        return super().document(stage, idea, context)


def test_small_scale_revises_design_only_for_critical_findings(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    small = {**BACKEND, "scale": "small"}
    high = CriticalReviewEngine("High")
    run_project("Small API", workspace=tmp_path / "a", engine=high, decisions=small)
    assert high.design_calls == 1
    assert any("SMALL PROJECT" in context for stage, context in high.prompts if stage == "analysis")
    assert any("SMALL PROJECT REVIEW" in context for stage, context in high.prompts if stage == "review")
    critical = CriticalReviewEngine("Critical")
    run_project("Small API", workspace=tmp_path / "b", engine=critical, decisions=small)
    assert critical.design_calls == 2
    # The revision asks for the changed sections only.
    assert any("CHANGED SECTIONS ONLY" in context for stage, context in critical.prompts if stage == "design")
    normal = CriticalReviewEngine("High")
    run_project("Normal API", workspace=tmp_path / "c", engine=normal, decisions=BACKEND)
    assert normal.design_calls == 2


def test_scale_defaults_to_normal():
    assert validate_decisions(BACKEND)["scale"] == "normal"
    assert validate_decisions({**BACKEND, "scale": "small"})["scale"] == "small"


def test_turkish_project_gets_turkish_documents_and_prompts(tmp_path, monkeypatch):
    """The document language is a project decision: reports written by the application and the agent prompts follow it."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    class Recording(StubEngine):
        languages = set()

        def document(self, stage, idea, context):
            Recording.languages.add(getattr(self, "language", None))
            return super().document(stage, idea, context)

    turkish = run_project("Stock API", workspace=tmp_path / "tr", engine=Recording(), decisions={**BACKEND, "language": "tr"})
    assert turkish.status == "completed", turkish.error
    assert Recording.languages == {"tr"}
    report = (turkish.output_dir / "test_report.md").read_text(encoding="utf-8")
    assert "Test raporu" in report and "BAŞARILI" in report
    assert "Mimari inceleme raporu" in (turkish.output_dir / "design_review.md").read_text(encoding="utf-8")
    assert "Doğrulanan test sonucu" in (turkish.output_dir / "deployment.md").read_text(encoding="utf-8")
    assert "İzlenebilirlik matrisi" in (turkish.output_dir / "traceability.md").read_text(encoding="utf-8")
    assert "Üretilen proje" in (turkish.project_dir / "README.md").read_text(encoding="utf-8")
    # Stored events and errors stay English; the UI translates them when displayed.
    events = History(tmp_path / "tr" / "runs" / "history.sqlite3").get(turkish.id)["events"]
    assert not [event for event in events if re.search(r"[çğıöşüÇĞİÖŞÜ]", event["detail"])]

    english = run_project("Stock API", workspace=tmp_path / "en", engine=StubEngine(), decisions=BACKEND)
    assert "Test report" in (english.output_dir / "test_report.md").read_text(encoding="utf-8")
    assert "Verified test result" in (english.output_dir / "deployment.md").read_text(encoding="utf-8")


def test_streamlit_ui_language_switch_translates_the_interface(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr("autosdlc.models.available_models", lambda: ["glm-5.3-flash"])
    path = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")
    app = AppTest.from_file(path, default_timeout=30).run()
    assert not app.exception
    assert any("New project" in title.value for title in app.title)
    assert app.selectbox(key="ui_language").value == "en"
    app.selectbox(key="ui_language").select("tr").run()
    assert not app.exception, [e.value for e in app.exception]
    assert any("Yeni proje" in title.value for title in app.title)
    # The new project's document language follows the interface language; jargon in the options is untouched.
    assert app.selectbox(key="choice_language").value == "tr"
    assert app.selectbox(key="project_type_input").options[2] == "Fullstack"
    assert any(button.label == "Uygula" for button in app.button)


def test_legacy_turkish_protocol_tokens_are_still_accepted():
    """Runs recorded before the tokens became language neutral used these Turkish ones; they must keep working."""
    from autosdlc.runner import open_questions, parse_start_stage, review_verdict, suggested_skips
    from autosdlc.sections import apply_patch

    assert review_verdict("KARAR: ONAY") == "APPROVE" and review_verdict("**KARAR:** REVİZYON") == "REVISE"
    assert review_verdict("VERDICT: APPROVE") == "APPROVE" and review_verdict("VERDICT: REVISE") == "REVISE"
    assert open_questions("- SORU: Kelvin negatif olabilir mi?\nSORU: yok\nBAŞLANGIÇ: analysis") == ["Kelvin negatif olabilir mi?"]
    assert parse_start_stage("BAŞLANGIÇ: design") == "design" and parse_start_stage("START: development") == "development"
    assert suggested_skips("ATLA: deployment, maintenance") == ["deployment", "maintenance"]
    assert suggested_skips("SKIP: review") == ["review"]
    base = "# Requirements\n\n## Functional\n\nFR-01: x\n"
    assert apply_patch(base, "DEĞİŞİKLİK YOK") == (base, 0, 0)
    assert apply_patch(base, "# Requirements\n\n## Functional\n\nFR-01: x\n<!-- SONRA: Functional -->\n## Extra\n\ny\n")[2] >= 0


def test_streamlit_template_is_applied_in_the_ui_language(monkeypatch):
    from streamlit.testing.v1 import AppTest

    from autosdlc.i18n import set_language

    monkeypatch.setattr("autosdlc.models.available_models", lambda: ["glm-5.3-flash"])
    app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "streamlit_app.py"), default_timeout=30).run()
    app.selectbox(key="template_choice").select("event-registration-node").run()
    # The button callback runs before the script sets the language; the thread may still hold another session's.
    app.session_state["ui_language"] = "tr"
    set_language("en")
    app.button(key="apply_template").click().run()
    assert app.session_state["idea_input"].startswith("Bir etkinlik kayıt")
    assert app.session_state["constraints_input"].startswith("İki klasör")


def test_streamlit_keeps_the_ui_language_when_the_page_drops_its_widget_key(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr("autosdlc.models.available_models", lambda: ["glm-5.3-flash"])
    app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "streamlit_app.py"), default_timeout=30)
    # What st.switch_page leaves: the selector's key and ?lang= are gone, the kept copy is not.
    app.session_state["kept_language"] = "tr"
    app.run()
    assert app.session_state["ui_language"] == "tr"


def test_uncovered_tests_read_shell_commands_and_globs():
    from autosdlc.runner import uncovered_tests

    manifest = {"api_test": ["node", "--test", "tests/api/*.test.js"],
                "ui_test": ["sh", "-c", "corepack pnpm@10.26.0 exec playwright install chromium && node --test tests/ui/*.test.js"]}
    names = ["tests/api/a.test.js", "tests/api/helpers.js", "tests/ui/b.test.js", "tests/e2e/c.test.js"]
    # The helper is not a test; the warning once listed every file of these commands, the helper too.
    assert uncovered_tests(manifest, names) == ["tests/e2e/c.test.js"]


def test_retrying_a_failed_change_continues_that_change(tmp_path):
    db = History(tmp_path / "history.sqlite3")
    db.create("r1", "Event app", tmp_path / "out", tmp_path / "project")
    change_id = db.create_change("r1", "feature", "Title search", "Search events by title.")
    db.update_change("r1", change_id, status="failed")
    db.add_iteration("r1", "v1 · Initial version", "initial", None, "completed", 1, tmp_path / "v1")
    assert db.reopen_failed_change("r1") is None
    db.add_iteration("r1", "v2 · CR-001", "feature", change_id, "failed", 3, tmp_path / "v2")
    assert db.reopen_failed_change("r1") == change_id
    assert db.active_change("r1")["id"] == change_id
    # A change that was finished some other way is left alone.
    db.update_change("r1", change_id, status="done")
    assert db.reopen_failed_change("r1") is None
