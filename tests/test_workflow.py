import json
from pathlib import Path

import pytest

from autosdlc.crewai_engine import CrewAIEngine
from autosdlc.files import write_generated_files
from autosdlc.runner import run_project
from autosdlc.intake import validate_decisions
from autosdlc.deployment import render_compose
from autosdlc.storage import History
from autosdlc.cli import main as cli_main
from autosdlc.runner import RunResult


class StubEngine:
    """Local test double for the workflow; never used by the application UI or CLI."""

    def document(self, stage: str, idea: str, context: str) -> str:
        if stage == "analysis":
            return "# Requirements\n\nFR-01: provide an HTTP response.\n"
        if stage == "design":
            return "# Design\n\n```mermaid\nflowchart LR\n A --> B\n```\n\n```mermaid\nsequenceDiagram\n A->>B: Request\n```\n\n```mermaid\nerDiagram\n TASK { int id }\n```\n"
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
        return {"tests/test_api.py": "from fastapi.testclient import TestClient\nfrom app.main import app\n\ndef test_http_response():\n    assert TestClient(app).get('/').json()['answer'] == 1\n"}


BACKEND = {"project_type": "backend", "stack": "Python / FastAPI", "database": "sqlite", "cache": "none", "observability": "none", "ui": "none"}


def test_flow_creates_artifacts_and_repairs_failed_test(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    result = run_project("Task tracking API", workspace=tmp_path, engine=StubEngine(), decisions=BACKEND)

    assert result.status == "completed", result.error
    assert result.attempts == 2
    for name in (
        "planning.md", "requirements.md", "architecture.md", "test_report.md",
        "deployment.md", "maintenance_report.md", "project/app/main.py",
        "project/tests/test_api.py", "project/Dockerfile", "project/compose.yaml",
        "project/docs/architecture.md", "project/README.md", "decisions.yaml",
    ):
        assert (result.output_dir / name).is_file(), name
    stored = History(tmp_path / "runs" / "history.sqlite3").get(result.id)
    assert stored["status"] == "completed"
    assert stored["provider"] == "opencode-go"
    assert any(e["stage"] == "testing" and e["status"] == "failed" for e in stored["events"])
    assert any(e["stage"] == "development" and e["status"] == "repair_completed" for e in stored["events"])
    assert any(e["stage"] == "testing" and e["status"] == "passed" for e in stored["events"])


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
    assert len(engine.agents) == 7
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
        "# Requirements\nFR-01: Create and list books.",
        "# Design\n```mermaid\nflowchart LR\nA-->B\n```\n```mermaid\nsequenceDiagram\nA->>B: Request\n```\n```mermaid\nerDiagram\nBOOK { int id }\n```",
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
    assert "PASS" in (result.output_dir / "test_report.md").read_text(encoding="utf-8")


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
    answers = iter(["A tiny CLI", "cli", "Rust"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    captured = {}

    def fake_run(idea, **kwargs):
        captured.update(idea=idea, **kwargs)
        return RunResult("run123", "completed", tmp_path / "runs/run123", tmp_path / "runs/run123/project", 1)

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
    assert "API tests" in report and "UI tests" in report

