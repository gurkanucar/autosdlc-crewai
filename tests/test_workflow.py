import json
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


class StubEngine:
    """Local test double for the workflow; never used by the application UI or CLI."""

    def document(self, stage: str, idea: str, context: str) -> str:
        if stage == "analysis":
            return "# Requirements\n\nFR-01: provide an HTTP response.\nFR-02: list items.\nNFR-01: respond quickly.\n"
        if stage == "design":
            return "# Design\n\n```mermaid\nflowchart LR\n A --> B\n```\n\n```mermaid\nsequenceDiagram\n A->>B: Request\n```\n\n```mermaid\nerDiagram\n TASK { int id }\n```\n"
        if stage == "review":
            return "# İnceleme\n\n| 1 | Düşük | Ad | Öneri |\n\nKARAR: ONAY\n"
        return f"# {stage}\n\n{idea}\n"

    def code(self, idea: str, docs: str, feedback: str | None = None) -> dict[str, str]:
        answer = 1 if feedback else 0
        return {
            "app/__init__.py": "",
            "app/main.py": f"from fastapi import FastAPI\napp = FastAPI()\n@app.get('/')\ndef root():\n    return {{'answer': {answer}}}\n",
            "project.yaml": "port: 8000\napi_test: [python, -m, pytest, -q, tests/test_api.py]\n",
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
    assert engine.agents["analysis"]["role"] == "Analiz Ajanı"
    assert "kabul kriterleri" in engine.tasks["analysis"]["description"]
    assert len(engine.agents) == 8
    assert engine.agents["review"]["role"] == "Mimari İnceleme Ajanı"
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
        "# İnceleme\nBulgu yok.\nKARAR: ONAY",
        json.dumps({"files": StubEngine().code("", "", feedback="fix")}),
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

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", fake_post)
    engine = CrewAIEngine(api_key="test-key")
    result = run_project("Book tracker", workspace=tmp_path, engine=engine, decisions=BACKEND)
    assert result.status == "completed", result.error
    assert result.attempts == 1
    assert "BAŞARILI" in (result.output_dir / "test_report.md").read_text(encoding="utf-8")


def test_architecture_choices_and_compose():
    with pytest.raises(ValueError, match="gerekli"):
        validate_decisions({"project_type": "backend", "stack": "Go/Gin"})
    cli = validate_decisions({"project_type": "cli", "stack": "Rust"})
    assert cli["database"] == "none"
    assert cli["ui"] == "none"
    fullstack = validate_decisions({"project_type": "fullstack", "stack": "Node/Next.js", "database": "postgres", "cache": "redis", "observability": "openobserve"})
    assert fullstack["ui"] == "web"
    compose = render_compose(fullstack, 3000)
    assert compose["services"]["app"]["ports"] == ["3000:3000"]
    assert {"db", "redis", "otel-collector", "openobserve"} <= set(compose["services"])


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
    answers = iter(["A tiny CLI", "cli", "Rust", ""])
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
    assert "API testleri" in report and "Arayüz testleri" in report


def test_stage_question_waits_for_answer_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    class AskingEngine(StubEngine):
        def document(self, stage, idea, context):
            if stage == "planning" and "Yanıt: küçük işletme" not in context:
                raise ClarificationNeeded("Hedef kullanıcı kim?")
            return super().document(stage, idea, context)

    def answer(run_id, stage, prompt):
        db = History(tmp_path / "runs" / "history.sqlite3")
        question_id = db.ask(run_id, stage, prompt)
        assert db.pending_question(run_id)["id"] == question_id
        assert db.answer(run_id, question_id, "küçük işletme")
        assert not db.answer(run_id, question_id, "ikinci cevap")
        return db.question(question_id)["answer"]

    result = run_project(
        "Stok API", workspace=tmp_path, engine=AskingEngine(),
        decisions=BACKEND, on_question=answer,
    )
    assert result.status == "completed", result.error
    row = History(tmp_path / "runs" / "history.sqlite3").get(result.id)
    assert row["questions"][0]["prompt"] == "Hedef kullanıcı kim?"
    assert row["questions"][0]["answer"] == "küçük işletme"
    assert "küçük işletme" in (result.output_dir / "clarifications.yaml").read_text(encoding="utf-8")
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


def test_agent_question_marker_is_detected(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "KULLANICI_SORUSU: Hedef kullanıcı kim?"}}]}

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", lambda *args, **kwargs: Response())
    engine = CrewAIEngine(api_key="test-key")
    with pytest.raises(ClarificationNeeded, match="Hedef kullanıcı kim"):
        engine.document("planning", "Stok API", "")


def test_pause_resume_and_delete_project(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    run_id = "pause1234567"
    db = History(tmp_path / "runs" / "history.sqlite3")

    def request_pause(stage, status, _detail):
        if stage == "analysis" and status == "completed":
            assert db.request_pause(run_id)

    paused = run_project(
        "Stok yönetimi", workspace=tmp_path, engine=StubEngine(),
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
    with pytest.raises(ValueError, match="güvenli"):
        delete_project(tmp_path, run_id)
    assert tmp_path.is_dir()
    assert db.get(run_id) is not None


def test_pause_while_question_waits_then_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    run_id = "questionpause"
    db = History(tmp_path / "runs" / "history.sqlite3")

    class AskingEngine(StubEngine):
        def document(self, stage, idea, context):
            if stage == "planning" and "Yanıt: işletme sahibi" not in context:
                raise ClarificationNeeded("Hedef kullanıcı kim?")
            return super().document(stage, idea, context)

    def stop_at_question(current_id, stage, prompt):
        db.ask(current_id, stage, prompt)
        db.request_pause(current_id)
        raise RunPaused()

    paused = run_project(
        "Stok API", workspace=tmp_path, engine=AskingEngine(), decisions=BACKEND,
        run_id=run_id, on_question=stop_at_question,
    )
    assert paused.status == "paused"
    pending = db.pending_question(run_id)
    assert pending["prompt"] == "Hedef kullanıcı kim?"

    def reply(current_id, stage, prompt):
        assert db.ask(current_id, stage, prompt) == pending["id"]
        assert db.answer(current_id, pending["id"], "işletme sahibi")
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
    raw = 'Dosyalar aşağıda:\n{"files": {"main.go": "package main"}}\nİyi çalışmalar.'
    assert _parse_file_bundle(raw) == {"main.go": "package main"}


def test_traceability_matrix_recognises_test_name_formats():
    assert requirement_ids("FR-1, NFR-02 ve fr03") == ["FR-01", "NFR-02", "FR-03"]
    matrix, coverage = build_matrix(
        "FR-01 ekle\nFR-02 listele\nNFR-01 hız",
        {"tests/test_api.py": "def test_fr01_create(): ...\ndef test_FR_02_list(): ..."},
    )
    assert coverage == {"FR": 1.0, "NFR": 0.0}
    assert "| NFR-01 | — | Test yok |" in matrix


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
    assert "| FR-01 | `tests/test_api.py` | Kapsanıyor |" in matrix
    assert "| FR-02 | — | Test yok |" in matrix


def test_quality_gate_asks_agent_to_rewrite_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    calls = []

    class SloppyEngine(StubEngine):
        def document(self, stage, idea, context):
            if stage == "analysis":
                calls.append(context)
                if len(calls) == 1:
                    return "# Gereksinimler\nFR-01: liste."
            return super().document(stage, idea, context)

    result = run_project("Gate API", workspace=tmp_path, engine=SloppyEngine(), decisions=BACKEND)
    assert result.status == "completed", result.error
    assert len(calls) == 2 and "KALİTE KAPISINDAN GEÇEMEDİ" in calls[1]


def test_late_stages_see_answers_and_test_result_despite_long_documents(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    contexts = {}

    class VerboseEngine(StubEngine):
        def document(self, stage, idea, context):
            contexts[stage] = context
            if stage == "planning" and "Yanıt: öğrenciler" not in context:
                raise ClarificationNeeded("Hedef kullanıcı kim?")
            return super().document(stage, idea, context) + "x" * 50_000

    result = run_project(
        "Uzun API", workspace=tmp_path, engine=VerboseEngine(), decisions=BACKEND,
        on_question=lambda *_args: "öğrenciler",
    )
    assert result.status == "completed", result.error
    head = contexts["maintenance"][:12000]
    assert "Yanıt: öğrenciler" in head and "Gerçek test sonucu: BAŞARILI" in head



def test_review_requests_revision_then_approves(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    seen = []

    class StrictReviewer(StubEngine):
        def document(self, stage, idea, context):
            if stage == "review":
                seen.append("review")
                return "KARAR: ONAY" if "REVİZE" in context else "| 1 | Kritik | FR-02 tasarımda yok | Ekle |\nKARAR: REVİZYON"
            if stage == "design" and "MİMARİ İNCELEME BULGULARI" in context:
                seen.append("revision")
                return super().document(stage, idea, context) + "\nREVİZE: FR-02 eklendi\n"
            return super().document(stage, idea, context)

    result = run_project("Review API", workspace=tmp_path, engine=StrictReviewer(), decisions=BACKEND)
    assert result.status == "completed", result.error
    assert seen == ["review", "revision", "review"]
    assert "REVİZE" in (result.project_dir / "docs" / "architecture.md").read_text(encoding="utf-8")
    report = (result.output_dir / "design_review.md").read_text(encoding="utf-8")
    assert "ONAYLANDI" in report and "2 tur" in report


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
    db.create("stale1234567", "Yarım kalan", reports, project)
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
        {"stage": "design", "status": "revision_completed", "detail": ""},
        {"stage": "testing", "status": "traceability", "detail": "FR kapsamı %75"},
        {"stage": "workflow", "status": "reset", "detail": "design"},
    ]
    states = stage_states(events, "paused")
    assert states["planning"] == "done" and states["design"] == "pending" and states["review"] == "pending"
    loops = counters(events)
    assert loops["coverage"] == "75" and loops["revisions"] == 0
    svg = render_svg(stage_states(events[:4], "running"), counters(events[:4]))
    assert svg.startswith("<svg") and "revizyon ×1" in svg and "sdlc-halo" in svg



def test_streamlit_create_button_starts_a_run(monkeypatch):
    """Gerçek arayüzü çalıştırıp "Projeyi oluştur"a basar; run_project'e geçerli argümanlar gitmeli."""
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr("autosdlc.runner.run_project", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("autosdlc.models.available_models", lambda: ["glm-5.3-flash"])
    app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "streamlit_app.py"), default_timeout=30).run()
    assert not app.exception
    app.text_area[0].set_value("Basit envanter takip uygulaması")
    app.text_input[0].set_value("Node.js, pnpm, sade HTML")
    next(button for button in app.button if "Projeyi oluştur" in button.label).click()
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    assert not app.error, [e.value for e in app.error]
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.1)
    assert calls and calls[0]["idea"] == "Basit envanter takip uygulaması"
    assert calls[0]["run_id"] and calls[0]["decisions"]["stack"] == "Node.js, pnpm, sade HTML"
