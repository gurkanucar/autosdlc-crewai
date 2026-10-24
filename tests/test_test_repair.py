"""A test file that does not even build is repaired by the Test Agent; the developer may not touch tests."""

from autosdlc.runner import failure_is_in_tests, run_project
from autosdlc.storage import History

GO_BUILD_FAILED = (
    "FAIL\tapp/tests [setup failed]\n# app/tests\ntests/gui_test.go:22:1: imports must appear before other declarations\n"
)


def test_failure_is_in_tests_only_for_build_errors_inside_tests():
    assert failure_is_in_tests(GO_BUILD_FAILED)
    # The same build failure caused by application code is the developer's to fix.
    assert not failure_is_in_tests("FAIL\tapp [build failed]\ninternal/ui/ui.go:9:2: undefined: foo\n")
    assert not failure_is_in_tests("FAIL\tapp/tests [build failed]\ntests/a_test.go:3:1: x\ninternal/b.go:4:1: y\n")
    # Ordinary assertion failures are never "the tests are broken".
    assert not failure_is_in_tests("tests/test_api.py:5: AssertionError\nFAILED tests/test_api.py::test_x\n")
    assert not failure_is_in_tests("build failed with no locations [build failed]")


RUST_TEST_BROKEN = """error[E0425]: cannot find value `x` in this scope
 --> tests/count.rs:7:13
  |
7 |     assert_eq!(x, 1);
error: could not compile `wc` (test "count") due to 1 previous error
"""
RUST_APP_BROKEN = """error[E0308]: mismatched types
 --> src/lib.rs:12:5
error: could not compile `wc` (lib) due to 1 previous error
"""
PYTHON_TEST_SYNTAX = """ERROR collecting tests/test_calc.py
E     File "/data/projects/calc-1/tests/test_calc.py", line 3
E       def test_(:
E   SyntaxError: invalid syntax
"""
PYTHON_APP_SYNTAX = """ERROR collecting tests/test_calc.py
E     File "/data/projects/calc-1/calc.py", line 3
E       def add(:
E   SyntaxError: invalid syntax
"""
NODE_TEST_SYNTAX = """/work/project/tests/format.test.js:5
  const x = ;
            ^
SyntaxError: Unexpected token ';'
"""


def test_failure_detection_covers_rust_python_and_node():
    assert failure_is_in_tests(RUST_TEST_BROKEN)
    assert not failure_is_in_tests(RUST_APP_BROKEN)
    assert failure_is_in_tests(PYTHON_TEST_SYNTAX)
    assert not failure_is_in_tests(PYTHON_APP_SYNTAX)
    assert failure_is_in_tests(NODE_TEST_SYNTAX)
    # A missing import in the application is not a broken test file.
    assert not failure_is_in_tests("ERROR collecting tests/test_calc.py\nE   ImportError: cannot import name 'add' from 'calc'\n")


CHECK = (
    "import sys\n"
    "from pathlib import Path\n"
    "text = Path('tests/gui_test.go').read_text()\n"
    "if 'BROKEN' in text:\n"
    "    print('FAIL\\tapp/tests [setup failed]\\n# app/tests\\ntests/gui_test.go:22:1: imports must appear before other declarations')\n"
    "    sys.exit(1)\n"
)


class BrokenTestsEngine:
    """The first test file is broken. `code` must never be asked to repair it; `tests` repairs it when given feedback."""

    def __init__(self):
        self.code_feedback = []
        self.test_feedback = []

    def document(self, stage, idea, context):
        if stage == "analysis":
            return "# Requirements\n\nFR-01: show a quote.\nNFR-01: start fast.\n"
        if stage == "design":
            return "# Design\n\n```mermaid\nflowchart LR\n A --> B\n```\n\n```mermaid\nsequenceDiagram\n A->>B: x\n```\n"
        if stage == "review":
            return "# Review\n\nVERDICT: APPROVE\n"
        return f"# {stage}\n"

    def code(self, idea, docs, feedback=None, change=None):
        self.code_feedback.append(feedback)
        return {"check.py": CHECK, "project.yaml": "unit_test: [python, check.py]\n"}

    def tests(self, idea, docs, files, change=None, feedback=None):
        self.test_feedback.append(feedback)
        return {"tests/gui_test.go": "// BROKEN FR-01\n" if feedback is None else "// fixed FR-01\n"}


def test_broken_test_files_go_back_to_the_test_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    engine = BrokenTestsEngine()
    result = run_project("Quote app", workspace=tmp_path, engine=engine,
                         decisions={"project_type": "cli", "stack": "Go", "delivery": "none", "scale": "small"})

    assert result.status == "completed", result.error
    assert result.attempts == 2
    assert engine.code_feedback == [None]  # the developer was never asked to fix the tests
    assert engine.test_feedback[0] is None and "tests/gui_test.go:22:1" in engine.test_feedback[1]
    events = {(e["stage"], e["status"]) for e in History(tmp_path / "runs" / "history.sqlite3").get(result.id)["events"]}
    assert {("testing", "tests_repair_started"), ("testing", "tests_repaired"), ("testing", "passed")} <= events
