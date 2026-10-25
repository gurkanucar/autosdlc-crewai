"""Guards around the repair loop: what the agents see, what they may overwrite and what an attempt is spent on."""

import json
import socket

from autosdlc.crewai_engine import CrewAIEngine, _files_for_prompt
from autosdlc.runner import run_project
from autosdlc.storage import History


def test_files_named_in_the_failure_and_their_imports_come_first_and_whole():
    files = {
        "README.md": "x" * 3000,
        "server/src/app.js": "export function createApp() {}\n" + "y" * 1000,
        "server/src/index.js": "import { createApp } from './app.js';\n",
        "server/src/other.js": "z" * 3000,
        "tests/api.test.js": "t" * 3000,
    }
    text = _files_for_prompt(files, 2000, focus="file:///data/p/server/src/index.js:4 SyntaxError", skip=("tests/",))
    shown = json.loads(text.split("\nThese files exist")[0])
    assert list(shown) == ["server/src/index.js", "server/src/app.js"]
    # The rest is named, not cut in the middle, and the tests are left out.
    assert "README.md, server/src/other.js" in text and "tests/api.test.js" not in text
    assert "never return a placeholder" in text


def test_everything_fits_without_a_note():
    assert _files_for_prompt({"a.py": "x = 1"}, 1000) == '{"a.py": "x = 1"}'


def _engine(answer):
    engine = CrewAIEngine.__new__(CrewAIEngine)
    engine.current_files = {}
    engine.asked = []

    def ask(stage, extra="", **values):
        engine.asked.append({"extra": extra, **values})
        return answer

    engine._ask = ask
    return engine


def test_the_developer_repair_sees_the_code_without_the_tests():
    engine = _engine(json.dumps({"files": {"app.py": "x = 2"}}))
    engine.current_files = {"app.py": "x = 1", "tests/test_app.py": "assert False"}
    engine.code("idea", "docs", feedback="app.py:1 failed")
    feedback = engine.asked[0]["feedback"]
    assert '"app.py": "x = 1"' in feedback and "assert False" not in feedback


def test_the_test_agent_sees_project_yaml_and_its_scripts_first():
    engine = _engine(json.dumps({"files": {"tests/t.test.js": "x"}}))
    files = {"web/big.js": "w" * 40000, "scripts/api-test.sh": "node --test tests/*.test.js",
             "project.yaml": "api_test: [sh, scripts/api-test.sh]\n"}
    engine.tests("idea", "docs", files, feedback="SyntaxError")
    listing = engine.asked[0]["files"]
    assert listing.startswith('{"project.yaml"') and "scripts/api-test.sh" in listing.split("These files")[0]


PADDING = "# " + "padding " * 150 + "\n"


class RepairEngine:
    """Python library whose add() is wrong; the repairs return what the test sets in `repairs`."""

    def __init__(self, workspace, repairs):
        self.workspace = workspace
        self.repairs = repairs
        self.reasons = []

    def document(self, stage, idea, context):
        if stage == "analysis":
            return "# Requirements\n\nFR-01: add.\nNFR-01: fast.\n"
        if stage == "design":
            return "# Design\n\n```mermaid\nflowchart LR\n A --> B\n```\n\n```mermaid\nsequenceDiagram\n A->>B: x\n```\n"
        if stage == "review":
            return "# Review\n\nVERDICT: APPROVE\n"
        return f"# {stage}\n"

    def code(self, idea, docs, feedback=None, change=None):
        if feedback is None:
            return {"calc.py": PADDING + "def add(a, b):\n    return a - b\n",
                    "project.yaml": "unit_test: [python, -m, pytest, -q, tests]\n"}
        self.reasons.append(feedback)
        return self.repairs.pop(0) if self.repairs else {}

    def tests(self, idea, docs, files, change=None, feedback=None):
        if feedback is None:
            # A test file left on disk by an earlier generation of the suite.
            project = next(path.parent for path in self.workspace.rglob("calc.py") if "iterations" not in path.parts)
            (project / "tests").mkdir(exist_ok=True)
            (project / "tests" / "test_old.py").write_text("from helper import gone\n", encoding="utf-8")
        return {"tests/test_calc.py": "from calc import add\n\ndef test_fr01_add():\n    assert add(1, 2) == 3\n"}


def _run(tmp_path, monkeypatch, engine, decisions=None):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    result = run_project("Calc", workspace=tmp_path, engine=engine, decisions=decisions or {
        "project_type": "library", "stack": "Python", "delivery": "none", "scale": "small"})
    events = [(e["stage"], e["status"], e["detail"])
              for e in History(tmp_path / "runs" / "history.sqlite3").get(result.id)["events"]]
    return result, events


def test_old_tests_are_removed_and_a_placeholder_never_replaces_a_file(tmp_path, monkeypatch):
    engine = RepairEngine(tmp_path, [{"calc.py": "_placeholder = None\n"}, {"calc.py": "def add(a, b):\n    return a + b\n"}])
    result, events = _run(tmp_path, monkeypatch, engine)
    assert result.status == "completed", result.error
    assert ("testing", "old_tests_removed", "tests/test_old.py") in events
    assert ("development", "stubs_rejected", "calc.py") in events
    # The suite that ran is only the new one: the old file's import error is not in the repair feedback.
    assert "gone" not in engine.reasons[0]


def test_failed_tests_are_named_as_the_error(tmp_path, monkeypatch):
    result, _events = _run(tmp_path, monkeypatch, RepairEngine(tmp_path, []))
    assert result.status == "failed"
    assert result.error == "The tests did not pass after 3 attempts; see the test report"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerEngine(RepairEngine):
    """A backend whose server crashes at start; the developer fixes it once the start failure comes back."""

    def __init__(self, workspace, port):
        super().__init__(workspace, [{"server.py": self.SERVER}])
        self.port = port

    SERVER = (
        "import http.server, os\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "http.server.HTTPServer(('127.0.0.1', int(os.environ.get('PORT', '0'))), H).serve_forever()\n"
    )

    def code(self, idea, docs, feedback=None, change=None):
        if feedback is None:
            return {"server.py": "import module_that_does_not_exist\n",
                    "project.yaml": f"port: {self.port}\nstart: [python, server.py]\n"
                                    "api_test: [python, -m, pytest, -q, tests]\n"}
        return super().code(idea, docs, feedback, change)

    def tests(self, idea, docs, files, change=None, feedback=None):
        return {"tests/test_api.py": "import os, urllib.request\n\ndef test_fr01_up():\n"
                                     "    assert urllib.request.urlopen(os.environ['BASE_URL']).status == 200\n"}


def test_a_server_that_does_not_start_is_repaired_without_using_an_attempt(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("PORT", str(port))
    engine = ServerEngine(tmp_path, port)
    result, events = _run(tmp_path, monkeypatch, engine, {
        "project_type": "backend", "stack": "Python", "database": "none", "cache": "none",
        "observability": "none", "ui": "none", "delivery": "none", "scale": "small"})
    assert result.status == "completed", result.error
    assert result.attempts == 1
    assert ("development", "repair_started", "the application server did not start") in events
    assert "module_that_does_not_exist" in engine.reasons[0]
