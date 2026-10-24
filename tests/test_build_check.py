"""Code that does not compile is fixed before the test attempts start, so it never uses up a test attempt."""

from autosdlc.runner import compile_commands, run_project, with_display
from autosdlc.storage import History


def test_compile_commands_follow_the_project_files(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.js").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "build.sh").write_text("echo hi\n", encoding="utf-8")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "index.js").write_text("x\n", encoding="utf-8")
    commands = compile_commands(tmp_path)
    assert ["cargo", "test", "--no-run", "--all-features"] in commands
    assert ["go", "test", "-count=1", "-run", "^$", "./..."] in commands
    assert ["node", "--check", "src/cli.js"] in commands
    assert ["python", "-m", "py_compile", "app.py", "tests/test_app.py"] in commands
    assert ["sh", "-n", "build.sh"] in commands
    # Dependencies are not the project's code.
    assert not any("node_modules" in arg for command in commands for arg in command)


def test_compile_commands_prefer_tsc_for_typescript(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin" / "tsc").write_text("", encoding="utf-8")
    (tmp_path / "index.ts").write_text("export {}\n", encoding="utf-8")
    assert compile_commands(tmp_path) == [["node_modules/.bin/tsc", "--noEmit"]]


def test_tests_get_a_virtual_display_on_a_linux_server(monkeypatch):
    # A desktop app's test that opens a window anyway must not fail with "no display" on the server.
    monkeypatch.setattr("autosdlc.runner.shutil.which", lambda name, path=None: "/usr/bin/xvfb-run")
    command = ["python", "-m", "pytest", "-q", "tests"]
    assert with_display(command, {}, "linux") == ["/usr/bin/xvfb-run", "-a", *command]
    # A real display, another system or a missing xvfb-run leaves the command as it is.
    assert with_display(command, {"DISPLAY": ":0"}, "linux") == command
    assert with_display(command, {}, "win32") == command
    monkeypatch.setattr("autosdlc.runner.shutil.which", lambda name, path=None: None)
    assert with_display(command, {}, "linux") == command


class CompileEngine:
    """Python project: the first code or the first tests have a syntax error, fixed when the build error comes back."""

    def __init__(self, broken: str):
        self.broken = broken
        self.code_feedback = []
        self.test_feedback = []

    def document(self, stage, idea, context):
        if stage == "analysis":
            return "# Requirements\n\nFR-01: add.\nNFR-01: fast.\n"
        if stage == "design":
            return "# Design\n\n```mermaid\nflowchart LR\n A --> B\n```\n\n```mermaid\nsequenceDiagram\n A->>B: x\n```\n"
        if stage == "review":
            return "# Review\n\nVERDICT: APPROVE\n"
        return f"# {stage}\n"

    def code(self, idea, docs, feedback=None, change=None):
        self.code_feedback.append(feedback)
        source = "def add(a, b:\n" if self.broken == "code" and feedback is None else "def add(a, b):\n    return a + b\n"
        return {"calc.py": source, "project.yaml": "unit_test: [python, -m, pytest, -q, tests]\n"}

    def tests(self, idea, docs, files, change=None, feedback=None):
        self.test_feedback.append(feedback)
        body = "def test_fr01(:\n" if self.broken == "tests" and feedback is None else "from calc import add\n\ndef test_fr01_add():\n    assert add(1, 2) == 3\n"
        return {"tests/test_calc.py": body}


def _run(tmp_path, monkeypatch, engine):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    result = run_project("Calc", workspace=tmp_path, engine=engine,
                         decisions={"project_type": "library", "stack": "Python", "delivery": "none", "scale": "small"})
    events = [(e["stage"], e["status"]) for e in History(tmp_path / "runs" / "history.sqlite3").get(result.id)["events"]]
    return result, events


def test_application_compile_error_is_fixed_before_the_tests(tmp_path, monkeypatch):
    engine = CompileEngine("code")
    result, events = _run(tmp_path, monkeypatch, engine)
    assert result.status == "completed", result.error
    assert result.attempts == 1
    assert engine.code_feedback[0] is None and "calc.py" in engine.code_feedback[1]
    assert ("development", "build_failed") in events and ("development", "build_fixed") in events
    # The full compile output is kept for the failure report, not only the end stored in the event.
    assert "calc.py" in (result.output_dir / "build_checks" / "check-01-development.txt").read_text(encoding="utf-8")
    # The developer fixed it before the Test Agent saw the code.
    assert events.index(("development", "build_fixed")) < events.index(("testing", "started"))


def test_test_compile_error_goes_back_to_the_test_agent_without_an_attempt(tmp_path, monkeypatch):
    engine = CompileEngine("tests")
    result, events = _run(tmp_path, monkeypatch, engine)
    assert result.status == "completed", result.error
    assert result.attempts == 1
    assert engine.code_feedback == [None]
    assert engine.test_feedback[0] is None and "tests/test_calc.py" in engine.test_feedback[1]
    assert ("testing", "build_failed") in events and ("testing", "tests_repaired") in events
