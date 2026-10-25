"""SDLC stages, artifact creation, and the bounded test/repair loop."""

from __future__ import annotations

import os
import posixpath
import json
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import yaml

from .crewai_engine import ClarificationNeeded, CrewAIEngine
from .i18n import normalize, t
from .deployment import write_deployment_files
from .services import LocalServices
from .exports import is_compiled_binary, plain
from .files import MAX_FILE_BYTES, write_generated_files
from .intake import validate_decisions
from .project import read_project_manifest
from .sections import NO_CHANGE, NO_CHANGE_TOKENS, apply_patch
from .storage import History, local_time, now, stage_models as read_stage_models
from .traceability import build_matrix


SDLC_STAGES = ("planning", "analysis", "design", "review", "development", "testing", "deployment", "maintenance")
STAGE_FILES = {
    "planning": "planning.md", "analysis": "requirements.md", "design": "architecture.md",
    "review": "design_review.md", "deployment": "deployment.md", "maintenance": "maintenance_report.md",
}
MAX_REVIEW_ROUNDS = 2
# Protocol tokens are language neutral (English); the Turkish ones written by older runs are still accepted.
VERDICT = re.compile(r"(?:VERDICT|KARAR):\s*\**\s*(APPROVE|REVISE|ONAY|REV[İI]ZYON)", re.IGNORECASE)
# A "| 1 | Critical | …" row of the findings table; in a small project only this requires a design revision.
CRITICAL_FINDING = re.compile(r"^\s*\|[^|\n]*\|\s*\**\s*(?:Critical|Kritik)\b", re.IGNORECASE | re.MULTILINE)
# Maximum document length (characters) in a small project; the agent is told not to exceed it.
SMALL_DOC_LIMIT = 3500
SMALL_REVIEW_LIMIT = 2000
# Stages a change request may skip; development and testing always run.
SKIPPABLE_STAGES = ("analysis", "design", "review", "deployment", "maintenance")
# First line of deployment/maintenance documents; recognised in any language through the marker comment.
TEST_MARKER = "<!-- test-result -->"
TEST_HEADER = re.compile(r"\A> (?:[^\n]*" + re.escape(TEST_MARKER) + r"|Doğrulanan test sonucu:[^\n]*)\n+")
# Findings the reviewer must write with these exact English severity words in every language.
START_STAGE = re.compile(r"(?:START|BAŞLANGIÇ):\s*\**\s*(analysis|design|development)", re.IGNORECASE)
SKIP_LINE = re.compile(r"(?:SKIP|ATLA):\s*\**\s*([a-z, ]*)", re.IGNORECASE)
TEST_DIRS = ("tests/", "test/")
PYTHON_TOOLS = {"python", "python3", "pip", "pip3"}
DOC_BUDGET = 9000
# The earlier documents each stage reads, in order of importance. The engine truncates the context from the
# end, so the most important document comes first.
STAGE_INPUTS = {
    "planning": (),
    "analysis": ("planning",),
    "design": ("analysis", "planning"),
    "review": ("design", "analysis"),
    "development": ("design", "analysis", "review"),
    "testing": ("analysis", "design"),
    "deployment": ("design", "analysis"),
    "maintenance": ("analysis", "review", "deployment", "design"),
}
# Seconds the package command may run: release builds of GUI crates for several targets are slow on a small server.
PACKAGE_TIMEOUT = 1800
# Fix rounds the compile check may ask for before the test attempts; they do not count as test attempts.
BUILD_FIXES = 2
SERVER_START_FAILURES = ("The application server exited before the tests",
                         "The application server did not start within 60 seconds")
SERVER_START_REPAIR = "the application server did not start"
TESTS_REPORTED = "reported as wrong by the developer"
# Attempts beyond max_repairs + 1 a run earns by passing more tests after each repair.
EXTRA_ATTEMPTS = 2
TEST_CODE_NOTE = (
    "\n\nTHESE TESTS FAIL IN THEIR OWN CODE, not because of the application: an error thrown in a test file or helper "
    "(a value that is undefined), the test's own SQL, a test file that does not load, or a request the application "
    "rejected as invalid (the server log above says why, e.g. a required field the test did not send). Fix these "
    "test files; send what the design requires.\n"
)
REGRESSED_NOTE = (
    "\n\nYOUR LAST REPAIR BROKE THESE TESTS: they passed before it and their test files did not change:\n{tests}\n"
    "Restore the behaviour they check (look at what your last repair changed, e.g. a validation you loosened) "
    "while you fix the other failures; do not trade one passing test for another.\n"
)
REVERTED_NOTE = (
    "\n\nYOUR LAST REPAIR WAS ROLLED BACK: with it {count} tests passed instead of {best}. It broke: {broken}. The output "
    "above is from the restored code. Change only what the failing tests above need, and keep the code the passing "
    "tests rely on.\n"
)


@dataclass
class RunResult:
    id: str
    status: str
    output_dir: Path
    project_dir: Path
    attempts: int
    error: str | None = None


class RunPaused(Exception):
    """The job was paused before moving on to the next SDLC stage."""


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return plain(path.read_text(encoding="utf-8", errors="replace"))[-limit:]
    except OSError:
        return ""


def _clip(text: str, limit: int) -> str:
    """The start and the end of a long output: test runners report the first failures in detail at the top and the
    summary at the bottom, so keeping only the end lost what failed first."""
    if len(text) <= limit:
        return text
    head = limit * 3 // 10
    return f"{text[:head]}\n[... {len(text) - limit} characters left out ...]\n{text[-(limit - head):]}"


_TAP_NOT_OK = re.compile(r"^(\s*)not ok \d+ - (.+?)\s*$")
_TAP_COUNT = re.compile(r"^# (?:tests|suites|pass|fail|cancelled|skipped|todo) \d+\s*$")


def failure_digest(output: str, keep_failure: Callable[[list[str]], bool] = lambda block: True) -> str:
    """The failing tests of a node:test (TAP) output with their errors, the counts and the logs; the output unchanged
    when it has no failing TAP test. The passing tests filled most of the output, and a repair that saw only its
    end saw the server log but not which tests failed. keep_failure picks the failure blocks that are kept."""
    lines = output.splitlines()
    keep: list[str] = []
    found = in_log = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("### "):
            in_log = "log" in line.lower()
            keep.append(line)
        elif in_log or _TAP_COUNT.match(line):
            keep.append(line)
        elif match := _TAP_NOT_OK.match(line):
            found = True
            end = i
            # The YAML block of a failure (error, expected, actual, location) ends with "..." one level deeper.
            if i + 1 < len(lines) and lines[i + 1].strip() == "---":
                closing = " " * (len(match.group(1)) + 2) + "..."
                end = next((j for j in range(i + 2, min(len(lines), i + 120)) if lines[j].rstrip() == closing), i)
            if keep_failure(lines[i:end + 1]):
                keep.extend(lines[i:end + 1])
            i = end
        i += 1
    return "\n".join(keep) + "\n" if found else output


_TAP_FIELD = re.compile(r"^\s+(name|code|error|location|expected|actual): ['\"]?(.*?)['\"]?\s*$")
_STACK_FRAME = re.compile(r"(?:^|[\s(])(?:file://)?(/[^\s():]+|[A-Za-z]:[^\s():]+):\d+:\d+")


def failure_in_test_code(block: list[str]) -> bool:
    """A failure the test's own code caused, which only its author can fix: a TypeError or ReferenceError thrown in a
    test file (a helper that returned undefined), a database error from the test's own SQL, or a test file that did
    not even load. An assertion that the application answered wrongly is the application's.

    Seven tests that read `.token` of undefined went to the developer three times, who rewrote the application."""
    fields = {match.group(1): match.group(2) for line in block if (match := _TAP_FIELD.match(line))}
    frames = [path for line in block if not line.strip().startswith("location:")
              for path in _STACK_FRAME.findall(line) if "node_modules" not in path]
    # A stack cut where a long output was clipped falls back to where the test is.
    thrown_in_tests = _in_tests(frames[0]) if frames else _in_tests(fields.get("location", ""))
    if fields.get("location", "").endswith(":1:1") and fields.get("error") == "test failed":
        return _in_tests(fields["location"])  # the whole file failed: it did not load or crashed outside a test
    if fields.get("name") in {"TypeError", "ReferenceError", "SyntaxError", "RangeError"}:
        return thrown_in_tests and "fetch failed" not in fields.get("error", "")
    # node-postgres errors carry the 5-character SQLSTATE as their code; only the test's own queries count here.
    if fields.get("name") == "error" and re.fullmatch(r"[0-9A-Z]{5}", fields.get("code", "")):
        return any(_in_tests(path) for path in frames)
    # A request the test expected to succeed was rejected as invalid: the test sent data the validation refuses (six
    # events were created without a date, and the developer could not change anything about it). The server log
    # with the rejection reason goes along.
    if fields.get("name") == "AssertionError" and fields.get("expected") in {"200", "201", "204"}:
        return fields.get("actual") in {"400", "422"}
    return False


def _leaf_failure(block: list[str]) -> bool:
    # A suite's own block only says how many of its subtests failed; the subtests are reported on their own.
    return not any(re.search(r"error: '\d+ subtests? failed'", line) for line in block)


def failures_by_author(output: str, regressed: set[str] = frozenset()) -> tuple[str, str]:
    """(the failures in the tests' own code, the other failures) as digests of the output; "" for a side without a
    failure. An output without TAP failures is the application's as a whole. A regressed test (it passed before the
    last code repair, and its file did not change) is the application's whatever its error looks like."""
    counts = {True: 0, False: 0}

    def side(block: list[str]) -> bool:
        match = _TAP_NOT_OK.match(block[0])
        return not (match and match.group(2) in regressed) and failure_in_test_code(block)

    def in_tests(block: list[str]) -> bool:
        if not _leaf_failure(block):
            return False
        result = side(block)
        counts[result] += 1
        return result

    tests = failure_digest(output, in_tests)
    if not counts[True] and not counts[False]:
        return "", failure_digest(output)
    app = failure_digest(output, lambda block: not (_leaf_failure(block) and side(block)))
    return (tests if counts[True] else ""), (app if counts[False] else "")


_TAP_OK = re.compile(r"^\s*ok \d+ - (.+?)\s*$")
_TAP_LOCATION = re.compile(r"^\s+location: ['\"]?(.*?):\d+:\d+['\"]?\s*$")


def _test_file(location: str) -> str:
    """tests/x.test.js from an absolute location such as /data/projects/app/tests/x.test.js."""
    location = re.sub(r"[\\/]+", "/", location)  # TAP escapes Windows separators as \\
    for folder in TEST_DIRS:
        if location.startswith(folder):
            return location
        if "/" + folder in location:
            return folder + location.split("/" + folder, 1)[1]
    return location


def regressed_tests(before: str, after: str, tests_before: dict[str, str], tests_after: dict[str, str]) -> list[str]:
    """Tests that passed in the attempt before and fail now although their test file did not change: the code repair
    in between broke them. A repair once loosened the upload checks to make other tests pass, and the tests that
    rejected a GIF, a PDF and a too large file failed from then on while the total stayed the same."""
    passed = {match.group(1) for line in before.splitlines() if (match := _TAP_OK.match(line))}
    found: list[str] = []

    def collect(block: list[str]) -> bool:
        match = _TAP_NOT_OK.match(block[0])
        location = next((m.group(1) for line in block if (m := _TAP_LOCATION.match(line))), "")
        name = _test_file(location)
        if (match and _leaf_failure(block) and match.group(2) in passed and name
                and tests_before.get(name) is not None and tests_before.get(name) == tests_after.get(name)):
            found.append(match.group(2))
        return False

    failure_digest(after, collect)
    return found


def failing_test_names(output: str) -> set[str]:
    return {match.group(2) for line in output.splitlines() if (match := _TAP_NOT_OK.match(line))} | set(
        re.findall(r"^FAILED (\S+)", output, re.M))


def passed_test_count(output: str) -> int | None:
    """How many tests passed (node:test, pytest, jest, cargo, Playwright summaries); None when the output says nothing."""
    counts = [int(n) for n in re.findall(r"^# pass (\d+)\s*$", output, re.M)]
    counts += [int(n) for n in re.findall(r"\b(\d+) passed\b", output)]
    return sum(counts) if counts else None


def _stop_process(process: subprocess.Popen) -> None:
    """Stops the server together with its child processes; otherwise the port may stay open."""
    if process.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=15, check=False)
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_logged(command: list[str], cwd: Path, env: dict[str, str], log: Path, timeout: int,
                should_stop: Callable[[], bool] | None = None) -> tuple[str, int | None]:
    """Runs a build command with its output in a file; the code is None when it could not start or timed out.

    The command runs in its own process group: on a timeout the compilers it started are stopped too, and nothing
    waits for pipes that a leftover child still holds open (cargo -> rustc, sh build.sh -> go build).
    When should_stop turns true (the user pressed Stop) the command is stopped and RunPaused is raised.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != "nt")
    except OSError as exc:
        return str(exc), None
    deadline = time.monotonic() + timeout
    while True:
        try:
            code = process.wait(timeout=0.5)
            return _tail(log, 20_000), code
        except subprocess.TimeoutExpired:
            if should_stop is not None and should_stop():
                _stop_process(process)
                raise RunPaused() from None
            if time.monotonic() > deadline:
                _stop_process(process)
                return f"timed out after {timeout} seconds\n{_tail(log, 2500)}", None


def with_display(command: list[str], env: dict[str, str], platform: str = sys.platform) -> list[str]:
    """On a Linux server without a screen the test command runs under a virtual display (xvfb-run), so a desktop
    app's test that opens a window anyway fails on its own merits, not with "no display"."""
    if not platform.startswith("linux") or env.get("DISPLAY"):
        return command
    xvfb_run = shutil.which("xvfb-run", path=env.get("PATH"))
    return [xvfb_run, "-a", *command] if xvfb_run else command


def _project_files(project_dir: Path, suffixes: set[str]) -> list[str]:
    """The project's own source files with these suffixes; dependencies, build outputs and hidden folders are left out."""
    found = []
    for path in sorted(project_dir.rglob("*")):
        relative = path.relative_to(project_dir)
        folders = relative.parts[:-1]
        if path.suffix in suffixes and path.is_file() and not any(part in SNAPSHOT_SKIP or part.startswith(".") for part in folders):
            found.append(relative.as_posix())
    return found


NODE_SCRIPTS = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}
# The file names Node's test runner picks up on its own: x.test.js, x-test.js, x_test.js, x.spec.js, test-x.js, test.js.
NODE_TEST_NAME = re.compile(r"(?:[._-]test|\.spec)\.[cm]?[jt]s$|^test(?:[-_].+)?\.[cm]?[jt]s$")


def expand_node_test_folders(args: list[str], project_dir: Path) -> list[str]:
    """`node --test tests/api` fails with "Cannot find module": Node 22 runs a folder argument as a file. A folder is
    replaced with its test files (with every script in it when none has a test name), found when the command runs,
    so tests added later run too."""
    expanded = []
    for arg in args:
        folder = project_dir / arg
        if arg.startswith("-") or not folder.is_dir():
            expanded.append(arg)
            continue
        scripts = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix in NODE_SCRIPTS
                         and "node_modules" not in path.relative_to(folder).parts)
        named = [path for path in scripts if NODE_TEST_NAME.search(path.name)]
        expanded.extend(path.relative_to(project_dir).as_posix() for path in named or scripts)
        if not scripts:
            expanded.append(arg)
    return expanded


def compile_commands(project_dir: Path) -> list[list[str]]:
    """Commands that only build or parse the project and its tests, without running them. Rust and Go reuse the build
    cache for the test run that follows; for Python, Node.js and shell this is a syntax check."""
    commands = []
    if (project_dir / "Cargo.toml").is_file():
        # --all-features: a GUI behind an optional feature is otherwise compiled only by the release build.
        commands.append(["cargo", "test", "--no-run", "--all-features"])
    if (project_dir / "go.mod").is_file():
        commands.append(["go", "test", "-count=1", "-run", "^$", "./..."])
    tsc = project_dir / "node_modules" / ".bin" / "tsc"
    if (project_dir / "tsconfig.json").is_file() and (tsc.is_file() or tsc.with_suffix(".cmd").is_file()):
        commands.append(["node_modules/.bin/tsc", "--noEmit"])
    else:
        commands += [["node", "--check", name] for name in _project_files(project_dir, {".js", ".mjs", ".cjs"})]
    python_files = _project_files(project_dir, {".py"})
    if python_files:
        commands.append(["python", "-m", "py_compile", *python_files])
    commands += [["sh", "-n", name] for name in _project_files(project_dir, {".sh"})]
    return commands


_JS_SOURCES = {".js", ".mjs", ".jsx"}
_JS_COMMENTS = re.compile(r"/\*.*?\*/|^\s*//[^\n]*", re.DOTALL | re.MULTILINE)
# `import { a, b as c } from './x.js'` (with an optional default before the braces) and `export { a } from './x.js'`.
_JS_NAMED_IMPORT = re.compile(r"\b(?:import\s+(?:[\w$]+\s*,\s*)?|export\s*)\{([^}]*)\}\s*from\s*['\"](\.{1,2}/[^'\"]+)['\"]")
_JS_EXPORT_DECLARATION = re.compile(r"\bexport\s+(?:async\s+)?(?:function\s*\*?|class|const|let|var)\s*([\w$]+)")
_JS_EXPORT_LIST = re.compile(r"\bexport\s*\{([^}]*)\}")


def _js_names(clause: str, exported: bool) -> list[str]:
    """Names in an import/export brace list: `a as b` is a when imported and b when exported."""
    names = []
    for item in clause.split(","):
        parts = item.split()
        if parts and parts[0] != "type":
            names.append(parts[-1] if exported and len(parts) == 3 and parts[1] == "as" else parts[0])
    return names


def _js_exports(source: str) -> set[str] | None:
    """What a module exports by name; None when that cannot be told statically (`export *`, destructured exports)."""
    if re.search(r"\bexport\s*\*|\bexport\s+(?:const|let|var)\s*[{\[]", source):
        return None
    names = set(_JS_EXPORT_DECLARATION.findall(source))
    for clause in _JS_EXPORT_LIST.findall(source):
        names.update(_js_names(clause, exported=True))
    if re.search(r"\bexport\s+default\b", source):
        names.add("default")
    return names


def missing_js_imports(project_dir: Path) -> list[str]:
    """Relative named imports whose target module does not export the name.

    `node --check` only parses a file, so a repair that renamed an export without updating its importers was found
    only when the server crashed at start, using up a test attempt each time. The application's files are reported
    first: the tests cannot be fixed by the developer, and a broken application would hide what the tests expect.
    """
    sources = {}
    for name in _project_files(project_dir, _JS_SOURCES):
        try:
            # Comments are blanked, keeping their line breaks so the reported line numbers stay right.
            sources[name] = _JS_COMMENTS.sub(lambda match: "\n" * match.group().count("\n"),
                                             (project_dir / name).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    problems: list[str] = []
    for name, source in sources.items():
        for clause, specifier in _JS_NAMED_IMPORT.findall(source):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(name), specifier))
            target = next((candidate for candidate in (base, *(base + suffix for suffix in _JS_SOURCES),
                                                      *(f"{base}/index{suffix}" for suffix in _JS_SOURCES))
                           if candidate in sources), None)
            exports = _js_exports(sources[target]) if target else None
            if exports is None:
                continue  # not a project JS file (a .ts/.json module, a package) or not checkable
            line = source[:source.find(clause)].count("\n") + 1
            for imported in _js_names(clause, exported=False):
                if imported not in exports:
                    problems.append(f"{name}:{line}: imports '{imported}' from '{specifier}', but {target} does not "
                                    f"export it (it exports: {', '.join(sorted(exports)) or 'nothing'})")
    in_app = [problem for problem in problems if not _in_tests(problem.split(":", 1)[0])]
    return in_app or problems


# `import x from 'pkg'`, `import 'pkg'`, `import('pkg')`: a bare specifier names a package, not a file.
_JS_PACKAGE_IMPORT = re.compile(r"(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)['\"]([^'\"./#~][^'\"]*)['\"]")
NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants", "crypto", "dgram",
    "diagnostics_channel", "dns", "domain", "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring", "readline", "repl", "stream", "string_decoder",
    "sys", "test", "timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
}


def _closing(text: str, start: int) -> int:
    """The index of the bracket that closes the one at start; -1 when it is not closed."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack = []
    quote = ""
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if char == "\\":
                continue
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return index
    return -1


def _top_level_items(text: str) -> list[str]:
    """The comma separated items of a parameter list or object literal, without descending into nested brackets."""
    items, depth, current, quote = [], 0, "", ""
    for char in text:
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        items.append(current.strip())
    return items


def _object_keys(literal: str) -> set[str] | None:
    """The keys an object pattern or literal names ({ a, b = 1, c: d } → a, b, c); None with a ...spread."""
    keys = set()
    for item in _top_level_items(literal.strip()[1:-1]):
        if item.startswith("..."):
            return None
        match = re.match(r"['\"]?([\w$]+)['\"]?", item)
        if match:
            keys.add(match.group(1))
    return keys


_JS_EXPORTED_FUNCTION = re.compile(r"\bexport\s+(?:async\s+)?function\s+([\w$]+)\s*\(")


def test_helper_misuse(project_dir: Path) -> list[str]:
    """Calls of the tests' own helpers that cannot work, found without running them: an option the helper does not
    read (tests passed `cover` to a helper that reads `coverImage`, so every upload test sent the default PNG), and
    `.body.x` read from a helper that returns the raw fetch Response, whose body is a stream (`created.body.id` was
    never set, and five test files failed on it)."""
    sources = {name: _source(project_dir, name) for name in _project_files(project_dir, _JS_SOURCES)
               if _in_tests(name)}
    helpers: dict[str, tuple[int, set[str] | None, bool]] = {}
    for source in sources.values():
        for match in _JS_EXPORTED_FUNCTION.finditer(source):
            params_end = _closing(source, match.end() - 1)
            body_start = source.find("{", params_end)
            body_end = _closing(source, body_start) if params_end > 0 and body_start > 0 else -1
            if body_end < 0:
                continue
            params = _top_level_items(source[match.end():params_end])
            options = [(index, item) for index, item in enumerate(params) if item.startswith("{")]
            pattern = options[-1] if options else None
            # `{ a, b = f() } = {}`: the pattern ends at its own closing brace, before a default for the whole object.
            keys = _object_keys(pattern[1][:_closing(pattern[1], 0) + 1]) if pattern else None
            body = source[body_start:body_end]
            returns_response = bool(re.search(r"\breturn\s+(?:await\s+)?fetch\s*\(", body))
            helpers[match.group(1)] = (pattern[0] if pattern else -1, keys, returns_response)
    problems = []
    for name, source in sorted(sources.items()):
        for helper, (position, keys, returns_response) in helpers.items():
            for call in re.finditer(rf"(?<![\w$.]){re.escape(helper)}\s*\(", source):
                if source[max(0, call.start() - 9):call.start()].rstrip().endswith("function"):
                    continue  # the definition itself
                end = _closing(source, call.end() - 1)
                args = _top_level_items(source[call.end():end]) if end > 0 else []
                line = source.count("\n", 0, call.start()) + 1
                if keys is not None and 0 <= position < len(args) and args[position].startswith("{"):
                    unknown = sorted((_object_keys(args[position]) or set()) - keys)
                    if unknown:
                        problems.append(f"{name}:{line}: passes {', '.join(unknown)} to {helper}(), which reads only "
                                        f"{', '.join(sorted(keys))}; the value is ignored")
            if returns_response:
                for assigned in re.finditer(rf"(?:const|let|var)\s+([\w$]+)\s*=\s*await\s+{re.escape(helper)}\s*\(",
                                            source):
                    variable = assigned.group(1)
                    use = re.search(rf"\b{re.escape(variable)}\.body\s*(?:\?\.|\.)\s*[\w$]", source[assigned.end():])
                    if use:
                        line = source.count("\n", 0, assigned.end() + use.start()) + 1
                        problems.append(f"{name}:{line}: reads {variable}.body.… but {helper}() returns the fetch "
                                        "Response, whose body is a stream; parse it (await res.json()) in the helper "
                                        "and return { status, body }")
                        break
    return problems


def missing_js_packages(project_dir: Path) -> list[str]:
    """Packages a JS file imports that are not installed where Node looks for them from that file.

    In a workspace a test at the root cannot load a package installed only for server/ ("Cannot find package
    'express'"), and every test file failed that way. Checked only once the dependencies are installed.
    """
    if not (project_dir / "node_modules").is_dir():
        return []
    problems = []
    for name in _project_files(project_dir, _JS_SOURCES):
        try:
            source = _JS_COMMENTS.sub(lambda match: "\n" * match.group().count("\n"),
                                      (project_dir / name).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        for match in _JS_PACKAGE_IMPORT.finditer(source):
            specifier = match.group(1)
            if ":" in specifier or specifier.startswith("@/"):
                continue  # node:fs, virtual: modules, bundler aliases
            parts = specifier.split("/")
            package = "/".join(parts[:2]) if specifier.startswith("@") else parts[0]
            if package in NODE_BUILTINS:
                continue
            folder = (project_dir / name).parent
            while not (folder / "node_modules" / package).exists():
                if folder == project_dir:
                    line = source[:match.start()].count("\n") + 1
                    problems.append(f"{name}:{line}: imports the package '{package}', which is not installed where "
                                    "this file is loaded from; add it to the package.json that covers this file, "
                                    "or do not use it here")
                    break
                folder = folder.parent
    return problems


_NODE_TEST_CALL = re.compile(r"\bnode\b[^\n;&|]*?--test\b([^\n;&|]*)")
_PLAYWRIGHT_TEST_CALL = re.compile(r"\bplaywright\s+test\b([^\n;&|]*)")


def test_runner_mismatches(project_dir: Path, manifest: dict) -> list[str]:
    """Test files written for another runner than the project.yaml command that runs them.

    A Playwright Test spec run by `node --test` throws "did not expect test() to be called here" (this repeated in
    three runs in a row). A node:test file run by `playwright test` finds "No tests found", and Playwright closes the
    browser under the node:test tests while they run."""
    commands = [manifest.get(key) or [] for key in ("unit_test", "api_test", "ui_test")]
    texts = [" ".join(command) for command in commands]
    for arg in (arg for command in commands for arg in command):
        if arg.endswith((".sh", ".bash")) and (project_dir / arg).is_file():
            texts.append((project_dir / arg).read_text(encoding="utf-8", errors="replace"))
    scripts = _project_files(project_dir, NODE_SCRIPTS)
    return _playwright_specs_run_by_node(project_dir, texts, scripts) + _node_tests_run_by_playwright(
        project_dir, texts, scripts)


def _source(project_dir: Path, name: str) -> str:
    try:
        return (project_dir / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _node_tests_run_by_playwright(project_dir: Path, texts: list[str], scripts: list[str]) -> list[str]:
    # Playwright Test picks *.test.* / *.spec.* files; its arguments are regular expressions matched against the paths.
    candidates = [name for name in scripts if re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", name)]
    run: set[str] = set()
    for text in texts:
        for match in _PLAYWRIGHT_TEST_CALL.finditer(text):
            filters = [token.strip("'\"") for token in match.group(1).split() if not token.startswith("-")]
            for name in candidates:
                try:
                    if not filters or any(re.search(pattern, name) for pattern in filters):
                        run.add(name)
                except re.error:
                    continue
    return [f"{name}:1: is written with node:test but a `playwright test` command in project.yaml runs it (\"No tests "
            "found\", and Playwright closes the browser under it); write it with @playwright/test's test() and expect()"
            for name in sorted(run) if re.search(r"['\"]node:test['\"]", _source(project_dir, name))]


def _playwright_specs_run_by_node(project_dir: Path, texts: list[str], scripts: list[str]) -> list[str]:
    run: set[str] = set()
    for text in texts:
        for match in _NODE_TEST_CALL.finditer(text):
            patterns = [token.strip("'\"") for token in match.group(1).split() if not token.startswith("-")]
            if not patterns:
                run.update(name for name in scripts if NODE_TEST_NAME.search(name.rsplit("/", 1)[-1]))
            for pattern in patterns:
                try:
                    if (project_dir / pattern).is_dir():
                        run.update(name for name in scripts if name.startswith(pattern.rstrip("/") + "/"))
                    else:
                        run.update(path.relative_to(project_dir).as_posix() for path in project_dir.glob(pattern)
                                   if path.is_file())
                except (ValueError, NotImplementedError, OSError):
                    continue  # an absolute path or a shell variable, not a pattern of this project
    problems = []
    for name in sorted(run):
        if re.search(r"['\"]@playwright/test['\"]", _source(project_dir, name)):
            problems.append(f"{name}:1: uses @playwright/test but a `node --test` command in project.yaml runs it; "
                            "write it with node:test (and Playwright's library API from 'playwright'), or move it where "
                            "only a `playwright test` command runs it")
    return problems


# Lines of the impact analysis's open-questions section: the request is not applied until the user answers them.
OPEN_QUESTION = re.compile(r"^\s*(?:[-*]|\d+[.)])?\s*\**\s*(?:QUESTION|SORU)\s*\**\s*:\s*\**\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def open_questions(impact: str) -> list[str]:
    """The 'QUESTION: …' lines of the impact analysis; 'none' answers do not count."""
    found = [match.group(1).strip() for match in OPEN_QUESTION.finditer(impact)]
    return [question for question in found if question.strip(" .").casefold() not in {"yok", "none", "no", "n/a", "-"}][:5]


def test_placement_problem(tests: dict[str, str]) -> str | None:
    """The Test Agent may only write under tests/ or test/; returns the reason if that is violated."""
    outside = sorted(name for name in tests if not name.startswith(TEST_DIRS))
    if not tests or len(outside) == len(tests):
        return "Test files must be under tests/ or test/" + (f" (received: {', '.join(outside)})" if outside else "")
    if outside:
        return f"The Test Agent may only write under tests/ or test/ (outside: {', '.join(outside)})"
    # A test that wrote a "larger than 5 MB" upload out literally made a file far over the limit and ended the run.
    large = sorted(name for name, content in tests.items() if len(content.encode("utf-8")) > MAX_FILE_BYTES)
    if large:
        return (f"These test files are larger than {MAX_FILE_BYTES // 1000} KB: {', '.join(large)}; generate large "
                "test data in code (e.g. Buffer.alloc(6 * 1024 * 1024)) instead of writing it out")
    return None


PATH_RUNNERS = {"pytest", "node", "jest", "mocha", "vitest"}


def uncovered_tests(manifest: dict, test_names: list[str]) -> list[str]:
    """Test files that the test commands in project.yaml do not run.

    If the commands do not name a test file/folder (e.g. "npm test") the coverage is unknowable and an empty list is returned.
    """
    # Only runners that take a file path are examined; the "test" in "npm test" or "go test" is a subcommand.
    commands = [manifest.get(key) or [] for key in ("unit_test", "api_test", "ui_test")]
    targets = [arg.rstrip("/") for command in commands
               if any(Path(token).name in PATH_RUNNERS for token in command[:3])
               for arg in command[1:] if arg.rstrip("/") in {"tests", "test"} or arg.startswith(TEST_DIRS)]
    if not targets:
        return []
    return sorted(name for name in test_names
                  if name.endswith((".py", ".js", ".ts", ".mjs", ".go", ".rs", ".java"))
                  and not any(name == target or name.startswith(target + "/") for target in targets))


def backup_versions(output_dir: Path, filename: str) -> None:
    """Copies the document's current version under versions/ with a timestamp."""
    source = output_dir / filename
    if source.is_file():
        versions = output_dir / "versions"
        versions.mkdir(exist_ok=True)
        stamp = local_time().strftime("%Y%m%d-%H%M%S")
        target = versions / f"{source.stem}.{stamp}{source.suffix}"
        counter = 1
        while target.exists():
            target = versions / f"{source.stem}.{stamp}-{counter}{source.suffix}"
            counter += 1
        shutil.copy2(source, target)


def review_verdict(report: str) -> str | None:
    match = VERDICT.search(report)
    if match is None:
        return None
    return "APPROVE" if match.group(1).upper() in {"APPROVE", "ONAY"} else "REVISE"


def parse_start_stage(report: str) -> str | None:
    """The stage named in the impact analysis's 'START: …' line."""
    found = START_STAGE.search(report)
    return found.group(1).lower() if found else None


def suggested_skips(report: str) -> list[str]:
    """The skippable stages named in the impact analysis's 'SKIP: deployment, maintenance' line."""
    found = SKIP_LINE.search(report)
    names = [name.strip().lower() for name in found.group(1).split(",")] if found else []
    return [name for name in SKIPPABLE_STAGES if name in names]


def validate_stage_models(stage_models: dict[str, str] | None) -> dict[str, str]:
    """Optional model per SDLC stage; empty choices are dropped, so those stages use the run's model."""
    result = {}
    for stage, model in (stage_models or {}).items():
        if stage not in SDLC_STAGES:
            raise ValueError(f"Unknown stage for a model choice: {stage}")
        model = str(model or "").strip()
        if len(model) > 100:
            raise ValueError("A model id must be at most 100 characters")
        if model:
            result[stage] = model
    return result


def skip_stages(change: dict | None) -> set[str]:
    """The unaffected stages (to be skipped) that the user approved for a change request."""
    if not change or not change.get("skip_stages"):
        return set()
    return {name.strip() for name in change["skip_stages"].split(",")} & set(SKIPPABLE_STAGES)


def completed_from_events(events: list[dict]) -> set[str]:
    """Replays the events in order; a 'reset' event makes that stage and the ones after it need regeneration."""
    completed: set[str] = set()
    for event in events:
        if event["stage"] == "workflow" and event["status"] == "reset" and event["detail"] in SDLC_STAGES:
            completed -= set(SDLC_STAGES[SDLC_STAGES.index(event["detail"]):])
        elif event["status"] in {"completed", "skipped"} or (event["stage"] == "testing" and event["status"] == "passed"):
            completed.add(event["stage"])
    return completed


def default_project_dir(workspace: Path, idea: str, run_id: str) -> Path:
    normalized = unicodedata.normalize("NFKD", idea.lower().replace("ı", "i"))
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:48].rstrip("-") or "project"
    return workspace / "projects" / f"{slug}-{run_id}"


# A test run that stopped before any test ran because the test files themselves could not be built or loaded:
# Go "[build failed]", Rust "could not compile", a Python/Node SyntaxError.
_TEST_BUILD_FAILED = re.compile(
    r"\[(?:build|setup) failed\]|error: could not compile|^\s*(?:E\s+)?(?:Syntax|Indentation)Error\b", re.MULTILINE)
# Where an error is reported: go/gcc/tsc "file:line:col: msg", rust "--> file:line:col", a python traceback
# `File "x", line N`, node's bare "file:line" header.
_ERROR_LOCATION = re.compile(
    r"^\s*([^\s:\"]+\.\w+):\d+(?::\d+)?: "
    r"|^\s*--> ([^\s:]+\.\w+):\d+:\d+"
    r"|^\s*(?:E\s+)?File \"([^\"]+)\", line \d+"
    r"|^\s*((?:[A-Za-z]:)?/?[^\s:\"]+\.\w+):\d+\s*$",
    re.MULTILINE,
)


_GO_REQUIRE_BLOCK = re.compile(r"^require\s*\([^)]*\)[ \t]*\n?", re.MULTILINE)
_GO_REQUIRE_LINE = re.compile(r"^require\s+\S+\s+\S+[^\n]*\n?", re.MULTILINE)


def strip_go_requires(go_mod: str) -> str:
    """go.mod without its require entries: versions a model writes from memory are invented (an unknown pseudo-version
    breaks the build), while `go mod tidy` resolves the real ones from the imports."""
    return _GO_REQUIRE_LINE.sub("", _GO_REQUIRE_BLOCK.sub("", go_mod))


def _in_tests(name: str) -> bool:
    name = name.replace("\\", "/")
    return name.startswith(TEST_DIRS) or any("/" + folder in name for folder in TEST_DIRS)


def failure_is_in_tests(output: str) -> bool:
    """True when the tests could not even be built or loaded and every reported error location is inside tests/.

    Such a failure cannot be fixed by the developer (tests are protected), so the Test Agent repairs its own files.
    """
    if not _TEST_BUILD_FAILED.search(output):
        return False
    locations = [name for groups in _ERROR_LOCATION.findall(output) for name in groups if name]
    return bool(locations) and all(_in_tests(name) for name in locations)


# Folders left out of a version snapshot because they can be recreated or are large.
SNAPSHOT_SKIP = (".autosdlc-venv", ".venv", "node_modules", ".git", "__pycache__", ".pytest_cache",
                 "test-results", "playwright-report", "dist", "build", "target")


def _snapshot_ignore(directory: str, names: list[str]) -> set[str]:
    """Rebuildable folders and compiled binaries (e.g. a `go build` output at the root) stay out of a snapshot."""
    skipped = set(shutil.ignore_patterns(*SNAPSHOT_SKIP)(directory, names))
    return skipped | {name for name in names if is_compiled_binary(Path(directory, name))}


# English source labels; translate with t() when displaying.
CHANGE_KINDS = {"feature": "New feature", "bug": "Bug", "change": "Change", "edit": "Document edit"}


class StageEngine(Protocol):
    def document(self, stage: str, idea: str, context: str) -> str: ...
    # With change (a change request) the engine keeps the existing code/tests and returns only what changed.
    def code(self, idea: str, docs: str, feedback: str | None = None, change: str | None = None) -> dict[str, str]: ...
    def tests(self, idea: str, docs: str, files: dict[str, str], change: str | None = None,
              feedback: str | None = None) -> dict[str, str]: ...


class RunSession:
    def __init__(
        self,
        idea: str,
        workspace: Path,
        engine: StageEngine,
        max_repairs: int,
        decisions: dict[str, str],
        output: Path | None = None,
        on_event: Callable[[str, str, str], None] | None = None,
        on_question: Callable[[str, str, str], str] | None = None,
        run_id: str | None = None,
        resume: bool = False,
        stage_models: dict[str, str] | None = None,
        keep_tests: bool = False,
    ):
        self.idea = idea.strip()
        # A retry of the Testing stage that runs the test files already on disk instead of writing a new suite.
        self.keep_tests = keep_tests
        self.workspace = Path(workspace).resolve()
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.history = History(self.workspace / "runs" / "history.sqlite3")
        existing = self.history.get(self.run_id) if resume else None
        if resume:
            if existing is None or existing["status"] != "paused":
                raise ValueError("Only a paused run can be resumed")
            self.output_dir = Path(existing["output_dir"])
            self.project_dir = Path(existing["project_dir"])
            if not self.output_dir.is_dir() or not self.project_dir.is_dir():
                raise ValueError("The project and report folders must exist to resume")
        else:
            self.output_dir = self.workspace / "runs" / self.run_id
            self.project_dir = Path(output).expanduser().resolve() if output is not None else default_project_dir(self.workspace, self.idea, self.run_id)
            if self.project_dir.exists():
                raise ValueError(f"Project folder already exists: {self.project_dir}")
            self.output_dir.mkdir(parents=True)
            self.project_dir.mkdir(parents=True)
            (self.project_dir / "docs").mkdir()
            self.history.create(self.run_id, self.idea, self.output_dir, self.project_dir, getattr(engine, "model", None), stage_models)
        self.engine = engine
        self.on_event = on_event
        self.on_question = on_question
        self.decisions = decisions
        if not resume:
            (self.output_dir / "decisions.yaml").write_text(yaml.safe_dump(decisions, sort_keys=False), encoding="utf-8")
            (self.project_dir / "docs" / "decisions.yaml").write_text(yaml.safe_dump(decisions, sort_keys=False), encoding="utf-8")
        self.max_repairs = max_repairs
        # Language of every document the agents write (and of the report files this module writes).
        self.language = normalize(decisions.get("language"))
        engine.language = self.language
        # In a small project documents are kept short and the review only sends the design back on a critical finding.
        self.small = decisions.get("scale") == "small"
        self.docs: dict[str, str] = {}
        self.clarifications: list[dict[str, str]] = []
        self.code_files: dict[str, str] = {}
        self.test_files: dict[str, str] = {}
        self.traceability = ""
        self.manifest: dict = {}
        # While a change request is applied: earlier documents are the base and only affected parts are updated.
        self.change: dict | None = None
        self.previous_docs: dict[str, str] = {}
        self.setup_done = False
        self.python_executable = sys.executable
        self.attempts = 0
        self.test_passed = False
        # Executables were asked for (binary delivery) but none could be built; the project then fails.
        self.package_failed = False
        # Throwaway PostgreSQL/Redis for the tests, started on first use and stopped when the run ends or pauses.
        self.services = LocalServices(self.output_dir / "tmp" / "services", self.decisions)
        self.last_test_output = ""
        # Outcome of building the executables of a binary delivery; the deployment agent documents it.
        self.package_report = ""
        self.completed_stages: set[str] = set()
        self._live_written = 0.0
        self._activity_written = 0.0
        self._stop_checked, self._stopping = 0.0, False
        if hasattr(engine, "output_listener"):
            engine.output_listener = self._live_output
        if hasattr(getattr(engine, "llm", None), "call_log"):
            engine.llm.call_log = self._log_call
            engine.llm.activity = self._activity
            engine.llm.should_stop = self._stop_requested
        if resume:
            self._load_checkpoint(existing)
            if not self.history.resume(self.run_id):
                raise ValueError("The run is already being resumed")

    def _t(self, message: str, **values: object) -> str:
        """Text written into project documents, in the project's document language."""
        return t(message, language=self.language, **values)

    def _load_checkpoint(self, row: dict) -> None:
        self.completed_stages = completed_from_events(row["events"])
        for stage, name in STAGE_FILES.items():
            path = self.output_dir / name
            if path.is_file():
                self.previous_docs[stage] = path.read_text(encoding="utf-8")
                # Old documents of stages to be regenerated must not leak into the context (delta mode provides them separately).
                if stage in self.completed_stages:
                    self.docs[stage] = self.previous_docs[stage]
        self.change = self.history.active_change(self.run_id)
        if self.change and not any(
            event["status"] == "change_started" and event["detail"].startswith(self.change["id"]) for event in row["events"]
        ):
            self._event("workflow", "change_started", f"{self.change['id']} · {self.change['title']}")
        traceability = self.output_dir / "traceability.md"
        if traceability.is_file():
            self.traceability = traceability.read_text(encoding="utf-8")
        clarifications = self.output_dir / "clarifications.yaml"
        if clarifications.is_file():
            self.clarifications = yaml.safe_load(clarifications.read_text(encoding="utf-8")) or []
        bundle = self.output_dir / "code_files.json"
        if bundle.is_file():
            self.code_files = json.loads(bundle.read_text(encoding="utf-8"))
            if hasattr(self.engine, "current_files"):
                self.engine.current_files.update(self.code_files)
            self.manifest = read_project_manifest(self.project_dir, self.decisions)
        self.attempts = row["attempts"]
        self.test_passed = "testing" in self.completed_stages
        attempt = self.output_dir / "test_attempts" / f"attempt-{self.attempts}.txt"
        if attempt.is_file():
            self.last_test_output = attempt.read_text(encoding="utf-8")

    def _live_output(self, stage: str, text: str) -> None:
        """Writes the agent's streaming output to the file the UI reads (at most ~3 times a second)."""
        now = time.monotonic()
        if now - self._live_written < 0.3:
            return
        self._live_written = now
        try:
            live_dir = self.output_dir / "live"
            live_dir.mkdir(exist_ok=True)
            (live_dir / f"{stage}.md").write_text(text, encoding="utf-8")
        except OSError:
            pass  # The live view is only a convenience; a write can collide while the UI reads the file.

    def _activity(self, record: dict) -> None:
        """What the run is doing right now (a model call or a command), for the live activity view in the UI.

        Progress of a streaming call is written at most once a second; every phase change is written at once.
        """
        moment = time.monotonic()
        if record.get("phase") in {"reasoning", "writing"} and moment - self._activity_written < 1:
            return
        self._activity_written = moment
        try:
            live_dir = self.output_dir / "live"
            live_dir.mkdir(exist_ok=True)
            if record.get("kind") == "model" and record.get("phase") == "waiting" and record.get("stage"):
                # A new call starts: the previous call's output would look like this call is already writing.
                (live_dir / f"{record['stage']}.md").unlink(missing_ok=True)
                self._live_written = 0.0
            temporary = live_dir / "activity.json.tmp"
            temporary.write_text(json.dumps({**record, "updated_at": now()}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, live_dir / "activity.json")
        except OSError:
            pass  # Only a convenience; the UI may be reading the file at that moment.

    def _stop_requested(self) -> bool:
        """Whether the user pressed Stop; asked while a model answer streams or a command runs, so the database is
        read at most once a second."""
        moment = time.monotonic()
        if moment - self._stop_checked >= 1:
            self._stop_checked = moment
            self._stopping = self.history.pause_requested(self.run_id)
        return self._stopping

    def _run_reported(self, label: str, command: list[str], env: dict[str, str], log: Path, timeout: int) -> tuple[str, int | None]:
        """_run_logged, with the command shown in the live activity view while it runs."""
        current = {"kind": "command", "label": label, "command": " ".join(command), "started_at": now(),
                   "log": log.relative_to(self.output_dir).as_posix()}
        self._activity({**current, "phase": "running"})
        output, code = _run_logged(command, self.project_dir, env, log, timeout=timeout, should_stop=self._stop_requested)
        self._activity({**current, "phase": "done" if code == 0 else "failed", "exit": code})
        return output, code

    def _log_call(self, record: dict) -> None:
        """One line per model request attempt, so a failure report shows which call failed and how."""
        try:
            with open(self.output_dir / "llm_calls.jsonl", "a", encoding="utf-8") as log:
                log.write(json.dumps({"time": now(), **record}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _keep_build_output(self, stage: str, output: str) -> None:
        """The event keeps only the end of a build error, where a stack trace often hides the message; the failure
        report needs all of it."""
        try:
            folder = self.output_dir / "build_checks"
            folder.mkdir(exist_ok=True)
            number = len(list(folder.glob("check-*.txt"))) + 1
            (folder / f"check-{number:02d}-{stage}.txt").write_text(f"{now()}\n{output}", encoding="utf-8")
        except OSError:
            pass

    def record_failure(self, exc: BaseException) -> None:
        """Keeps the full traceback next to the run; the history only stores the one-line message."""
        try:
            with open(self.output_dir / "failure.txt", "a", encoding="utf-8") as report:
                report.write(f"=== {now()} · attempts {self.attempts} · {type(exc).__name__}\n")
                report.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + "\n")
        except OSError:
            pass

    def _check_pause(self) -> None:
        if self.history.pause_requested(self.run_id):
            raise RunPaused()

    def execute(self, stage: str, operation: Callable[[], str]) -> str:
        self._check_pause()
        if stage in self.completed_stages:
            return "Stage already completed"
        if stage in skip_stages(self.change) and stage in self.previous_docs:
            return self._skip(stage)
        result = operation()
        self._check_pause()
        return result

    def _skip(self, stage: str) -> str:
        """Stage not affected by the change: the previous document stays as it is, only the test result line is refreshed."""
        content = self.previous_docs[stage]
        if stage == "deployment":
            write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
            self._package()
        if stage in {"deployment", "maintenance"}:
            content = self._with_test_header(content)
            self._write_doc(STAGE_FILES[stage], content)
        self.docs[stage] = content
        self._event(stage, "skipped", f"{self.change['id']} does not affect this stage")
        return "Stage skipped"

    def _with_test_header(self, content: str) -> str:
        verdict = self._t("PASSED") if self.test_passed else self._t("FAILED")
        header = self._t("Verified test result: **{verdict}** ({attempts} attempts).", verdict=verdict, attempts=self.attempts)
        return f"> {header} {TEST_MARKER}\n\n{TEST_HEADER.sub('', content, count=1)}"

    def pause(self) -> RunResult:
        self.services.stop()
        self._event("workflow", "paused", "Paused at the user's request")
        self.history.pause(self.run_id, self.attempts)
        return RunResult(self.run_id, "paused", self.output_dir, self.project_dir, self.attempts)

    def _event(self, stage: str, status: str, detail: str = "") -> None:
        self.history.event(self.run_id, stage, status, detail)
        if self.on_event is not None:
            self.on_event(stage, status, detail)

    def _generate(self, stage: str, instruction: str = "", base: str | None = None) -> str:
        """Produces the document; if it fails the quality gate the agent rewrites it once with the reason.

        With base (a change request or a review revision) the agent returns only the changed/new sections instead
        of the whole document, and they are placed into the existing document, which keeps the output short.
        """
        if base is None and self.change and stage != "review":
            base = self.previous_docs.get(stage)
        gate_feedback = ""
        for _ in range(2):
            prompt = self._change_instruction(stage) + instruction + gate_feedback
            if base:
                prompt += self._patch_instruction(base)
            elif self.small and stage == "review":
                # The review is the model's most expensive step (long hidden reasoning); a small project needs a short one.
                prompt += (
                    "SMALL PROJECT REVIEW: report at most 5 findings and only real risks. Do NOT walk through every "
                    f"requirement. The whole report must stay under {SMALL_REVIEW_LIMIT} characters: the findings table, "
                    "then the VERDICT line.\n\n"
                )
            elif self.small:
                prompt += (
                    f"SMALL PROJECT: keep the document to about {SMALL_DOC_LIMIT} characters at most. Write only the "
                    "sections this project needs; no generic explanations, repetition or long tables.\n\n"
                )
            output = self._with_questions(
                stage, lambda: self.engine.document(stage, self.idea, prompt + self._context(stage))
            )
            content = output
            if base:
                content, replaced, added = apply_patch(base, output)
                if replaced or added:
                    self._event(stage, "delta", f"{replaced} sections updated, {added} sections added")
                elif any(token.casefold() in output.casefold() for token in NO_CHANGE_TOKENS):
                    self._event(stage, "delta", "no change needed")
                else:
                    # If the agent returned plain text without headings the patch cannot be applied; take it as the document.
                    content = output
            problem = self._quality_gate(stage, content)
            if problem is None:
                return content
            self._event(stage, "gate_failed", problem)
            gate_feedback = (
                f"THE PREVIOUS DRAFT FAILED THE QUALITY GATE: {problem}\n"
                + ("Return the sections that fix the gaps.\n\n" if base else "Fix the gaps and rewrite the whole document.\n\n")
            )
        raise ValueError(problem)

    def _patch_instruction(self, base: str) -> str:
        return (
            "RESPONSE FORMAT — CHANGED SECTIONS ONLY: Do NOT return the whole document. Write only the sections that "
            "must change or be added. Every changed section must start with EXACTLY the heading of the EXISTING DOCUMENT "
            "(same # level and same text) and contain the section's complete new content with its subsections; the section "
            "is replaced by it as a whole. For a new section use a new heading and put the line "
            "'<!-- AFTER: <existing heading text> -->' right above it, naming the existing heading it is inserted after. "
            f"If no section needs to change, write only '{NO_CHANGE}'.\n"
            f"EXISTING DOCUMENT:\n{base[:14000]}\n\n"
        )

    def _document(self, stage: str, filename: str, announce: bool = True) -> str:
        if announce:
            self._event(stage, "started")
        content = self._generate(stage)
        if stage in {"deployment", "maintenance"}:
            content = self._with_test_header(content)
        self.docs[stage] = content
        self._write_doc(filename, content)
        self._event(stage, "completed", filename)
        return content

    def _quality_gate(self, stage: str, content: str) -> str | None:
        """The minimum conditions for a document to pass to the next stage; if it fails the agent writes it once more."""
        if not content.strip():
            return f"The {stage} agent returned an empty document"
        if stage == "analysis" and "FR-" not in content:
            return "The analysis document must contain numbered functional requirements such as FR-01"
        if stage == "analysis" and "NFR-" not in content:
            return "The analysis document must contain numbered non-functional requirements such as NFR-01"
        if stage == "design" and content.count("```mermaid") < 2:
            return "The architecture document must contain Mermaid component and sequence diagrams"
        if stage == "design" and self.decisions["database"] != "none" and "erDiagram" not in content:
            return "The architecture document must contain an ER diagram for the selected database"
        if stage == "review" and review_verdict(content) is None:
            return "The review report must contain a 'VERDICT: APPROVE' or 'VERDICT: REVISE' line"
        return None

    def _change_text(self) -> str:
        change = self.change or {}
        text = (
            f"{change.get('id')} · {CHANGE_KINDS.get(change.get('kind'), change.get('kind'))}: {change.get('title')}\n"
            f"{change.get('description', '')}"
        )
        if change.get("clarifications"):
            # The user's answers to open questions take precedence over assumptions.
            text += f"\n\nThe user's clarifications (firm decisions):\n{change['clarifications'][:3000]}"
        if change.get("impact"):
            text += f"\n\nImpact analysis:\n{change['impact'][:4000]}"
        return text

    def _change_instruction(self, stage: str) -> str:
        """Delta mode: the agent takes the existing document as the base and updates only what the change affects."""
        if not self.change:
            return ""
        if stage == "review":
            return (
                f"THIS REVIEW IS FOR A CHANGE ITERATION ({self.change['id']}). Check especially whether the change "
                f"is reflected in the design correctly and completely.\n{self._change_text()}\n\n"
            )
        if not self.previous_docs.get(stage):
            return f"CHANGE REQUEST BEING APPLIED:\n{self._change_text()}\n\n"
        return (
            f"CHANGE MODE — {self._change_text()}\n\n"
            "Take the EXISTING DOCUMENT below as the base. Update only the sections this change affects or add "
            "new sections/requirements; do not touch unaffected sections. "
            f"Put the tag [{self.change['id']}] at the end of every changed or added place.\n\n"
        )

    def _write_doc(self, filename: str, content: str) -> None:
        # Keep the old version before overwriting (so delta updates and section edits can be undone).
        target = self.output_dir / filename
        if target.is_file() and target.read_text(encoding="utf-8") != content:
            backup_versions(self.output_dir, filename)
        (self.output_dir / filename).write_text(content, encoding="utf-8")
        (self.project_dir / "docs" / filename).write_text(content, encoding="utf-8")

    def _with_questions(self, stage: str, operation: Callable[[], str | dict]) -> str | dict:
        for _ in range(3):
            try:
                return operation()
            except ClarificationNeeded as exc:
                if self.on_question is None:
                    raise ValueError(f"The {stage} agent asked for a decision: {exc.question}") from exc
                self._event(stage, "question", exc.question)
                answer = self.on_question(self.run_id, stage, exc.question).strip()
                if not answer:
                    raise ValueError("Cannot continue with an empty user answer")
                self.clarifications.append({"stage": stage, "question": exc.question, "answer": answer})
                self._write_doc("clarifications.yaml", yaml.safe_dump(self.clarifications, allow_unicode=True, sort_keys=False))
                self._event(stage, "answered", "The user answered")
        raise ValueError(f"The {stage} agent could not proceed even after the clarification")

    def _context(self, stage: str) -> str:
        """Short critical information first, long documents after; every document is truncated to a budget."""
        parts = ["Architecture decisions and user constraints:\n" + yaml.safe_dump(self.decisions, allow_unicode=True, sort_keys=False)]
        if self.change:
            parts.append("CHANGE REQUEST BEING APPLIED (the goal of this iteration):\n" + self._change_text())
        if self.clarifications:
            parts.append("The user's answers to stage questions (firm decisions):\n" + "\n".join(
                f"- [{item['stage']}] Question: {item['question']}\n  Answer: {item['answer']}" for item in self.clarifications
            ))
        notes = self.history.notes(self.run_id)
        if notes:
            parts.append("Guidance the user gave through the chat (take it into account):\n" + "\n".join(
                f"- {note['content']}" for note in notes
            ))
        if stage in {"deployment", "maintenance"}:
            parts.append(
                f"Actual test result: {'PASSED' if self.test_passed else 'FAILED'} ({self.attempts} attempts).\n"
                f"End of the last test output:\n{self.last_test_output[-3000:]}"
            )
            if self.traceability:
                parts.append(self.traceability[:3000])
            if self.package_report:
                parts.append("Packaging result (the executables actually built):\n" + self.package_report)
        for name in STAGE_INPUTS[stage]:
            if name in self.docs:
                doc = self.docs[name]
                if len(doc) > DOC_BUDGET:
                    doc = doc[:DOC_BUDGET] + "\n\n…(document truncated)"
                parts.append(f"--- {name} document ---\n{doc}")
        return "\n\n".join(parts)

    def planning(self) -> str:
        return self._document("planning", "planning.md")

    def analysis(self) -> str:
        return self._document("analysis", "requirements.md")

    def design(self) -> str:
        return self._document("design", "architecture.md")

    def review(self) -> str:
        """Architecture review: on a REVISE verdict the Design Agent fixes the findings and the review is repeated."""
        self._event("review", "started")
        rounds = []
        for number in range(1, MAX_REVIEW_ROUNDS + 1):
            report = self._generate("review")
            verdict = review_verdict(report)
            rounds.append(f"## {self._t('Review round {number}', number=number)} — {verdict}\n\n{report}")
            self._event("review", "approved" if verdict == "APPROVE" else "changes_requested", f"round {number}")
            if verdict == "APPROVE" or number == MAX_REVIEW_ROUNDS:
                break
            if self.small and not CRITICAL_FINDING.search(report):
                self._event("review", "revision_skipped", "small project: no critical finding, the findings are left for the maintenance list")
                break
            self._event("design", "revision_started", f"review round {number}")
            self.docs["design"] = self._generate("design", (
                "ARCHITECTURE REVIEW FINDINGS — revise the existing design only where these findings require it:\n"
                f"{report[:6000]}\n\n"
            ), base=self.docs["design"])
            self._write_doc("architecture.md", self.docs["design"])
            self._event("design", "revision_completed", "architecture.md")
        summary = "APPROVED" if verdict == "APPROVE" else "CONTINUED WITH OPEN FINDINGS"
        content = (
            f"# {self._t('Architecture review report')}\n\n"
            + self._t("Result: **{summary}** ({rounds} rounds).", summary=self._t(summary), rounds=len(rounds))
            + "\n\n" + "\n\n".join(rounds)
        )
        self.docs["review"] = content
        self._write_doc("design_review.md", content)
        self._event("review", "completed", "design_review.md")
        return summary

    def development(self) -> str:
        self._event("development", "started")
        if self.change and self.code_files:
            return self._develop_change()
        self.code_files = self._without_docs(self._with_questions(
            "development", lambda: self.engine.code(self.idea, self._context("development"))))
        write_generated_files(self.project_dir, self.code_files)
        self._save_code_files()
        self._load_manifest()
        write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
        self._event("development", "completed", ", ".join(self.code_files))
        return "application ready"

    def _develop_change(self) -> str:
        """Change request: existing code is kept; for a bug a test that reproduces it is written first (TDD)."""
        description = self._change_text()
        feedback = None
        if self.change["kind"] == "bug":
            existing = self._test_files_on_disk()
            tests = self._with_questions("testing", lambda: self.engine.tests(
                self.idea, self._context("testing"), {**self.code_files, **existing}, change=description
            ))
            tests = {name: content for name, content in tests.items() if name.startswith(TEST_DIRS)}
            if tests:
                write_generated_files(self.project_dir, tests)
                if hasattr(self.engine, "current_files"):
                    self.engine.current_files.update(tests)
            output, code = self._run_checks(self._test_env())
            self.setup_done = False
            missing = uncovered_tests(self.manifest, list(tests))
            if missing:
                self._event("testing", "tests_not_covered", ", ".join(missing))
                output += (
                    f"\nWARNING: The new test files are not run by the project.yaml test commands: {', '.join(missing)}. "
                    "Update the test commands in project.yaml to run the tests folder instead of a single file."
                )
            self._event("testing", "bug_reproduced" if code else "bug_not_reproduced", ", ".join(tests) or "no tests")
            feedback = output
        replacements = self._with_questions("development", lambda: self.engine.code(
            self.idea, self._context("development"), feedback=feedback, change=description
        ))
        rejected = sorted(name for name in replacements if name.startswith(TEST_DIRS))
        replacements = self._without_stubs(self._without_docs(
            {name: content for name, content in replacements.items() if name not in rejected}))
        if rejected:
            self._event("development", "tests_protected", ", ".join(rejected))
        if replacements:
            write_generated_files(self.project_dir, replacements)
            self.code_files.update(replacements)
            self._save_code_files()
            self._load_manifest()
            write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
        self._event("development", "completed", ", ".join(replacements) or "no changed files")
        return "change applied"

    def _without_docs(self, files: dict[str, str]) -> dict[str, str]:
        """docs/ holds the SDLC documents the system writes; a model that adds its own file there must not fail the run."""
        skipped = sorted(name for name in files if name.split("/", 1)[0] == "docs")
        if skipped:
            self._event("development", "files_skipped", ", ".join(skipped))
        return {name: content for name, content in files.items() if name not in skipped}

    def _remove_old_tests(self) -> None:
        """A rewritten test suite replaces the old one: tests left from an earlier generation import helpers that
        no longer exist and fail every attempt, and they crowd the agents' prompts."""
        old = self._test_files_on_disk()
        for folder in ("tests", "test"):
            shutil.rmtree(self.project_dir / folder, ignore_errors=True)
        if hasattr(self.engine, "current_files"):
            for name in [name for name in self.engine.current_files if name.startswith(TEST_DIRS)]:
                del self.engine.current_files[name]
        if old:
            self._event("testing", "old_tests_removed", ", ".join(old))

    def _without_stubs(self, files: dict[str, str]) -> dict[str, str]:
        """A repair that does not see a file may return it as a placeholder (`const _placeholder = undefined;` once
        replaced a whole Express app); the existing file is kept instead."""
        def stub(name: str, content: str) -> bool:
            old = self.code_files.get(name, "")
            if len(old) < 1000 or len(content.strip()) >= 200 or len(content) * 10 >= len(old):
                return False
            # A real, shorter rewrite still defines or re-exports some of what the file did.
            defined = set(re.findall(r"\b(?:def|class|function|const|let|var|fn|func|type|interface)\s+(\w+)", old))
            return bool(defined) and not any(re.search(rf"\b{re.escape(word)}\b", content) for word in defined)

        stubs = sorted(name for name, content in files.items() if stub(name, content))
        if stubs:
            self._event("development", "stubs_rejected", ", ".join(stubs))
        return {name: content for name, content in files.items() if name not in stubs}

    def _test_files_on_disk(self) -> dict[str, str]:
        files = {}
        for folder in ("tests", "test"):
            root = self.project_dir / folder
            for path in sorted(root.rglob("*")) if root.is_dir() else []:
                if path.is_file() and "__pycache__" not in path.parts and path.stat().st_size <= 50_000:
                    try:
                        files[path.relative_to(self.project_dir).as_posix()] = path.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        continue
        return files

    def _test_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for secret_name in ("OPENCODE_API_KEY", "OPENAI_API_KEY"):
            env.pop(secret_name, None)
        # Keep a generated SQLite application runnable during API checks even when
        # the user has not created a local .env file. Isolate it from project data.
        if self.decisions.get("database") == "sqlite":
            env["TASK_DB_PATH"] = str(self.output_dir / "tmp" / "autosdlc-test.sqlite3")
        if self.decisions.get("project_type") in {"backend", "fullstack"}:
            # Files the tests upload stay out of the project folder.
            env["UPLOADS_DIR"] = str(self.output_dir / "tmp" / "uploads")
        started = self.services.env is not None
        env.update(self.services.start())
        if not started:
            for note in self.services.notes:
                self._event("testing", "services_unavailable", note)
            if self.services.env:
                self._event("testing", "services_started", ", ".join(sorted(self.services.env)))
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env.update({"NO_COLOR": "1", "FORCE_COLOR": "0"})
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Without a TTY pnpm aborts instead of recreating node_modules (ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY).
        env["CI"] = "true"
        return env

    def _load_manifest(self) -> None:
        """Validates project.yaml/Dockerfile; on an error the Development Agent fixes it once with the reason."""
        try:
            self.manifest = read_project_manifest(self.project_dir, self.decisions)
            return
        except ValueError as exc:
            problem = str(exc)
        self._event("development", "gate_failed", problem)
        fixes = self._with_questions("development", lambda: self.engine.code(
            self.idea, self._context("development"),
            feedback=f"THE PROJECT CONFIGURATION COULD NOT BE VALIDATED: {problem}\nReturn project.yaml (and the Dockerfile if needed) fixed.",
        ))
        fixes = self._without_docs({name: content for name, content in fixes.items() if not name.startswith(TEST_DIRS)})
        if fixes:
            write_generated_files(self.project_dir, fixes)
            self.code_files.update(fixes)
            self._save_code_files()
        self.manifest = read_project_manifest(self.project_dir, self.decisions)

    def _save_code_files(self) -> None:
        (self.output_dir / "code_files.json").write_text(
            json.dumps(self.code_files, ensure_ascii=False), encoding="utf-8"
        )

    def testing(self) -> str:
        # The Test Agent writes against code that compiles, and a compile error does not use up a test attempt.
        self._fix_build(with_tests=False)
        self._event("testing", "started")
        existing = self._test_files_on_disk() if self.change or self.keep_tests else {}
        if existing and not self.change:
            # The suite of the last try is kept: a working suite (all API tests passing once) was thrown away for a
            # new one that did not even parse, and every new suite is a fresh draw.
            tests = existing
            self._event("testing", "tests_kept", f"{len(tests)} test files")
        elif existing:
            # Change request: existing tests are kept, only new/updated tests are written, then all of them run (regression).
            new_tests = self._with_questions("testing", lambda: self.engine.tests(
                self.idea, self._context("testing"), {**self.code_files, **existing}, change=self._change_text()
            ))
            if any(not name.startswith(TEST_DIRS) for name in new_tests):
                raise ValueError("The test agent may only write test files")
            if new_tests:
                write_generated_files(self.project_dir, new_tests)
            tests = {**existing, **new_tests}
            self._event("testing", "regression", f"{len(new_tests)} new/updated, {len(tests)} test files in total")
        else:
            gate_feedback = ""
            for _ in range(2):
                tests = self._with_questions("testing", lambda: self.engine.tests(
                    self.idea, gate_feedback + self._context("testing"), self.code_files
                ))
                problem = test_placement_problem(tests)
                if problem is None:
                    break
                # Like the other quality gates: the reason is given to the Test Agent, which is asked to fix it once.
                self._event("testing", "gate_failed", problem)
                gate_feedback = f"YOUR PREVIOUS TEST OUTPUT WAS REJECTED: {problem}. " + (
                    "Return the complete test suite again with this fixed.\n\n" if "larger than" in problem else
                    "Write all test files under the tests/ folder (e.g. tests/test_x.py); rewrite test files at the "
                    "root under tests/ as well.\n\n"
                )
            else:
                raise ValueError(problem)
            self._remove_old_tests()
            write_generated_files(self.project_dir, tests)
        self.test_files = tests
        if hasattr(self.engine, "current_files"):
            self.engine.current_files.update(tests)
        self._fix_build(with_tests=True)
        tests = self.test_files
        missing = uncovered_tests(self.manifest, list(tests))
        if missing:
            # A test that does not run must not look like a pass: it is stated clearly in the report and the events.
            self._event("testing", "tests_not_covered", ", ".join(missing))
        self.traceability, coverage = build_matrix(self.docs.get("analysis", ""), tests, self.language)
        self._write_doc("traceability.md", self.traceability)
        self._event("testing", "traceability", f"FR coverage {coverage['FR'] * 100:.0f}%")
        attempt_dir = self.output_dir / "test_attempts"
        attempt_dir.mkdir(exist_ok=True)
        reports = []
        # The attempt with the most passing tests on the current test files, and the code it ran.
        best: dict | None = None
        # Attempts run while there are repairs left; a run that keeps improving earns up to EXTRA_ATTEMPTS more.
        limit, extra, previous = self.max_repairs + 1, 0, None
        # The output of the last attempt and the test files it ran, to find tests the code repair after it broke.
        last_run: tuple[str, dict[str, str]] | None = None
        number = 0
        while number < limit:
            number += 1
            self.attempts = number
            tests_run = dict(self.test_files)
            output, code = self._run_checks(self._test_env())
            # A server that does not even start ran no test; like a compile error it goes back to the developer
            # without using up an attempt (a repair that broke an import cost two attempts this way). Once per
            # attempt: a budget for the whole stage was spent before the first attempt and left the others bare.
            if (code and any(marker in output for marker in SERVER_START_FAILURES)
                    and self._repair_code(output, SERVER_START_REPAIR)):
                self._fix_build(with_tests=True)
                output, code = self._run_checks(self._test_env())
            passed = code == 0
            self.last_test_output = output
            (attempt_dir / f"attempt-{number}.txt").write_text(output, encoding="utf-8")
            reports.append(
                f"## {self._t('Attempt {number}', number=number)}\n{self._t('Exit code: {code}', code=code)}\n\n```text\n{output}\n```\n"
            )
            self._event("testing", "passed" if passed else "failed", f"attempt {number}")
            if passed:
                self.test_passed = True
                break
            count = passed_test_count(output)
            base, note = output, ""
            if (number == limit and extra < EXTRA_ATTEMPTS and self.max_repairs and count is not None
                    and previous is not None and count > previous):
                # The last repair fixed tests (0, 1, 2 of 4 UI tests passed over three attempts, then no attempt was
                # left): one more repair is worth it while the run keeps getting better.
                limit += 1
                extra += 1
                self._event("testing", "attempt_added", f"{previous} → {count} passed")
            previous = count
            if best and best["tests"] == self.test_files and count is not None and count < best["passes"]:
                # The last repair broke tests that passed before (54 passing fell to 35 once, and the run ended on the
                # worse code): its code is rolled back and the next repair starts from the better version.
                broken = sorted(failing_test_names(output) - failing_test_names(best["output"]))
                self._restore_code(best["code"])
                self.last_test_output = best["output"]
                self._event("development", "repair_reverted",
                            f"attempt {number}: {count} passed, attempt {best['number']}: {best['passes']} passed")
                reports.append("> " + self._t(
                    "The repair before this attempt made it worse; the code was rolled back to the version of attempt {best}.",
                    best=best["number"]) + "\n")
                base = best["output"]
                note = REVERTED_NOTE.format(count=count, best=best["passes"], broken="; ".join(broken[:15]) or "-")
                # The next repair starts from the restored code: progress and broken tests are measured against it,
                # not against the attempt that was thrown away (20 → 47 passed once looked like progress, and tests
                # of the discarded attempt were reported as broken).
                previous = best["passes"]
                last_run = (best["output"], best["tests"])
            elif count is not None and (best is None or best["tests"] != self.test_files or count >= best["passes"]):
                best = {"number": number, "passes": count, "output": output,
                        "code": dict(self.code_files), "tests": dict(self.test_files)}
            regressed: list[str] = []
            if not note:
                if last_run:
                    # The total can stay the same while the repair fixed some tests and broke others; the broken
                    # ones are named to the developer, and the rules that send failures to the Test Agent skip them.
                    regressed = regressed_tests(last_run[0], output, last_run[1], tests_run)
                    if regressed:
                        self._event("development", "repair_regressed", "; ".join(regressed[:10]))
                        note = REGRESSED_NOTE.format(tests="\n".join(f"- {name}" for name in regressed[:20]))
                last_run = (output, tests_run)
            if number >= limit:
                continue
            if failure_is_in_tests(output):
                # The error is in the Test Agent's own files; the developer is not allowed to touch them, so the author repairs them.
                if self._repair_tests(failure_digest(base) + note, f"from test attempt {number}"):
                    continue
            # Each failure goes to its author: one in the test's own code to the Test Agent, the rest to the developer.
            in_tests, in_app = failures_by_author(base, set(regressed))
            repaired = bool(in_tests) and self._repair_tests(in_tests + TEST_CODE_NOTE, f"from test attempt {number}")
            if in_app and self._repair_code(in_app + note, f"from test attempt {number}"):
                repaired = True
            if repaired:
                # A repair that broke the build or an import is sent back before it costs the next attempt.
                self._fix_build(with_tests=True)
        summary = "PASSED" if self.test_passed else "FAILED"
        report = f"# {self._t('Test report')}\n\n" + self._t(
            "Result: **{summary}** ({attempts} attempts).", summary=self._t(summary), attempts=self.attempts) + "\n\n"
        if missing:
            report += self._t("> ⚠️ These test files were not run by the project.yaml commands: {files}", files=", ".join(missing)) + "\n\n"
        report += self._t(
            "Traceability: FR coverage {fr}%, NFR coverage {nfr}% ([matrix](traceability.md)).",
            fr=f"{coverage['FR'] * 100:.0f}", nfr=f"{coverage['NFR'] * 100:.0f}") + "\n\n"
        report += "\n".join(reports)
        self._write_doc("test_report.md", report)
        return self._t(summary)

    def _repair_tests(self, output: str, reason: str) -> bool:
        """The Test Agent fixes its own files from the failure output; False when it returned no test file."""
        self._event("testing", "tests_repair_started", reason)
        fixed = self._with_questions("testing", lambda: self.engine.tests(
            self.idea, self._context("testing"), {**self.code_files, **self._test_files_on_disk()}, feedback=output
        ))
        fixed = {name: content for name, content in fixed.items() if name.startswith(TEST_DIRS)}
        if not fixed:
            return False
        write_generated_files(self.project_dir, fixed)
        self.test_files = {**self.test_files, **fixed}
        if hasattr(self.engine, "current_files"):
            self.engine.current_files.update(fixed)
        self._event("testing", "tests_repaired", ", ".join(fixed))
        return True

    def _restore_code(self, code: dict[str, str]) -> None:
        """Puts the application files back as they were; files added since then are removed."""
        current = hasattr(self.engine, "current_files")
        for name in set(self.code_files) - set(code):
            (self.project_dir / name).unlink(missing_ok=True)
            if current:
                self.engine.current_files.pop(name, None)
        changed = {name: content for name, content in code.items() if self.code_files.get(name) != content}
        if changed:
            write_generated_files(self.project_dir, changed)
        self.code_files.clear()
        self.code_files.update(code)
        if current:
            self.engine.current_files.update(code)
        self._save_code_files()
        self._load_manifest()
        self.setup_done = False

    def _repair_code(self, output: str, reason: str) -> bool:
        """The Development Agent fixes the application from the failure output; False when nothing was changed."""
        self._event("development", "repair_started", reason)
        replacements = self._with_questions("development", lambda: self.engine.code(
            self.idea, self._context("development"), feedback=output
        ))
        # The tests belong to the Test Agent; the developer cannot make them pass by changing them.
        rejected = sorted(name for name in replacements if name.startswith(TEST_DIRS))
        replacements = self._without_stubs(self._without_docs(
            {name: content for name, content in replacements.items() if name not in rejected}))
        if hasattr(self.engine, "current_files"):
            self.engine.current_files.update(self.test_files)
        if rejected:
            self._event("development", "tests_protected", ", ".join(rejected))
        issues = {name: reason for name, reason in (getattr(self.engine, "test_issues", None) or {}).items()
                  if name in self.test_files}
        if replacements:
            write_generated_files(self.project_dir, replacements)
            self.code_files.update(replacements)
            self._save_code_files()
            self._load_manifest()
            self.setup_done = False
            self._event("development", "repair_completed", ", ".join(replacements))
        if issues:
            # A wrong test (an endpoint the design does not have, a broken setup) cannot be fixed by the developer;
            # its author fixes it. The report goes last, so it survives when the output is cut to its end.
            self._event("development", "test_issues_reported", ", ".join(issues))
            report = "\n".join(f"- {name}: {reason}" for name, reason in issues.items())
            if self._repair_tests(f"{output}\n\nTHE DEVELOPER REPORTS THESE TESTS AS WRONG (the application is right "
                                  f"here); fix them:\n{report}\n", TESTS_REPORTED):
                return True
        return bool(replacements)

    def _fix_build(self, with_tests: bool) -> None:
        """Compile-only check before the test attempts: a compile error goes back to its author (the developer, or the
        Test Agent for its own files) without using up a test attempt. What is still broken after BUILD_FIXES rounds
        is left to the test attempts, which report it."""
        stage = "testing" if with_tests else "development"
        failed = False
        for round_number in range(BUILD_FIXES + 1):
            output, code = self._compile_check(self._test_env())
            if code == 0:
                if failed:
                    self._event(stage, "build_fixed", f"fix rounds: {round_number}")
                return
            failed = True
            self._event(stage, "build_failed", output[-300:])
            self._keep_build_output(stage, output)
            if round_number == BUILD_FIXES:
                return
            reason = "from the compile check"
            if failure_is_in_tests(output):
                if not with_tests:
                    # The tests of an earlier generation, which the Test Agent replaces next; the developer cannot
                    # fix them (three repair rounds were spent on a stale spec once).
                    return
                if not self._repair_tests(output, reason):
                    return
            elif not self._repair_code(output, reason):
                return

    def _compile_check(self, env: dict[str, str]) -> tuple[str, int]:
        """Runs setup, then the commands that only build or parse the code; ("", 0) when nothing can be checked."""
        output, code = self._run_checks(env, keys=("setup",))
        if code:
            return output, code
        log = self.output_dir / "tmp" / "compile.log"
        # A syntax check runs file by file; all of its failures are reported at once. Stopping at the first one let
        # the Test Agent fix one file per round, and eight broken test files used up every round and attempt.
        syntax_errors = []
        for command in compile_commands(self.project_dir):
            output, code = self._run_reported("Compile", self._command(command, env), env, log, timeout=600)
            if code is None:
                continue  # the toolchain is missing or too slow here; the test commands report it themselves
            if code and command[:2] in (["node", "--check"], ["sh", "-n"]):
                syntax_errors.append(f"[build failed] {' '.join(command)} (exit {code})\n{_clip(output, 1500)}")
            elif code:
                return f"[build failed] {' '.join(command)} (exit {code})\n{output}", code
        if syntax_errors:
            return "\n".join(syntax_errors[:25]), 1
        problems = (missing_js_imports(self.project_dir) + missing_js_packages(self.project_dir)
                    + test_runner_mismatches(self.project_dir, self.manifest) + test_helper_misuse(self.project_dir))
        # The application's problems alone first: the developer cannot fix the tests, and the tests may be right.
        problems = [problem for problem in problems if not _in_tests(problem.split(":", 1)[0])] or problems
        if problems:
            return "[build failed] import check: modules or packages that cannot be loaded as written, or test " \
                   "helpers that are called in a way that cannot work\n" + \
                "\n".join(problems[:30]), 1
        return "", 0

    def _run_checks(self, env: dict[str, str], keys: tuple[str, ...] = ("setup", "unit_test", "api_test", "ui_test")) -> tuple[str, int]:
        """Execute stack-specific commands from the validated project manifest (only those in keys)."""
        env["PYTHONPATH"] = str(self.project_dir)
        temp_dir = self.output_dir / "tmp"
        temp_dir.mkdir(exist_ok=True)
        env.update({"TEMP": str(temp_dir), "TMP": str(temp_dir), "TMPDIR": str(temp_dir)})
        go_mod = self.project_dir / "go.mod"
        if self.manifest.get("setup", [])[:3] == ["go", "mod", "tidy"] and not self.setup_done and go_mod.is_file():
            go_mod.write_text(strip_go_requires(go_mod.read_text(encoding="utf-8")), encoding="utf-8")
            (self.project_dir / "go.sum").unlink(missing_ok=True)
        if self.manifest.get("setup") and self.manifest["setup"][0] in PYTHON_TOOLS and self.python_executable == sys.executable:
            venv_dir = self.project_dir / ".autosdlc-venv"
            try:
                if not (venv_dir / "pyvenv.cfg").is_file():
                    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], cwd=self.project_dir, capture_output=True, text=True, timeout=120, check=True)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                return f"Could not create the project-specific Python environment: {exc}", -1
            self.python_executable = str(venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        commands = [(label, self.manifest[key]) for label, key in
                    (("Setup", "setup"), ("Unit", "unit_test"), ("API", "api_test"), ("UI", "ui_test"))
                    if key in keys and self.manifest.get(key) and (key != "setup" or not self.setup_done)]
        logs = []
        failed = 0
        server_log = temp_dir / "server.log"
        for label, command in commands:
            server = None
            try:
                if label in {"API", "UI"} and self.manifest.get("start"):
                    port = self.manifest["port"]
                    if _port_open(port):
                        return "\n".join(logs + [f"Port {port} is in use by another process; no test was run against the old server"]), -1
                    env["BASE_URL"] = f"http://127.0.0.1:{port}"
                    with server_log.open("w", encoding="utf-8") as log_file:
                        server = subprocess.Popen(
                            self._command(self.manifest["start"], env), cwd=self.project_dir, env=env,
                            stdout=log_file, stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                        )
                    deadline = time.monotonic() + 60
                    while not _port_open(port):
                        if server.poll() is not None:
                            return "\n".join(logs + [SERVER_START_FAILURES[0], _tail(server_log)]), -1
                        if time.monotonic() > deadline:
                            return "\n".join(logs + [SERVER_START_FAILURES[1], _tail(server_log)]), -1
                        time.sleep(0.2)
                resolved = self._command(command, env)
                if label != "Setup":
                    resolved = with_display(resolved, env)
                # Into a log file rather than a pipe, so the live activity view can show the output while it runs.
                output, code = self._run_reported(label, resolved, env, temp_dir / f"{label.lower()}.log", timeout=300)
                if code is None:
                    return "\n".join(logs + [f"The {label} command failed: {output}"]), -1
                logs.append(_clip(f"### {label} tests (exit {code})\n{output}", 20_000))
                if code:
                    if server is not None:
                        logs.append("### Application server log\n" + _tail(server_log))
                    if label == "Setup":
                        return "\n".join(logs), code
                    # The other suites still run: UI tests left out while the API tests failed showed their own
                    # problem only on the last attempt, with no repair left.
                    failed = failed or code
                    continue
                if label == "Setup":
                    self.setup_done = True
            except (subprocess.TimeoutExpired, OSError) as exc:
                return "\n".join(logs + [f"The {label} command failed: {exc}"]), -1
            finally:
                if server is not None:
                    _stop_process(server)
        return "\n".join(logs), failed

    def _command(self, command: list[str], env: dict[str, str]) -> list[str]:
        executable, *args = command
        if executable in {"python", "python3"}:
            return [self.python_executable, *args]
        if executable in {"pip", "pip3"}:
            return [self.python_executable, "-m", "pip", *args]
        if executable == "node" and "--test" in args:
            args = expand_node_test_folders(args, self.project_dir)
        # On Windows npm/pnpm are .cmd files; the full path is resolved to run without a shell (harmless on macOS/Linux too).
        return [shutil.which(executable, path=env.get("PATH")) or executable, *args]

    def deployment(self) -> str:
        # Announced before packaging: a release build (e.g. Rust GUI crates for two targets) can take many minutes.
        self._event("deployment", "started")
        write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
        self._package()
        report = self._document("deployment", "deployment.md", announce=False)
        return report

    def _package(self) -> None:
        """Binary delivery: runs the manifest's package command once the tests pass and lists what landed in dist/.

        The release build can compile code the tests never did (a GUI behind a cargo feature, a //go:build windows
        file), so a failed build goes back to the developer, the tests run again and the build is retried. When the
        user asked for executables and none can be built, the project fails: passing tests are not the deliverable.
        """
        if self.decisions["delivery"] != "binary":
            return
        if not self.test_passed:
            self.package_report = "Packaging skipped: the tests did not pass, so no executables were built."
            self._event("deployment", "package_skipped", "tests did not pass")
            return
        command = self.manifest.get("package")
        if not command:
            self.package_report = ("No executables were built: project.yaml has no package command (the stack cannot "
                                   "produce standalone executables from Linux, so launcher scripts are delivered instead).")
            self._event("deployment", "package_unavailable", "no package command in project.yaml")
            return
        dist = self.project_dir / "dist"
        for round_number in range(BUILD_FIXES + 1):
            # dist/ only ever holds build outputs; executables of an older version must not be offered as current.
            shutil.rmtree(dist, ignore_errors=True)
            env = self._test_env()
            command = self.manifest.get("package") or command
            self._event("deployment", "package_started", " ".join(command))
            output, code = self._run_reported("Package", self._command(command, env), env,
                                              self.output_dir / "tmp" / "package.log", timeout=PACKAGE_TIMEOUT)
            timed_out = code is None
            output, code = output[-3000:], -1 if timed_out else code
            artifacts = sorted(path.relative_to(dist).as_posix() for path in dist.rglob("*") if path.is_file()) if dist.is_dir() else []
            if code == 0 and artifacts:
                self.package_report = "Executables built into dist/: " + ", ".join(artifacts)
                self._event("deployment", "packaged", ", ".join(artifacts))
                return
            self.package_report = f"Packaging failed (exit {code}); no executables are available.\n{output}"
            self._event("deployment", "package_failed", f"exit {code}: {output[-300:]}")
            # A timeout is not a code error the developer could fix.
            if timed_out or round_number == BUILD_FIXES or not self._repair_code(
                    f"THE RELEASE BUILD (`{' '.join(command)}`) FAILED, NO EXECUTABLE WAS PRODUCED:\n{output}",
                    "from the packaging build"):
                break
            # The fix must not break what the tests verified.
            test_output, test_code = self._run_checks(self._test_env())
            self._event("testing", "passed" if test_code == 0 else "failed", "after the packaging fix")
            if test_code:
                self.test_passed = False
                self.last_test_output = test_output
                break
        self.package_failed = True

    def maintenance(self) -> str:
        return self._document("maintenance", "maintenance_report.md")

    def result(self, error: str | None = None) -> RunResult:
        self.services.stop()
        if self.package_failed and not error:
            error = "The executables could not be built: " + self.package_report[:1500]
        if not error and not self.test_passed and self.attempts:
            error = f"The tests did not pass after {self.attempts} attempts; see the test report"
        status = "failed" if error or not self.test_passed else "completed"
        if self.change:
            self._close_change(status, error)
        self._write_project_readme(status)
        self.history.finish(self.run_id, status, self.attempts, error)
        self._record_iteration(status)
        return RunResult(self.run_id, status, self.output_dir, self.project_dir, self.attempts, error)

    def _record_iteration(self, status: str) -> None:
        """Records the finished iteration as a version: a snapshot of the documents and code plus the event range.

        So after a CR or a bugfix the previous version's state, documents and code are not lost.
        """
        number = self.history.next_iteration_number(self.run_id)
        target = self.output_dir / "iterations" / f"v{number}"
        try:
            docs = target / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            for path in self.output_dir.iterdir():
                if path.is_file() and path.suffix in {".md", ".yaml"}:
                    shutil.copy2(path, docs / path.name)
            if self.project_dir.is_dir():
                shutil.copytree(self.project_dir, target / "project", dirs_exist_ok=True,
                                ignore=_snapshot_ignore)
        except OSError as exc:
            self._event("workflow", "snapshot_failed", str(exc))
        if self.change:
            kind = self.change["kind"]
            label = f"v{number} · {self.change['id']} · {CHANGE_KINDS.get(kind, kind)}: {self.change['title']}"
        else:
            kind = "initial" if number == 1 else "rerun"
            label = f"v{number} · Initial version" if number == 1 else f"v{number} · Regeneration"
        self._event("workflow", "iteration_recorded", label)
        self.history.add_iteration(self.run_id, label, kind, self.change and self.change["id"], status, self.attempts, target)

    def _close_change(self, status: str, error: str | None) -> None:
        change = self.change
        done = status == "completed"
        self.history.update_change(self.run_id, change["id"], status="done" if done else "failed")
        entry = (
            f"## {change['id']} · {self._t(CHANGE_KINDS.get(change['kind'], change['kind']))} · {change['title']}\n\n"
            f"- {self._t('Date')}: {local_time().strftime('%Y-%m-%d %H:%M')}\n"
            f"- {self._t('Result')}: "
            + (self._t("Completed, tests passed") if done else self._t("Failed") + (f" ({error[:200]})" if error else " (" + self._t("tests did not pass") + ")"))
            + f"\n- {self._t('Starting stage')}: {change.get('start_stage') or '—'}\n"
            f"- {self._t('Test attempts')}: {self.attempts}\n\n{change['description']}\n"
        )
        path = self.output_dir / "CHANGELOG.md"
        previous = path.read_text(encoding="utf-8") if path.is_file() else f"# {self._t('Changelog')}\n"
        self._write_doc("CHANGELOG.md", previous.rstrip() + "\n\n" + entry)
        self._event("workflow", "change_completed" if done else "change_failed", f"{change['id']} · {change['title']}")

    def _write_project_readme(self, status: str) -> None:
        links = [
            ("Architecture decisions", "decisions.yaml"), ("Project plan", "planning.md"),
            ("Stage questions and answers", "clarifications.yaml"),
            ("Requirements", "requirements.md"), ("Architecture and diagrams", "architecture.md"),
            ("Architecture review report", "design_review.md"), ("Changelog", "CHANGELOG.md"),
            ("Traceability matrix", "traceability.md"),
            ("Test report", "test_report.md"), ("Deployment guide", "deployment.md"),
            ("Maintenance plan", "maintenance_report.md"),
        ]
        docs = "\n".join(f"- [{self._t(label)}](docs/{name})" for label, name in links if (self.project_dir / "docs" / name).exists())
        content = (
            f"# {self._t('Generated project')}\n\n{self.idea}\n\n"
            + self._t("SDLC status: **{status}**. Test attempts: {attempts}.",
                      status=self._t("completed") if status == "completed" else self._t("failed"), attempts=self.attempts)
            + f"\n\n## {self._t('Documents')}\n\n{docs}\n\n"
            + self._delivery_section()
        )
        (self.project_dir / "README.md").write_text(content, encoding="utf-8")

    def _delivery_section(self) -> str:
        """The README's how-to-run part, matching the delivery decision."""
        delivery = self.decisions["delivery"]
        if delivery == "docker":
            return (f"## {self._t('Running with Docker Compose')}\n\n"
                    + self._t("Copy `.env.example` to `.env` and fill in the required values, then run `docker compose up --build`.") + "\n"
                    + self._t("See the [deployment guide](docs/deployment.md) for checks and rollback.") + "\n")
        guide = self._t("Details are in the [deployment guide](docs/deployment.md).") + "\n"
        if delivery == "binary":
            dist = self.project_dir / "dist"
            files = sorted(path.name for path in dist.iterdir() if path.is_file()) if dist.is_dir() else []
            body = (self._t("Ready-to-run executables are in `dist/`; pick the one for your operating system:") + "\n\n"
                    + "\n".join(f"- `dist/{name}`" for name in files)) if files else self._t("No executables were built.")
            return f"## {self._t('Executables')}\n\n{body}\n\n{guide}"
        if delivery == "script":
            return (f"## {self._t('How to run')}\n\n" + self._t("Run `./run.sh` on Linux/macOS or `run.bat` on Windows.") + " "
                    + self._t("The launchers run the source code, so the language runtime (e.g. Python or Node.js) must be "
                              "installed on the computer.") + f"\n\n{guide}")
        return guide


def _run_crew_flow(session: RunSession) -> None:
    from crewai.flow.flow import Flow, listen, start

    class SDLCFlow(Flow):
        @start()
        def plan(self):
            return session.execute("planning", session.planning)

        @listen(plan)
        def analyse(self, _previous):
            return session.execute("analysis", session.analysis)

        @listen(analyse)
        def design(self, _previous):
            return session.execute("design", session.design)

        @listen(design)
        def review(self, _previous):
            return session.execute("review", session.review)

        @listen(review)
        def develop(self, _previous):
            return session.execute("development", session.development)

        @listen(develop)
        def test(self, _previous):
            return session.execute("testing", session.testing)

        @listen(test)
        def deploy(self, _previous):
            return session.execute("deployment", session.deployment)

        @listen(deploy)
        def maintain(self, _previous):
            return session.execute("maintenance", session.maintenance)

    SDLCFlow().kickoff()


def run_project(
    idea: str,
    *,
    workspace: Path | str | None = None,
    max_repairs: int = 2,
    api_key: str | None = None,
    model: str | None = None,
    engine: StageEngine | None = None,
    decisions: dict[str, str] | None = None,
    output: Path | str | None = None,
    on_event: Callable[[str, str, str], None] | None = None,
    on_question: Callable[[str, str, str], str] | None = None,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    stage_models: dict[str, str] | None = None,
    keep_tests: bool = False,
) -> RunResult:
    workspace = Path(workspace or Path.cwd()).resolve()
    if resume_run_id:
        row = History(workspace / "runs" / "history.sqlite3").get(resume_run_id)
        if row is None or row["status"] != "paused":
            raise ValueError("No paused run to resume was found")
        idea = row["idea"]
        decisions = yaml.safe_load((Path(row["output_dir"]) / "decisions.yaml").read_text(encoding="utf-8"))
        output = Path(row["project_dir"])
        model = model or row["model"]
        stage_models = read_stage_models(row) if stage_models is None else stage_models
        run_id = resume_run_id
    if not idea.strip() or len(idea) > 1000:
        raise ValueError("The project idea must be 1–1000 characters")
    if not 0 <= max_repairs <= 3:
        raise ValueError("max_repairs must be between 0 and 3")
    decisions = validate_decisions(decisions)
    stage_models = validate_stage_models(stage_models)
    engine = engine or CrewAIEngine(api_key=api_key, model=model)
    if hasattr(engine, "stage_models"):
        engine.stage_models = stage_models
    session = RunSession(idea, workspace, engine, max_repairs, decisions, Path(output) if output is not None else None,
                         on_event, on_question, run_id, bool(resume_run_id), stage_models, keep_tests=keep_tests)
    # Heartbeat: if the process dies the UI can pause the run as "interrupted".
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(10):
            session.history.heartbeat(session.run_id)

    threading.Thread(target=beat, daemon=True, name=f"autosdlc-heartbeat-{session.run_id}").start()
    try:
        _run_crew_flow(session)
        return session.result()
    except RunPaused:
        return session.pause()
    except Exception as exc:
        # A model call cut off by Stop can surface wrapped in the agent framework's own error.
        if session.history.pause_requested(session.run_id):
            return session.pause()
        session.record_failure(exc)
        session._event("workflow", "failed", str(exc))
        return session.result(error=str(exc))
    finally:
        stop.set()

