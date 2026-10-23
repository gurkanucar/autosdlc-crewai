"""SDLC stages, artifact creation, and the bounded test/repair loop."""

from __future__ import annotations

import os
import json
import re
import socket
import subprocess
import sys
import time
import uuid
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import yaml

from .crewai_engine import ClarificationNeeded, CrewAIEngine
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


class RunPaused(Exception):
    """İş, sonraki SDLC aşamasına geçmeden duraklatıldı."""


def default_project_dir(workspace: Path, idea: str, run_id: str) -> Path:
    normalized = unicodedata.normalize("NFKD", idea.lower().replace("ı", "i"))
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:48].rstrip("-") or "proje"
    return workspace / "projects" / f"{slug}-{run_id}"


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
                raise ValueError("Yalnızca duraklatılmış bir çalışma sürdürülebilir")
            self.output_dir = Path(existing["output_dir"])
            self.project_dir = Path(existing["project_dir"])
            if not self.output_dir.is_dir() or not self.project_dir.is_dir():
                raise ValueError("Devam etmek için proje ve rapor klasörleri bulunmalı")
        else:
            self.output_dir = self.workspace / "runs" / self.run_id
            self.project_dir = Path(output).resolve() if output is not None else default_project_dir(self.workspace, self.idea, self.run_id)
            if self.project_dir.exists():
                raise ValueError(f"Proje klasörü zaten mevcut: {self.project_dir}")
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
        self.docs: dict[str, str] = {}
        self.clarifications: list[dict[str, str]] = []
        self.code_files: dict[str, str] = {}
        self.manifest: dict = {}
        self.setup_done = False
        self.python_executable = sys.executable
        self.attempts = 0
        self.test_passed = False
        self.last_test_output = ""
        self.completed_stages: set[str] = set()
        if resume:
            self._load_checkpoint(existing)
            if not self.history.resume(self.run_id):
                raise ValueError("Çalışma zaten sürdürülüyor")

    def _load_checkpoint(self, row: dict) -> None:
        filenames = {
            "planning": "planning.md", "analysis": "requirements.md", "design": "architecture.md",
            "deployment": "deployment.md", "maintenance": "maintenance_report.md",
        }
        for stage, name in filenames.items():
            path = self.output_dir / name
            if path.is_file():
                self.docs[stage] = path.read_text(encoding="utf-8")
        clarifications = self.output_dir / "clarifications.yaml"
        if clarifications.is_file():
            self.clarifications = yaml.safe_load(clarifications.read_text(encoding="utf-8")) or []
        bundle = self.output_dir / "code_files.json"
        if bundle.is_file():
            self.code_files = json.loads(bundle.read_text(encoding="utf-8"))
            if hasattr(self.engine, "current_files"):
                self.engine.current_files.update(self.code_files)
            self.manifest = read_project_manifest(self.project_dir, self.decisions)
        self.completed_stages = {
            event["stage"] for event in row["events"]
            if event["status"] == "completed" or (event["stage"] == "testing" and event["status"] == "passed")
        }
        self.attempts = row["attempts"]
        self.test_passed = "testing" in self.completed_stages
        attempt = self.output_dir / "test_attempts" / f"attempt-{self.attempts}.txt"
        if attempt.is_file():
            self.last_test_output = attempt.read_text(encoding="utf-8")

    def _check_pause(self) -> None:
        if self.history.pause_requested(self.run_id):
            raise RunPaused()

    def execute(self, stage: str, operation: Callable[[], str]) -> str:
        self._check_pause()
        if stage in self.completed_stages:
            return "Aşama daha önce tamamlandı"
        result = operation()
        self._check_pause()
        return result

    def pause(self) -> RunResult:
        self._event("workflow", "paused", "Kullanıcı isteğiyle duraklatıldı")
        self.history.pause(self.run_id, self.attempts)
        return RunResult(self.run_id, "paused", self.output_dir, self.project_dir, self.attempts)

    def _event(self, stage: str, status: str, detail: str = "") -> None:
        self.history.event(self.run_id, stage, status, detail)
        if self.on_event is not None:
            self.on_event(stage, status, detail)

    def _document(self, stage: str, filename: str) -> str:
        self._event(stage, "started")
        context = self._context()
        if stage in {"deployment", "maintenance"}:
            context += (
                f"\n\nGerçek test sonucu: {'BAŞARILI' if self.test_passed else 'BAŞARISIZ'}"
                f" ({self.attempts} deneme).\n"
                f"Son test çıktısı:\n{self.last_test_output[-5000:]}"
            )
        content = self._with_questions(
            stage, lambda answer_context: self.engine.document(stage, self.idea, context + answer_context)
        )
        if not content.strip():
            raise ValueError(f"{stage} ajanı boş belge döndürdü")
        if stage == "analysis" and "FR-" not in content:
            raise ValueError("Analiz belgesinde numaralı işlevsel gereksinimler bulunmalı")
        if stage == "design" and content.count("```mermaid") < 2:
            raise ValueError("Mimari belgede Mermaid bileşen ve sıralama diyagramları bulunmalı")
        if stage == "design" and self.decisions["database"] != "none" and "erDiagram" not in content:
            raise ValueError("Seçilen veritabanı için mimari belgede ER diyagramı bulunmalı")
        if stage in {"deployment", "maintenance"}:
            content = (
                f"> Doğrulanan test sonucu: **{'BAŞARILI' if self.test_passed else 'BAŞARISIZ'}** "
                f"({self.attempts} deneme).\n\n{content}"
            )
        self.docs[stage] = content
        (self.output_dir / filename).write_text(content, encoding="utf-8")
        (self.project_dir / "docs" / filename).write_text(content, encoding="utf-8")
        self._event(stage, "completed", filename)
        return content

    def _with_questions(self, stage: str, operation: Callable[[str], str | dict]) -> str | dict:
        answers = ""
        for _ in range(3):
            try:
                return operation(answers)
            except ClarificationNeeded as exc:
                if self.on_question is None:
                    raise ValueError(f"{stage} ajanı şu kararı istedi: {exc.question}") from exc
                self._event(stage, "question", exc.question)
                answer = self.on_question(self.run_id, stage, exc.question).strip()
                if not answer:
                    raise ValueError("Boş kullanıcı yanıtıyla devam edilemez")
                answers += f"\n\nKullanıcının yanıtladığı soru: {exc.question}\nYanıt: {answer}"
                self.clarifications.append({"stage": stage, "question": exc.question, "answer": answer})
                clarifications_yaml = yaml.safe_dump(self.clarifications, allow_unicode=True, sort_keys=False)
                (self.output_dir / "clarifications.yaml").write_text(clarifications_yaml, encoding="utf-8")
                (self.project_dir / "docs" / "clarifications.yaml").write_text(clarifications_yaml, encoding="utf-8")
                self._event(stage, "answered", "Kullanıcı yanıtladı")
        raise ValueError(f"{stage} ajanı açıklama sonrasında da ilerleyemedi")

    def _context(self) -> str:
        context = "Mimari kararlar ve kullanıcı kısıtları:\n" + yaml.safe_dump(self.decisions, sort_keys=False)
        if self.clarifications:
            context += "\n\nKullanıcının aşama sorularına verdiği yanıtlar:\n" + yaml.safe_dump(
                self.clarifications, allow_unicode=True, sort_keys=False
            )
        return context + "\n\n" + "\n\n".join(self.docs.values())

    def planning(self) -> str:
        return self._document("planning", "planning.md")

    def analysis(self) -> str:
        return self._document("analysis", "requirements.md")

    def design(self) -> str:
        return self._document("design", "architecture.md")

    def development(self) -> str:
        self._event("development", "started")
        context = self._context()
        self.code_files = self._with_questions("development", lambda answers: self.engine.code(self.idea, context + answers))
        write_generated_files(self.project_dir, self.code_files)
        self._save_code_files()
        self.manifest = read_project_manifest(self.project_dir, self.decisions)
        write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
        self._event("development", "completed", ", ".join(self.code_files))
        return "uygulama hazır"

    def _save_code_files(self) -> None:
        (self.output_dir / "code_files.json").write_text(
            json.dumps(self.code_files, ensure_ascii=False), encoding="utf-8"
        )

    def testing(self) -> str:
        self._event("testing", "started")
        context = self._context()
        tests = self._with_questions("testing", lambda answers: self.engine.tests(self.idea, context + answers, self.code_files))
        if not any(name.startswith(("tests/", "test/")) for name in tests):
            raise ValueError("Test ajanı tests/ veya test/ altında dosya üretmeli")
        if any(not name.startswith(("tests/", "test/")) for name in tests):
            raise ValueError("Test ajanı yalnızca test dosyası yazabilir")
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
                f"## Deneme {number}\nÇıkış kodu: {code}\n\n```text\n{output}\n```\n"
            )
            self._event("testing", "passed" if passed else "failed", f"attempt {number}")
            if passed:
                self.test_passed = True
                break
            if number <= self.max_repairs:
                self._event("development", "repair_started", f"from test attempt {number}")
                context = self._context()
                replacements = self._with_questions("development", lambda answers: self.engine.code(
                    self.idea, context + answers, feedback=output
                ))
                write_generated_files(self.project_dir, replacements)
                self.code_files.update(replacements)
                self._save_code_files()
                self.manifest = read_project_manifest(self.project_dir, self.decisions)
                self.setup_done = False
                self._event("development", "repair_completed", ", ".join(replacements))
        summary = "BAŞARILI" if self.test_passed else "BAŞARISIZ"
        report = f"# Test raporu\n\nSonuç: **{summary}** ({self.attempts} deneme).\n\n"
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
                if not (venv_dir / "pyvenv.cfg").is_file():
                    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], cwd=self.project_dir, capture_output=True, text=True, timeout=120, check=True)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                return f"Projeye özel Python ortamı oluşturulamadı: {exc}", -1
            self.python_executable = str(venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        commands = [(label, self.manifest[key]) for label, key in
                    (("Kurulum", "setup"), ("Birim", "unit_test"), ("API", "api_test"), ("Arayüz", "ui_test"))
                    if self.manifest.get(key) and (key != "setup" or not self.setup_done)]
        logs = []
        for label, command in commands:
            server = None
            try:
                if label in {"API", "Arayüz"} and self.manifest.get("start"):
                    port = self.manifest["port"]
                    env["BASE_URL"] = f"http://127.0.0.1:{port}"
                    server = subprocess.Popen(
                        self._command(self.manifest["start"]),
                        cwd=self.project_dir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    for _ in range(50):
                        if server.poll() is not None:
                            return "\n".join(logs + ["Uygulama sunucusu testlerden önce kapandı"]), -1
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                                break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        return "\n".join(logs + ["Uygulama sunucusu başlamadı"]), -1
                process = subprocess.run(self._command(command), cwd=self.project_dir, env=env, capture_output=True, text=True, timeout=180, check=False)
                logs.append(f"### {label} testleri (çıkış {process.returncode})\n{process.stdout}\n{process.stderr}"[-20_000:])
                if process.returncode:
                    return "\n".join(logs), process.returncode
                if label == "Kurulum":
                    self.setup_done = True
            except (subprocess.TimeoutExpired, OSError) as exc:
                return "\n".join(logs + [f"{label} komutu başarısız: {exc}"]), -1
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
            ("Mimari kararlar", "decisions.yaml"), ("Proje planı", "planning.md"),
            ("Aşama soruları ve yanıtları", "clarifications.yaml"),
            ("Gereksinimler ve izlenebilirlik", "requirements.md"), ("Mimari ve diyagramlar", "architecture.md"),
            ("Test raporu", "test_report.md"), ("Dağıtım kılavuzu", "deployment.md"),
            ("Bakım planı", "maintenance_report.md"),
        ]
        docs = "\n".join(f"- [{label}](docs/{name})" for label, name in links if (self.project_dir / "docs" / name).exists())
        content = (
            f"# Üretilen proje\n\n{self.idea}\n\n"
            f"SDLC durumu: **{'tamamlandı' if status == 'completed' else 'başarısız'}**. Test denemesi: {self.attempts}.\n\n"
            f"## Belgeler\n\n{docs}\n\n"
            "## Docker Compose ile çalıştırma\n\n"
            "`.env.example` dosyasını `.env` olarak kopyalayıp gerekli değerleri doldur, ardından `docker compose up --build` çalıştır.\n"
            "Kontroller ve geri alma için [dağıtım kılavuzuna](docs/deployment.md) bak.\n"
        )
        (self.project_dir / "README.md").write_text(content, encoding="utf-8")


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
    workspace: Path | str = Path.cwd(),
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
    workspace = Path(workspace).resolve()
    if resume_run_id:
        row = History(workspace / "runs" / "history.sqlite3").get(resume_run_id)
        if row is None or row["status"] != "paused":
            raise ValueError("Sürdürülecek duraklatılmış çalışma bulunamadı")
        idea = row["idea"]
        decisions = yaml.safe_load((Path(row["output_dir"]) / "decisions.yaml").read_text(encoding="utf-8"))
        output = Path(row["project_dir"])
        model = model or row["model"]
        run_id = resume_run_id
    if not idea.strip() or len(idea) > 1000:
        raise ValueError("Proje fikri 1–1000 karakter olmalı")
    if not 0 <= max_repairs <= 3:
        raise ValueError("max_repairs değeri 0–3 arasında olmalı")
    decisions = validate_decisions(decisions)
    engine = engine or CrewAIEngine(api_key=api_key, model=model)
    session = RunSession(idea, workspace, engine, max_repairs, decisions, Path(output) if output is not None else None, on_event, on_question, run_id, bool(resume_run_id))
    try:
        _run_crew_flow(session)
        return session.result()
    except RunPaused:
        return session.pause()
    except Exception as exc:
        session._event("workflow", "failed", str(exc))
        return session.result(error=str(exc))

