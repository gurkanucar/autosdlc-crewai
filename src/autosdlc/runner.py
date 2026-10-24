"""SDLC stages, artifact creation, and the bounded test/repair loop."""

from __future__ import annotations

import os
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
from .files import write_generated_files
from .intake import validate_decisions
from .project import read_project_manifest
from .sections import NO_CHANGE, NO_CHANGE_TOKENS, apply_patch
from .storage import History, local_time, now
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


def _run_logged(command: list[str], cwd: Path, env: dict[str, str], log: Path, timeout: int) -> tuple[str, int | None]:
    """Runs a build command with its output in a file; the code is None when it could not start or timed out.

    The command runs in its own process group: on a timeout the compilers it started are stopped too, and nothing
    waits for pipes that a leftover child still holds open (cargo -> rustc, sh build.sh -> go build).
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log_file, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != "nt")
    except OSError as exc:
        return str(exc), None
    try:
        code = process.wait(timeout=timeout)
        return _tail(log, 20_000), code
    except subprocess.TimeoutExpired:
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
    r"|^\s*(/?[^\s:\"]+\.\w+):\d+\s*$",
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
    ):
        self.idea = idea.strip()
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
            self.history.create(self.run_id, self.idea, self.output_dir, self.project_dir, getattr(engine, "model", None))
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
        if hasattr(engine, "output_listener"):
            engine.output_listener = self._live_output
        if hasattr(getattr(engine, "llm", None), "call_log"):
            engine.llm.call_log = self._log_call
            engine.llm.activity = self._activity
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
            temporary = live_dir / "activity.json.tmp"
            temporary.write_text(json.dumps({**record, "updated_at": now()}, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, live_dir / "activity.json")
        except OSError:
            pass  # Only a convenience; the UI may be reading the file at that moment.

    def _run_reported(self, label: str, command: list[str], env: dict[str, str], log: Path, timeout: int) -> tuple[str, int | None]:
        """_run_logged, with the command shown in the live activity view while it runs."""
        current = {"kind": "command", "label": label, "command": " ".join(command), "started_at": now(),
                   "log": log.relative_to(self.output_dir).as_posix()}
        self._activity({**current, "phase": "running"})
        output, code = _run_logged(command, self.project_dir, env, log, timeout=timeout)
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
        replacements = self._without_docs({name: content for name, content in replacements.items() if name not in rejected})
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
        existing = self._test_files_on_disk() if self.change else {}
        if existing:
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
                gate_feedback = (
                    f"YOUR PREVIOUS TEST OUTPUT WAS REJECTED: {problem}. Write all test files under the tests/ folder "
                    "(e.g. tests/test_x.py); rewrite test files at the root under tests/ as well.\n\n"
                )
            else:
                raise ValueError(problem)
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
        for number in range(1, self.max_repairs + 2):
            self.attempts = number
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
            if number <= self.max_repairs and failure_is_in_tests(output):
                # The error is in the Test Agent's own files; the developer is not allowed to touch them, so the author repairs them.
                if self._repair_tests(output, f"from test attempt {number}"):
                    continue
            if number <= self.max_repairs:
                self._repair_code(output, f"from test attempt {number}")
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

    def _repair_code(self, output: str, reason: str) -> bool:
        """The Development Agent fixes the application from the failure output; False when nothing was changed."""
        self._event("development", "repair_started", reason)
        replacements = self._with_questions("development", lambda: self.engine.code(
            self.idea, self._context("development"), feedback=output
        ))
        # The tests belong to the Test Agent; the developer cannot make them pass by changing them.
        rejected = sorted(name for name in replacements if name.startswith(TEST_DIRS))
        replacements = self._without_docs({name: content for name, content in replacements.items() if name not in rejected})
        if hasattr(self.engine, "current_files"):
            self.engine.current_files.update(self.test_files)
        if rejected:
            self._event("development", "tests_protected", ", ".join(rejected))
        if not replacements:
            return False
        write_generated_files(self.project_dir, replacements)
        self.code_files.update(replacements)
        self._save_code_files()
        self._load_manifest()
        self.setup_done = False
        self._event("development", "repair_completed", ", ".join(replacements))
        return True

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
            if with_tests and failure_is_in_tests(output):
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
        for command in compile_commands(self.project_dir):
            output, code = self._run_reported("Compile", self._command(command, env), env, log, timeout=600)
            if code is None:
                continue  # the toolchain is missing or too slow here; the test commands report it themselves
            if code:
                return f"[build failed] {' '.join(command)} (exit {code})\n{output}", code
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
                            return "\n".join(logs + ["The application server exited before the tests", _tail(server_log)]), -1
                        if time.monotonic() > deadline:
                            return "\n".join(logs + ["The application server did not start within 60 seconds", _tail(server_log)]), -1
                        time.sleep(0.2)
                resolved = self._command(command, env)
                if label != "Setup":
                    resolved = with_display(resolved, env)
                # Into a log file rather than a pipe, so the live activity view can show the output while it runs.
                output, code = self._run_reported(label, resolved, env, temp_dir / f"{label.lower()}.log", timeout=300)
                if code is None:
                    return "\n".join(logs + [f"The {label} command failed: {output}"]), -1
                logs.append(f"### {label} tests (exit {code})\n{output}"[-20_000:])
                if code:
                    if server is not None:
                        logs.append("### Application server log\n" + _tail(server_log))
                    return "\n".join(logs), code
                if label == "Setup":
                    self.setup_done = True
            except (subprocess.TimeoutExpired, OSError) as exc:
                return "\n".join(logs + [f"The {label} command failed: {exc}"]), -1
            finally:
                if server is not None:
                    _stop_process(server)
        return "\n".join(logs), 0

    def _command(self, command: list[str], env: dict[str, str]) -> list[str]:
        executable, *args = command
        if executable in {"python", "python3"}:
            return [self.python_executable, *args]
        if executable in {"pip", "pip3"}:
            return [self.python_executable, "-m", "pip", *args]
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
        run_id = resume_run_id
    if not idea.strip() or len(idea) > 1000:
        raise ValueError("The project idea must be 1–1000 characters")
    if not 0 <= max_repairs <= 3:
        raise ValueError("max_repairs must be between 0 and 3")
    decisions = validate_decisions(decisions)
    engine = engine or CrewAIEngine(api_key=api_key, model=model)
    session = RunSession(idea, workspace, engine, max_repairs, decisions, Path(output) if output is not None else None, on_event, on_question, run_id, bool(resume_run_id))
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
        session.record_failure(exc)
        session._event("workflow", "failed", str(exc))
        return session.result(error=str(exc))
    finally:
        stop.set()

