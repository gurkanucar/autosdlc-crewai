"""SDLC stages, artifact creation, and the bounded test/repair loop."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .crewai_engine import CrewAIEngine
from .files import write_generated_files
from .storage import History


REQUIREMENTS = "fastapi>=0.115,<1\nuvicorn>=0.30,<1\n"
DOCKERFILE = """FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /data
ENV TASK_DB_PATH=/data/tasks.db
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""


@dataclass
class RunResult:
    id: str
    status: str
    output_dir: Path
    attempts: int
    error: str | None = None


class RunSession:
    def __init__(
        self,
        idea: str,
        workspace: Path,
        engine: CrewAIEngine,
        max_repairs: int,
    ):
        self.idea = idea.strip()
        self.workspace = Path(workspace).resolve()
        self.run_id = uuid.uuid4().hex[:12]
        self.output_dir = self.workspace / "runs" / self.run_id
        self.project_dir = self.output_dir / "project"
        self.output_dir.mkdir(parents=True)
        self.project_dir.mkdir()
        self.history = History(self.workspace / "runs" / "history.sqlite3")
        self.history.create(self.run_id, self.idea, self.output_dir)
        self.engine = engine
        self.max_repairs = max_repairs
        self.docs: dict[str, str] = {}
        self.code_files: dict[str, str] = {}
        self.attempts = 0
        self.test_passed = False
        self.last_test_output = ""

    def _event(self, stage: str, status: str, detail: str = "") -> None:
        self.history.event(self.run_id, stage, status, detail)

    def _document(self, stage: str, filename: str) -> str:
        self._event(stage, "started")
        context = "\n\n".join(self.docs.values())
        content = self.engine.document(stage, self.idea, context)
        if not content.strip():
            raise ValueError(f"{stage} agent returned an empty document")
        self.docs[stage] = content
        (self.output_dir / filename).write_text(content, encoding="utf-8")
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
        self.code_files = self.engine.code(self.idea, "\n\n".join(self.docs.values()))
        if "app/main.py" not in self.code_files:
            raise ValueError("Developer must supply app/main.py")
        write_generated_files(self.project_dir, self.code_files)
        (self.project_dir / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
        self._event("development", "completed", ", ".join(self.code_files))
        return "implementation ready"

    def testing(self) -> str:
        self._event("testing", "started")
        tests = self.engine.tests(
            self.idea, "\n\n".join(self.docs.values()), self.code_files
        )
        if "tests/test_app.py" not in tests:
            raise ValueError("Tester must supply tests/test_app.py")
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
            try:
                process = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                    cwd=self.project_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                output = (process.stdout + "\n" + process.stderr)[-20_000:]
                passed = process.returncode == 0
                code = process.returncode
            except subprocess.TimeoutExpired as exc:
                output = f"pytest timed out after {exc.timeout} seconds"
                passed = False
                code = -1
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
                    self.idea, "\n\n".join(self.docs.values()), feedback=output
                )
                if "app/main.py" not in replacements:
                    raise ValueError("Developer repair must supply app/main.py")
                write_generated_files(self.project_dir, replacements)
                self.code_files.update(replacements)
                self._event("development", "repair_completed", ", ".join(replacements))
        summary = "PASS" if self.test_passed else "FAIL"
        report = f"# Test report\n\nResult: **{summary}** after {self.attempts} attempt(s).\n\n"
        report += "\n".join(reports)
        (self.output_dir / "test_report.md").write_text(report, encoding="utf-8")
        return summary

    def deployment(self) -> str:
        self._event("deployment", "started")
        (self.project_dir / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
        status = "ready for local Docker build" if self.test_passed else "blocked by failed tests"
        (self.output_dir / "deployment.md").write_text(
            "# Deployment\n\n"
            f"Status: **{status}**.\n\n"
            "Build: `docker build -t autosdlc-example .` from `project/`.\n\n"
            "Run: `docker run --rm -p 8000:8000 -v task_data:/data autosdlc-example`.\n",
            encoding="utf-8",
        )
        self._event("deployment", "completed", status)
        return status

    def maintenance(self) -> str:
        self._event("maintenance", "started")
        items = [
            "Review generated source and tests before any real deployment.",
            "Add authentication, migrations, monitoring and backups for production use.",
            "Keep Python dependencies updated and rerun pytest after changes.",
        ]
        if not self.test_passed:
            items.insert(0, "Resolve the remaining pytest failures in test_report.md.")
        report = (
            "# Maintenance report\n\n"
            f"Latest test result: **{'PASS' if self.test_passed else 'FAIL'}**.\n\n"
            "## Follow-up work\n" + "\n".join(f"- {item}" for item in items) + "\n"
        )
        (self.output_dir / "maintenance_report.md").write_text(report, encoding="utf-8")
        self._event("maintenance", "completed")
        return report

    def result(self, error: str | None = None) -> RunResult:
        status = "failed" if error or not self.test_passed else "completed"
        self.history.finish(self.run_id, status, self.attempts, error)
        return RunResult(self.run_id, status, self.output_dir, self.attempts, error)


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
    engine: CrewAIEngine | None = None,
) -> RunResult:
    if not idea.strip() or len(idea) > 1000:
        raise ValueError("Project idea must be 1–1000 characters")
    if not 0 <= max_repairs <= 3:
        raise ValueError("max_repairs must be between 0 and 3")
    engine = engine or CrewAIEngine(api_key=api_key, model=model)
    session = RunSession(idea, Path(workspace), engine, max_repairs)
    try:
        _run_crew_flow(session)
        return session.result()
    except Exception as exc:
        session._event("workflow", "failed", str(exc))
        return session.result(error=str(exc))

