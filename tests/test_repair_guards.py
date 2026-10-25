"""Guards around the repair loop: what the agents see, what they may overwrite and what an attempt is spent on."""

import json
import shutil
import socket

import pytest

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


def _write(root, files):
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")


def test_a_renamed_export_is_reported_with_what_the_module_exports(tmp_path):
    from autosdlc.runner import missing_js_imports

    _write(tmp_path, {
        "server/src/cache.js": "/* import { nope } from './x.js' */\nexport async function getCached() {}\nexport const ttl = 5;\n",
        "server/src/helpers.js": "// header\nimport { getUpcoming, ttl } from './cache.js';\nexport { getUpcoming as get };\n",
        "server/src/index.js": "import app, { createApp as make } from './app';\nimport pkg from 'express';\n",
        "server/src/app.js": "function createApp() {}\nexport { createApp };\nexport default createApp;\n",
        "server/src/all.js": "export * from './cache.js';\n",
        "server/src/user.js": "import { anything } from './all.js';\n",
        "tests/a.test.js": "import { missing } from '../server/src/cache.js';\n",
    })
    # The application's own problem comes first and alone; the test file's would only distract the developer.
    assert missing_js_imports(tmp_path) == [
        "server/src/helpers.js:2: imports 'getUpcoming' from './cache.js', but server/src/cache.js does not export it "
        "(it exports: getCached, ttl)"]
    (tmp_path / "server/src/helpers.js").write_text("import { ttl } from './cache.js';\n", encoding="utf-8")
    assert missing_js_imports(tmp_path) == [
        "tests/a.test.js:1: imports 'missing' from '../server/src/cache.js', but server/src/cache.js does not export it "
        "(it exports: getCached, ttl)"]


def test_a_package_installed_only_for_another_workspace_package_is_reported(tmp_path):
    from autosdlc.runner import missing_js_packages

    _write(tmp_path, {
        "node_modules/node-fetch/package.json": "{}",
        "node_modules/@scope/lib/package.json": "{}",
        "server/node_modules/express/package.json": "{}",
        "server/src/app.js": "import express from 'express';\nimport fs from 'node:fs';\nimport path from 'path';\n",
        "tests/helpers.js": "import fetch from 'node-fetch';\nimport x from '@scope/lib/sub';\n\nimport express from 'express';\n",
        "web/src/main.jsx": "import '@/styles.css';\nimport v from 'virtual:pwa';\n",
    })
    assert missing_js_packages(tmp_path) == [
        "tests/helpers.js:4: imports the package 'express', which is not installed where this file is loaded from; "
        "add it to the package.json that covers this file, or do not use it here"]
    # Before the dependencies are installed nothing can be told.
    assert missing_js_packages(tmp_path / "server" / "src") == []


def test_a_playwright_spec_run_by_node_test_is_reported(tmp_path):
    from autosdlc.runner import test_runner_mismatches

    _write(tmp_path, {
        "scripts/api-test.sh": "#!/bin/sh\nset -e\nnode --test --test-concurrency=1 tests/*.test.js\n",
        "tests/api.test.js": "import test from 'node:test';\n",
        "tests/ui.test.js": "import { test } from '@playwright/test';\n",
        "tests/ui/flow.spec.js": "import { test } from \"@playwright/test\";\n",
    })
    manifest = {"api_test": ["sh", "scripts/api-test.sh"], "ui_test": ["npx", "playwright", "test", "tests/ui"]}
    assert test_runner_mismatches(tmp_path, manifest) == [
        "tests/ui.test.js:1: uses @playwright/test but a `node --test` command in project.yaml runs it; write it "
        "with node:test (and Playwright's library API from 'playwright'), or move it where only a `playwright test` "
        "command runs it"]
    # A bare `node --test` runs every file with a test name, the spec under tests/ui too.
    assert len(test_runner_mismatches(tmp_path, {"unit_test": ["node", "--test"]})) == 2


def test_a_node_test_file_run_by_playwright_test_is_reported(tmp_path):
    from autosdlc.runner import test_runner_mismatches

    _write(tmp_path, {
        "scripts/ui-test.sh": "#!/bin/sh\nset -e\nplaywright install chromium\nplaywright test tests/ui\n",
        "tests/ui.test.js": "import test from 'node:test';\nimport { chromium } from '@playwright/test';\n",
        "tests/api.test.js": "import test from 'node:test';\n",
    })
    problems = test_runner_mismatches(tmp_path, {"ui_test": ["sh", "scripts/ui-test.sh"]})
    assert len(problems) == 1 and problems[0].startswith("tests/ui.test.js:1: is written with node:test")


class SuitesEngine(RepairEngine):
    """The unit tests fail and the API suite after them has its own failure; both are in the first attempt's output."""

    def __init__(self, workspace, port):
        super().__init__(workspace, [])
        self.port = port

    def code(self, idea, docs, feedback=None, change=None):
        if feedback is None:
            return {"calc.py": "def add(a, b):\n    return a - b\n", "server.py": ServerEngine.SERVER,
                    "project.yaml": f"port: {self.port}\nstart: [python, server.py]\n"
                                    "unit_test: [python, -m, pytest, -q, tests/unit]\n"
                                    "api_test: [python, -m, pytest, -q, tests/api]\n"}
        self.reasons.append(feedback)
        return {"calc.py": "def add(a, b):\n    return a + b\n"}

    def tests(self, idea, docs, files, change=None, feedback=None):
        return {"tests/unit/test_calc.py": "from calc import add\n\ndef test_fr01_add():\n    assert add(1, 2) == 3\n",
                "tests/api/test_api.py": "def test_fr01_api():\n    assert 'API SUITE RAN' == ''\n"}


class SlowFixEngine(RepairEngine):
    """Five functions, four of them wrong; every repair fixes one more (1, 2, 3, 4 and then all 5 tests pass)."""

    @staticmethod
    def source(fixed):
        return "".join(f"def f{n}():\n    return {n if n == 1 or n <= fixed else 0}\n\n" for n in range(1, 6))

    def code(self, idea, docs, feedback=None, change=None):
        if feedback is None:
            return {"calc.py": self.source(1), "project.yaml": "unit_test: [python, -m, pytest, -q, tests]\n"}
        self.reasons.append(feedback)
        return {"calc.py": self.source(len(self.reasons) + 1)}

    def tests(self, idea, docs, files, change=None, feedback=None):
        return {"tests/test_calc.py": "import calc\n\n" + "".join(
            f"def test_fr01_f{n}():\n    assert calc.f{n}() == {n}\n\n" for n in range(1, 6))}


def test_a_run_that_keeps_improving_earns_extra_attempts(tmp_path, monkeypatch):
    result, events = _run(tmp_path, monkeypatch, SlowFixEngine(tmp_path, []))
    assert result.status == "completed", result.error
    assert result.attempts == 5
    assert [detail for _stage, status, detail in events if status == "attempt_added"] == ["2 → 3 passed", "3 → 4 passed"]


def test_a_run_that_does_not_improve_gets_no_extra_attempts(tmp_path, monkeypatch):
    result, events = _run(tmp_path, monkeypatch, RepairEngine(tmp_path, []))
    assert result.status == "failed" and result.attempts == 3
    assert "attempt_added" not in [status for _stage, status, _detail in events]


class KeptTestsEngine(RepairEngine):
    """Counts the suites written; the kept suite has a marker a new suite would not have."""

    def __init__(self, workspace, repairs):
        super().__init__(workspace, repairs)
        self.suites = 0

    def tests(self, idea, docs, files, change=None, feedback=None):
        if feedback is None:
            self.suites += 1
        return {"tests/test_calc.py": f"from calc import add\n\n# suite {self.suites}\n"
                                      "def test_fr01_add():\n    assert add(1, 2) == 3\n"}


def test_a_retry_can_keep_the_tests_of_the_last_try(tmp_path, monkeypatch):
    engine = KeptTestsEngine(tmp_path, [])
    first, _events = _run(tmp_path, monkeypatch, engine)
    assert first.status == "failed" and engine.suites == 1
    db = History(tmp_path / "runs" / "history.sqlite3")
    assert db.reset_from(first.id, "testing")
    engine.repairs = [{"calc.py": "def add(a, b):\n    return a + b\n"}]
    again = run_project("", workspace=tmp_path, engine=engine, resume_run_id=first.id, keep_tests=True)
    assert again.status == "completed", again.error
    # No new suite was written: the kept file (suite 1) ran and the developer fixed the code.
    assert engine.suites == 1
    assert "# suite 1" in (again.project_dir / "tests" / "test_calc.py").read_text(encoding="utf-8")
    assert ("testing", "tests_kept", "1 test files") in [
        (e["stage"], e["status"], e["detail"]) for e in db.get(first.id)["events"]]


def test_a_stale_test_that_fails_the_build_before_the_new_suite_is_not_the_developers_to_fix(tmp_path, monkeypatch):
    from autosdlc.runner import RunSession

    stale = ("[build failed] import check: modules or packages that cannot be loaded as written\n"
             "tests/ui.test.js:1: is written with node:test but a `playwright test` command runs it")
    real = RunSession._compile_check
    calls = []

    def compile_check(self, env):
        calls.append(len(calls))
        # Only the check before the new tests are written (the first one) sees the earlier generation's file.
        return (stale, 1) if len(calls) == 1 else real(self, env)

    monkeypatch.setattr(RunSession, "_compile_check", compile_check)
    engine = RepairEngine(tmp_path, [{"calc.py": "def add(a, b):\n    return a + b\n"}])
    result, _events = _run(tmp_path, monkeypatch, engine)
    assert result.status == "completed", result.error
    # The developer was asked only once, for the failing test run, never for the stale test.
    assert len(engine.reasons) == 1 and "tests/ui.test.js" not in engine.reasons[0]


def test_a_failing_suite_does_not_hide_the_suites_after_it(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("PORT", str(port))
    engine = SuitesEngine(tmp_path, port)
    result, _events = _run(tmp_path, monkeypatch, engine, {
        "project_type": "backend", "stack": "Python", "database": "none", "cache": "none",
        "observability": "none", "ui": "none", "delivery": "none", "scale": "small"})
    attempt = (result.output_dir / "test_attempts" / "attempt-1.txt").read_text(encoding="utf-8")
    assert "### Unit tests (exit 1)" in attempt and "### API tests (exit 1)" in attempt
    assert "API SUITE RAN" in engine.reasons[0]


def test_a_long_output_keeps_its_start_and_its_end():
    from autosdlc.runner import _clip

    text = "FIRST FAILURE " + "x" * 50_000 + " SUMMARY"
    clipped = _clip(text, 20_000)
    assert clipped.startswith("FIRST FAILURE") and clipped.endswith("SUMMARY") and len(clipped) < 20_100
    assert _clip("short", 20_000) == "short"


TAP = """### API tests (exit 1)
# Subtest: events
    # Subtest: FR-02 creates an event
    ok 1 - FR-02 creates an event
      ---
      duration_ms: 5
      ...
    # Subtest: FR-02 missing title answers 400
    not ok 2 - FR-02 missing title answers 400
      ---
      location: 'tests/events.test.js:60:3'
      expected: 400
      actual: 201
      ...
not ok 1 - events
  ---
  error: '1 subtest failed'
  ...
""" + "ok 9 - padding\n" * 500 + """# tests 3
# pass 2
# fail 1
### Application server log
{"err":"INVALID_FILE"}
"""


def test_the_repair_sees_the_failing_tests_not_the_passing_ones():
    from autosdlc.runner import failing_test_names, failure_digest, passed_test_count

    digest = failure_digest(TAP)
    assert "expected: 400" in digest and "actual: 201" in digest and "tests/events.test.js:60:3" in digest
    assert "padding" not in digest and "duration_ms" not in digest
    assert "# pass 2" in digest and '{"err":"INVALID_FILE"}' in digest
    assert failure_digest("no tap here\n") == "no tap here\n"
    assert failing_test_names(TAP) == {"FR-02 missing title answers 400", "events"}
    assert passed_test_count(TAP) == 2
    assert passed_test_count("1 failed, 4 passed in 0.1s") == 4 and passed_test_count("crash") is None


AUTHORS = """### API tests (exit 1)
# Subtest: registrations
    not ok 1 - FR-04 registers
      ---
      location: '/p/tests/registrations.test.js:63:3'
      error: "Cannot read properties of undefined (reading 'token')"
      code: 'ERR_TEST_FAILURE'
      name: 'TypeError'
      stack: |-
        TestContext.<anonymous> (file:///p/tests/registrations.test.js:65:49)
        async Test.run (node:internal/test_runner/test:1054:7)
      ...
    not ok 2 - FR-03 full event
      ---
      location: '/p/tests/eventList.test.js:77:3'
      error: 'null value in column "event_id" of relation "registrations" violates not-null constraint'
      code: '23502'
      name: 'error'
      stack: |-
        /p/node_modules/.pnpm/pg-pool@3.14.0/node_modules/pg-pool/index.js:45:11
        process.processTicksAndRejections (node:internal/process/task_queues:103:5)
        async registerViaSql (file:///p/tests/helpers.js:125:3)
      ...
    not ok 3 - FR-02 uploads a cover
      ---
      location: '/p/tests/upload.test.js:37:3'
      error: |-
        Expected values to be strictly equal:
        400 !== 201
      code: 'ERR_ASSERTION'
      name: 'AssertionError'
      expected: 201
      actual: 400
      stack: |-
        TestContext.<anonymous> (file:///p/tests/upload.test.js:52:12)
      ...
    not ok 4 - FR-03 the server is down
      ---
      location: '/p/tests/eventList.test.js:90:3'
      error: 'fetch failed'
      name: 'TypeError'
      stack: |-
        node:internal/deps/undici/undici:13510:13
        async TestContext.<anonymous> (file:///p/tests/eventList.test.js:91:5)
      ...
not ok 1 - registrations
  ---
  error: '4 subtests failed'
  ...
not ok 2 - tests/organizerRegistrations.test.js
  ---
  location: '/p/tests/organizerRegistrations.test.js:1:1'
  failureType: 'testCodeFailure'
  exitCode: 1
  error: 'test failed'
  ...
# pass 30
# fail 5
### Application server log
{"err":"INVALID_FILE"}
"""


def test_failures_in_the_tests_own_code_go_to_the_test_agent():
    from autosdlc.runner import failures_by_author

    in_tests, in_app = failures_by_author(AUTHORS)
    # A TypeError thrown in a test, the test's own SQL and a test file that does not load are the tests'.
    assert "FR-04 registers" in in_tests and "FR-03 full event" in in_tests
    assert "organizerRegistrations.test.js" in in_tests
    # A wrong answer of the application and an unreachable server are the application's.
    assert "FR-02 uploads a cover" in in_app and "FR-03 the server is down" in in_app
    assert "FR-02 uploads a cover" not in in_tests and "FR-04 registers" not in in_app
    assert "INVALID_FILE" in in_app
    assert failures_by_author("crash without TAP\n") == ("", "crash without TAP\n")


def test_the_developer_sees_the_start_of_a_long_test_output():
    from autosdlc.crewai_engine import _ends

    text = "FAILING TEST " + "x" * 30_000 + " SERVER LOG"
    assert _ends(text, 14_000).startswith("FAILING TEST") and _ends(text, 14_000).endswith("SERVER LOG")


class RegressingEngine(RepairEngine):
    """The first repair fixes add() but breaks sub() and mul() and adds a file; it is rolled back, the second fixes add()."""

    GOOD = "def sub(a, b):\n    return a - b\n\ndef mul(a, b):\n    return a * b\n"

    def code(self, idea, docs, feedback=None, change=None):
        if feedback is None:
            return {"calc.py": "def add(a, b):\n    return a - b\n\n" + self.GOOD,
                    "project.yaml": "unit_test: [python, -m, pytest, -q, tests]\n"}
        self.reasons.append(feedback)
        if len(self.reasons) == 1:
            return {"calc.py": "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return 0\n\ndef mul(a, b):\n    return 0\n",
                    "extra.py": "x = 1\n"}
        return {"calc.py": "def add(a, b):\n    return a + b\n\n" + self.GOOD}

    def tests(self, idea, docs, files, change=None, feedback=None):
        return {"tests/test_calc.py": "from calc import add, sub, mul\n\n"
                                      "def test_fr01_add():\n    assert add(1, 2) == 3\n\n"
                                      "def test_fr01_sub():\n    assert sub(3, 2) == 1\n\n"
                                      "def test_fr01_mul():\n    assert mul(2, 3) == 6\n"}


def test_a_repair_that_breaks_passing_tests_is_rolled_back(tmp_path, monkeypatch):
    engine = RegressingEngine(tmp_path, [])
    result, events = _run(tmp_path, monkeypatch, engine)
    assert result.status == "completed", result.error
    assert result.attempts == 3
    assert ("development", "repair_reverted", "attempt 2: 1 passed, attempt 1: 2 passed") in events
    assert "YOUR LAST REPAIR WAS ROLLED BACK" in engine.reasons[1] and "test_fr01_sub" in engine.reasons[1]
    assert not (result.project_dir / "extra.py").exists()


def test_the_developer_can_report_a_wrong_test():
    from autosdlc.crewai_engine import _test_issues

    raw = json.dumps({"files": {}, "test_issues": {"tests/a.test.js": "calls /api/nope", "src/app.js": "no"}})
    assert _test_issues(raw) == {"tests/a.test.js": "calls /api/nope"}
    assert _test_issues('{"files": {}}') == {} and _test_issues("not json") == {}


class WrongTestEngine(RepairEngine):
    """The test asserts something the design does not ask for; the developer reports it and the Test Agent fixes it."""

    def code(self, idea, docs, feedback=None, change=None):
        self.test_issues = {}
        if feedback is None:
            return {"calc.py": "def add(a, b):\n    return a + b\n",
                    "project.yaml": "unit_test: [python, -m, pytest, -q, tests]\n"}
        self.reasons.append(feedback)
        self.test_issues = {"tests/test_calc.py": "expects add(1, 2) == 4, which FR-01 does not say"}
        return {}

    def tests(self, idea, docs, files, change=None, feedback=None):
        self.test_feedback = getattr(self, "test_feedback", []) + [feedback]
        expected = 4 if feedback is None else 3
        return {"tests/test_calc.py": f"from calc import add\n\ndef test_fr01_add():\n    assert add(1, 2) == {expected}\n"}


def test_a_test_the_developer_reports_as_wrong_goes_back_to_its_author(tmp_path, monkeypatch):
    engine = WrongTestEngine(tmp_path, [])
    result, events = _run(tmp_path, monkeypatch, engine)
    assert result.status == "completed", result.error
    assert result.attempts == 2
    assert ("development", "test_issues_reported", "tests/test_calc.py") in events
    assert "THE DEVELOPER REPORTS THESE TESTS AS WRONG" in engine.test_feedback[1]
    assert "which FR-01 does not say" in engine.test_feedback[1]


class ImportEngine(RepairEngine):
    """Node library: the first repair renames an export without updating its importer; the compile check sends it back."""

    def code(self, idea, docs, feedback=None, change=None):
        if feedback is None:
            return {"src/calc.js": "export function add(a, b) { return a - b; }\n",
                    "src/index.js": "import { add } from './calc.js';\nexport { add };\n",
                    "project.yaml": "unit_test: [node, --test, tests/calc.test.js]\n"}
        self.reasons.append(feedback)
        if len(self.reasons) == 1:
            return {"src/calc.js": "export function sum(a, b) { return a + b; }\n"}
        return {"src/calc.js": "export function add(a, b) { return a + b; }\n"}

    def tests(self, idea, docs, files, change=None, feedback=None):
        return {"tests/calc.test.js": "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
                                      "import { add } from '../src/index.js';\n\n"
                                      "test('FR-01 adds', () => { assert.equal(add(1, 2), 3); });\n"}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_a_repair_that_breaks_an_import_is_sent_back_before_the_next_attempt(tmp_path, monkeypatch):
    engine = ImportEngine(tmp_path, [])
    result, events = _run(tmp_path, monkeypatch, engine, {
        "project_type": "library", "stack": "Node.js", "delivery": "none", "scale": "small"})
    assert result.status == "completed", result.error
    assert result.attempts == 2
    assert "imports 'add' from './calc.js'" in engine.reasons[1]
    assert ("testing", "build_failed") in [(stage, status) for stage, status, _detail in events]


class BrokenTestsEngine(ImportEngine):
    """Two test files use await in a plain callback; both must reach the Test Agent in one compile-check round."""

    def code(self, idea, docs, feedback=None, change=None):
        if feedback is None:
            # "type": "module" makes `node --check` parse the files as ES modules, where await needs an async function.
            return {"src/calc.js": "export function add(a, b) { return a + b; }\n",
                    "package.json": '{"name": "calc", "type": "module"}\n',
                    "project.yaml": "unit_test: [node, --test, tests/a.test.js, tests/b.test.js]\n"}
        self.reasons.append(feedback)
        return {}

    def tests(self, idea, docs, files, change=None, feedback=None):
        self.test_feedback = getattr(self, "test_feedback", []) + [feedback]
        callback = "() => { await Promise.resolve(); }" if feedback is None else "async () => { await Promise.resolve(); }"
        return {f"tests/{name}.test.js": f"import test from 'node:test';\n\ntest('FR-01 {name}', {callback});\n"
                for name in ("a", "b")}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_every_test_file_that_does_not_parse_is_reported_in_one_round(tmp_path, monkeypatch):
    engine = BrokenTestsEngine(tmp_path, [])
    result, _events = _run(tmp_path, monkeypatch, engine, {
        "project_type": "library", "stack": "Node.js", "delivery": "none", "scale": "small"})
    assert result.status == "completed", result.error
    assert len(engine.test_feedback) == 2
    assert "tests/a.test.js" in engine.test_feedback[1] and "tests/b.test.js" in engine.test_feedback[1]


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
