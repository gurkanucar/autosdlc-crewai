"""SDLC stages, artifact creation, and the bounded test/repair loop."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from .crewai_engine import CrewAIEngine
from .deployment import write_deployment_files
from .files import write_generated_files
from .intake import validate_decisions
from .project import read_project_manifest
from .storage import History


@dataclass
class RunResult:
    id: str
    status: str
    output_dir: Path
    project_dir: Path
    attempts: int
    error: str | None = None


class StageEngine(Protocol):
    def document(self, stage: str, idea: str, context: str) -> str: ...
    def code(self, idea: str, docs: str, feedback: str | None = None) -> dict[str, str]: ...
    def tests(self, idea: str, docs: str, files: dict[str, str]) -> dict[str, str]: ...


class RunSession:
    def __init__(
        self,
        idea: str,
        workspace: Path,
        engine: StageEngine,
        max_repairs: int,
        decisions: dict[str, str],
        output: Path | None = None,
    ):
        self.idea = idea.strip()
        self.workspace = Path(workspace).resolve()
        self.run_id = uuid.uuid4().hex[:12]
        self.output_dir = self.workspace / "runs" / self.run_id
        self.project_dir = Path(output).resolve() if output is not None else self.output_dir / "project"
        if self.project_dir.exists():
            raise ValueError(f"Project output path already exists: {self.project_dir}")
        self.output_dir.mkdir(parents=True)
        self.project_dir.mkdir(parents=True)
        (self.project_dir / "docs").mkdir()
        self.history = History(self.workspace / "runs" / "history.sqlite3")
        self.history.create(self.run_id, self.idea, self.output_dir, self.project_dir)
        self.engine = engine
        self.decisions = decisions
        (self.output_dir / "decisions.yaml").write_text(yaml.safe_dump(decisions, sort_keys=False), encoding="utf-8")
        (self.project_dir / "docs" / "decisions.yaml").write_text(yaml.safe_dump(decisions, sort_keys=False), encoding="utf-8")
        self.max_repairs = max_repairs
        self.docs: dict[str, str] = {}
        self.code_files: dict[str, str] = {}
        self.manifest: dict = {}
        self.setup_done = False
        self.python_executable = sys.executable
        self.attempts = 0
        self.test_passed = False
        self.last_test_output = ""

    def _event(self, stage: str, status: str, detail: str = "") -> None:
        self.history.event(self.run_id, stage, status, detail)

    def _document(self, stage: str, filename: str) -> str:
        self._event(stage, "started")
        context = "Architecture choices:\n" + yaml.safe_dump(self.decisions, sort_keys=False) + "\n\n" + "\n\n".join(self.docs.values())
        if stage in {"deployment", "maintenance"}:
            context += (
                f"\n\nActual test status: {'PASS' if self.test_passed else 'FAIL'}"
                f" after {self.attempts} attempt(s).\n"
                f"Latest pytest output:\n{self.last_test_output[-5000:]}"
            )
        content = self.engine.document(stage, self.idea, context)
        if not content.strip():
            raise ValueError(f"{stage} agent returned an empty document")
        if stage == "analysis" and "FR-" not in content:
            raise ValueError("Analysis document must include numbered functional requirements")
        if stage == "design" and content.count("```mermaid") < 2:
            raise ValueError("Architecture document must include component and sequence Mermaid diagrams")
        if stage == "design" and self.decisions["database"] != "none" and "erDiagram" not in content:
            raise ValueError("Architecture document must include an ER diagram for the selected database")
        if stage in {"deployment", "maintenance"}:
            content = (
                f"> Verified pytest status: **{'PASS' if self.test_passed else 'FAIL'}** "
                f"after {self.attempts} attempt(s).\n\n{content}"
            )
        self.docs[stage] = content
        (self.output_dir / filename).write_text(content, encoding="utf-8")
        (self.project_dir / "docs" / filename).write_text(content, encoding="utf-8")
        self._event(stage, "completed", filename)
        return content

    def planning(self) -> str:
        return self._document("planning", "planning.md")

    def analysis(self) -> str:
        return self._document("analysis", "requirements.md")

    def design(self) -> str:
        return self._document("design", "architecture.md")

    def development(self) -> str:
        self._event("development", "started")
        self.code_files = self.engine.code(self.idea, "Architecture choices:\n" + yaml.safe_dump(self.decisions) + "\n\n" + "\n\n".join(self.docs.values()))
        write_generated_files(self.project_dir, self.code_files)
        self.manifest = read_project_manifest(self.project_dir, self.decisions)
        write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
        self._event("development", "completed", ", ".join(self.code_files))
        return "implementation ready"

    def testing(self) -> str:
        self._event("testing", "started")
        tests = self.engine.tests(
            self.idea, "Architecture choices:\n" + yaml.safe_dump(self.decisions) + "\n\n" + "\n\n".join(self.docs.values()), self.code_files
        )
        if not any(name.startswith(("tests/", "test/")) for name in tests):
            raise ValueError("Tester must supply tests under tests/ or test/")
        if any(not name.startswith(("tests/", "test/")) for name in tests):
            raise ValueError("Tester may only write test files")
        write_generated_files(self.project_dir, tests)
        attempt_dir = self.output_dir / "test_attempts"
        attempt_dir.mkdir()
        reports = []
        for number in range(1, self.max_repairs + 2):
            self.attempts = number
            env = os.environ.copy()
            for secret_name in ("OPENCODE_API_KEY", "OPENAI_API_KEY"):
                env.pop(secret_name, None)
            env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            output, code = self._run_checks(env)
            passed = code == 0
            self.last_test_output = output
            (attempt_dir / f"attempt-{number}.txt").write_text(output, encoding="utf-8")
            reports.append(
                f"## Attempt {number}\nExit code: {code}\n\n```text\n{output}\n```\n"
            )
            self._event("testing", "passed" if passed else "failed", f"attempt {number}")
            if passed:
                self.test_passed = True
                break
            if number <= self.max_repairs:
                self._event("development", "repair_started", f"from test attempt {number}")
                replacements = self.engine.code(
                    self.idea, "Architecture choices:\n" + yaml.safe_dump(self.decisions) + "\n\n" + "\n\n".join(self.docs.values()), feedback=output
                )
                write_generated_files(self.project_dir, replacements)
                self.code_files.update(replacements)
                self.manifest = read_project_manifest(self.project_dir, self.decisions)
                self.setup_done = False
                self._event("development", "repair_completed", ", ".join(replacements))
        summary = "PASS" if self.test_passed else "FAIL"
        report = f"# Test report\n\nResult: **{summary}** after {self.attempts} attempt(s).\n\n"
        report += "\n".join(reports)
        (self.output_dir / "test_report.md").write_text(report, encoding="utf-8")
        (self.project_dir / "docs" / "test_report.md").write_text(report, encoding="utf-8")
        return summary

    def _run_checks(self, env: dict[str, str]) -> tuple[str, int]:
        """Execute stack-specific commands from the validated project manifest."""
        env["PYTHONPATH"] = str(self.project_dir)
        temp_dir = self.output_dir / "tmp"
        temp_dir.mkdir(exist_ok=True)
        env.update({"TEMP": str(temp_dir), "TMP": str(temp_dir), "TMPDIR": str(temp_dir)})
        if self.manifest.get("setup") and self.manifest["setup"][0] in {"python", "python3"} and self.python_executable == sys.executable:
            venv_dir = self.project_dir / ".autosdlc-venv"
            try:
                subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], cwd=self.project_dir, capture_output=True, text=True, timeout=120, check=True)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                return f"Could not create project Python environment: {exc}", -1
            self.python_executable = str(venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        commands = [(label, self.manifest[key]) for label, key in
                    (("Setup", "setup"), ("Unit", "unit_test"), ("API", "api_test"), ("UI", "ui_test"))
                    if self.manifest.get(key) and (key != "setup" or not self.setup_done)]
        logs = []
        for label, command in commands:
            server = None
            try:
                if label in {"API", "UI"} and self.manifest.get("start"):
                    port = self.manifest["port"]
                    env["BASE_URL"] = f"http://127.0.0.1:{port}"
                    server = subprocess.Popen(
                        self._command(self.manifest["start"]),
                        cwd=self.project_dir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    for _ in range(50):
                        if server.poll() is not None:
                            return "\n".join(logs + ["UI server exited before browser tests"]), -1
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                                break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        return "\n".join(logs + ["UI server did not start"]), -1
                process = subprocess.run(self._command(command), cwd=self.project_dir, env=env, capture_output=True, text=True, timeout=180, check=False)
                logs.append(f"### {label} tests (exit {process.returncode})\n{process.stdout}\n{process.stderr}"[-20_000:])
                if process.returncode:
                    return "\n".join(logs), process.returncode
                if label == "Setup":
                    self.setup_done = True
            except (subprocess.TimeoutExpired, OSError) as exc:
                return "\n".join(logs + [f"{label} command failed: {exc}"]), -1
            finally:
                if server is not None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
        return "\n".join(logs), 0

    def _command(self, command: list[str]) -> list[str]:
        return [self.python_executable if i == 0 and part in {"python", "python3"} else part for i, part in enumerate(command)]

    def deployment(self) -> str:
        write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
        report = self._document("deployment", "deployment.md")
        return report

    def maintenance(self) -> str:
        return self._document("maintenance", "maintenance_report.md")

    def result(self, error: str | None = None) -> RunResult:
        status = "failed" if error or not self.test_passed else "completed"
        self._write_project_readme(status)
        self.history.finish(self.run_id, status, self.attempts, error)
        return RunResult(self.run_id, status, self.output_dir, self.project_dir, self.attempts, error)

    def _write_project_readme(self, status: str) -> None:
        links = [
            ("Architecture decisions", "decisions.yaml"), ("Plan", "planning.md"),
            ("Requirements and traceability", "requirements.md"), ("Architecture and diagrams", "architecture.md"),
            ("Test report", "test_report.md"), ("Deployment runbook", "deployment.md"),
            ("Maintenance plan", "maintenance_report.md"),
        ]
        docs = "\n".join(f"- [{label}](docs/{name})" for label, name in links if (self.project_dir / "docs" / name).exists())
        content = (
            f"# Generated project\n\n{self.idea}\n\n"
            f"SDLC status: **{status}**. Test attempts: {self.attempts}.\n\n"
            f"## Documentation\n\n{docs}\n\n"
            "## Run with Docker Compose\n\n"
            "Copy `.env.example` to `.env`, fill required values, then run `docker compose up --build`.\n"
            "See [deployment runbook](docs/deployment.md) for checks and rollback.\n"
        )
        (self.project_dir / "README.md").write_text(content, encoding="utf-8")


def _run_crew_flow(session: RunSession) -> None:
    from crewai.flow.flow import Flow, listen, start

    class SDLCFlow(Flow):
        @start()
        def plan(self):
            return session.planning()

        @listen(plan)
        def analyse(self, _previous):
            return session.analysis()

        @listen(analyse)
        def design(self, _previous):
            return session.design()

        @listen(design)
        def develop(self, _previous):
            return session.development()

        @listen(develop)
        def test(self, _previous):
            return session.testing()

        @listen(test)
        def deploy(self, _previous):
            return session.deployment()

        @listen(deploy)
        def maintain(self, _previous):
            return session.maintenance()

    SDLCFlow().kickoff()


def run_project(
    idea: str,
    *,
    workspace: Path | str = Path.cwd(),
    max_repairs: int = 2,
    api_key: str | None = None,
    model: str | None = None,
    engine: StageEngine | None = None,
    decisions: dict[str, str] | None = None,
    output: Path | str | None = None,
) -> RunResult:
    if not idea.strip() or len(idea) > 1000:
        raise ValueError("Project idea must be 1–1000 characters")
    if not 0 <= max_repairs <= 3:
        raise ValueError("max_repairs must be between 0 and 3")
    decisions = validate_decisions(decisions)
    engine = engine or CrewAIEngine(api_key=api_key, model=model)
    session = RunSession(idea, Path(workspace), engine, max_repairs, decisions, Path(output) if output is not None else None)
    try:
        _run_crew_flow(session)
        return session.result()
    except Exception as exc:
        session._event("workflow", "failed", str(exc))
        return session.result(error=str(exc))

