import json
from pathlib import Path

import pytest

from autosdlc.crewai_engine import CrewAIEngine
from autosdlc.files import write_generated_files
from autosdlc.runner import run_project
from autosdlc.storage import History


class StubEngine:
    """Local test double for the workflow; never used by the application UI or CLI."""

    def document(self, stage: str, idea: str, context: str) -> str:
        return f"# {stage}\n\n{idea}\n"

    def code(self, idea: str, docs: str, feedback: str | None = None) -> dict[str, str]:
        answer = 1 if feedback else 0
        return {"app/__init__.py": "", "app/main.py": f"ANSWER = {answer}\n"}

    def tests(self, idea: str, docs: str, files: dict[str, str]) -> dict[str, str]:
        return {"tests/test_app.py": "from app.main import ANSWER\n\ndef test_answer():\n    assert ANSWER == 1\n"}


def test_flow_creates_artifacts_and_repairs_failed_test(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    result = run_project("Task tracking API", workspace=tmp_path, engine=StubEngine())

    assert result.status == "completed", result.error
    assert result.attempts == 2
    for name in (
        "planning.md", "requirements.md", "architecture.md", "test_report.md",
        "deployment.md", "maintenance_report.md", "project/app/main.py",
        "project/tests/test_app.py", "project/Dockerfile",
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
        "# Requirements\nCreate and list books.",
        "# Design\nFastAPI plus SQLite.",
        json.dumps({"files": {
            "app/__init__.py": "",
            "app/main.py": "VALUE = 1\n",
        }}),
        json.dumps({"files": {
            "tests/test_app.py": "from app.main import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
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
    result = run_project("Book tracker", workspace=tmp_path, engine=engine)
    assert result.status == "completed", result.error
    assert result.attempts == 1
    assert "PASS" in (result.output_dir / "test_report.md").read_text(encoding="utf-8")

