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
from .traceability import build_matrix


SDLC_STAGES = ("planning", "analysis", "design", "review", "development", "testing", "deployment", "maintenance")
STAGE_FILES = {
    "planning": "planning.md", "analysis": "requirements.md", "design": "architecture.md",
    "review": "design_review.md", "deployment": "deployment.md", "maintenance": "maintenance_report.md",
}
MAX_REVIEW_ROUNDS = 2
VERDICT = re.compile(r"KARAR:\s*\**\s*(ONAY|REV[İI]ZYON)", re.IGNORECASE)
TEST_DIRS = ("tests/", "test/")
PYTHON_TOOLS = {"python", "python3", "pip", "pip3"}
DOC_BUDGET = 9000
# Her aşamanın okuduğu önceki belgeler, önem sırasına göre. Motor bağlamı baştan
# kırptığı için en önemli belge önce gelir.
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


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _stop_process(process: subprocess.Popen) -> None:
    """Sunucuyu alt süreçleriyle birlikte kapatır; aksi hâlde port açık kalabilir."""
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


PATH_RUNNERS = {"pytest", "node", "jest", "mocha", "vitest"}


def uncovered_tests(manifest: dict, test_names: list[str]) -> list[str]:
    """project.yaml'daki test komutlarının çalıştırmadığı test dosyaları.

    Komutlar bir test dosyası/klasörü adlandırmıyorsa (ör. "npm test") kapsam bilinemez ve boş liste döner.
    """
    # Yalnızca dosya yolu alan çalıştırıcılar incelenir; "npm test", "go test" gibi komutlardaki "test" bir alt komuttur.
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
    """Belgenin mevcut hâlini versions/ altına zaman damgasıyla kopyalar."""
    source = output_dir / filename
    if source.is_file():
        versions = output_dir / "versions"
        versions.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
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
    return "ONAY" if match.group(1).upper() == "ONAY" else "REVİZYON"


def completed_from_events(events: list[dict]) -> set[str]:
    """Olayları sırayla oynatır; 'reset' olayı o aşama ve sonrasını yeniden üretilecek yapar."""
    completed: set[str] = set()
    for event in events:
        if event["stage"] == "workflow" and event["status"] == "reset" and event["detail"] in SDLC_STAGES:
            completed -= set(SDLC_STAGES[SDLC_STAGES.index(event["detail"]):])
        elif event["status"] == "completed" or (event["stage"] == "testing" and event["status"] == "passed"):
            completed.add(event["stage"])
    return completed


def default_project_dir(workspace: Path, idea: str, run_id: str) -> Path:
    normalized = unicodedata.normalize("NFKD", idea.lower().replace("ı", "i"))
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:48].rstrip("-") or "proje"
    return workspace / "projects" / f"{slug}-{run_id}"


CHANGE_KINDS = {"feature": "Yeni özellik", "bug": "Hata", "change": "Değişiklik", "edit": "Belge düzenlemesi"}


class StageEngine(Protocol):
    def document(self, stage: str, idea: str, context: str) -> str: ...
    # change verilirse (değişiklik talebi) motor mevcut kodu/testleri koruyup yalnızca değişenleri döndürür.
    def code(self, idea: str, docs: str, feedback: str | None = None, change: str | None = None) -> dict[str, str]: ...
    def tests(self, idea: str, docs: str, files: dict[str, str], change: str | None = None) -> dict[str, str]: ...


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
            self.project_dir = Path(output).expanduser().resolve() if output is not None else default_project_dir(self.workspace, self.idea, self.run_id)
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
        self.test_files: dict[str, str] = {}
        self.traceability = ""
        self.manifest: dict = {}
        # Değişiklik talebi uygulanırken: önceki belgeler temel alınır ve yalnızca etkilenen kısımlar güncellenir.
        self.change: dict | None = None
        self.previous_docs: dict[str, str] = {}
        self.setup_done = False
        self.python_executable = sys.executable
        self.attempts = 0
        self.test_passed = False
        self.last_test_output = ""
        self.completed_stages: set[str] = set()
        self._live_written = 0.0
        if hasattr(engine, "output_listener"):
            engine.output_listener = self._live_output
        if resume:
            self._load_checkpoint(existing)
            if not self.history.resume(self.run_id):
                raise ValueError("Çalışma zaten sürdürülüyor")

    def _load_checkpoint(self, row: dict) -> None:
        self.completed_stages = completed_from_events(row["events"])
        for stage, name in STAGE_FILES.items():
            path = self.output_dir / name
            if path.is_file():
                self.previous_docs[stage] = path.read_text(encoding="utf-8")
                # Yeniden üretilecek aşamaların eski belgeleri bağlama karışmasın (delta modunda ayrıca verilir).
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
        """Ajanın akan çıktısını arayüzün okuyacağı dosyaya yazar (en fazla saniyede ~3 kez)."""
        now = time.monotonic()
        if now - self._live_written < 0.3:
            return
        self._live_written = now
        try:
            live_dir = self.output_dir / "live"
            live_dir.mkdir(exist_ok=True)
            (live_dir / f"{stage}.md").write_text(text, encoding="utf-8")
        except OSError:
            pass  # Canlı gösterim yalnızca kolaylık; arayüz dosyayı okurken yazma çakışabilir.

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

    def _generate(self, stage: str, instruction: str = "") -> str:
        """Belgeyi üretir; kalite kapısından geçemezse gerekçesiyle bir kez daha yazdırır."""
        gate_feedback = ""
        for _ in range(2):
            content = self._with_questions(
                stage, lambda: self.engine.document(
                    stage, self.idea, self._change_instruction(stage) + instruction + gate_feedback + self._context(stage)
                )
            )
            problem = self._quality_gate(stage, content)
            if problem is None:
                return content
            self._event(stage, "gate_failed", problem)
            gate_feedback = (
                f"ÖNCEKİ TASLAK KALİTE KAPISINDAN GEÇEMEDİ: {problem}\n"
                "Eksikleri gidererek belgenin tamamını yeniden yaz.\n\n"
            )
        raise ValueError(problem)

    def _document(self, stage: str, filename: str) -> str:
        self._event(stage, "started")
        content = self._generate(stage)
        if stage in {"deployment", "maintenance"}:
            content = (
                f"> Doğrulanan test sonucu: **{'BAŞARILI' if self.test_passed else 'BAŞARISIZ'}** "
                f"({self.attempts} deneme).\n\n{content}"
            )
        self.docs[stage] = content
        self._write_doc(filename, content)
        self._event(stage, "completed", filename)
        return content

    def _quality_gate(self, stage: str, content: str) -> str | None:
        """Belgenin sonraki aşamaya geçmesi için gereken asgari koşullar; geçemezse ajan bir kez daha yazar."""
        if not content.strip():
            return f"{stage} ajanı boş belge döndürdü"
        if stage == "analysis" and "FR-" not in content:
            return "Analiz belgesinde FR-01 gibi numaralı işlevsel gereksinimler bulunmalı"
        if stage == "analysis" and "NFR-" not in content:
            return "Analiz belgesinde NFR-01 gibi numaralı işlevsel olmayan gereksinimler bulunmalı"
        if stage == "design" and content.count("```mermaid") < 2:
            return "Mimari belgede Mermaid bileşen ve sıralama diyagramları bulunmalı"
        if stage == "design" and self.decisions["database"] != "none" and "erDiagram" not in content:
            return "Seçilen veritabanı için mimari belgede ER diyagramı bulunmalı"
        if stage == "review" and review_verdict(content) is None:
            return "İnceleme raporu 'KARAR: ONAY' veya 'KARAR: REVİZYON' satırını içermeli"
        return None

    def _change_text(self) -> str:
        change = self.change or {}
        text = (
            f"{change.get('id')} · {CHANGE_KINDS.get(change.get('kind'), change.get('kind'))}: {change.get('title')}\n"
            f"{change.get('description', '')}"
        )
        if change.get("impact"):
            text += f"\n\nEtki analizi:\n{change['impact'][:4000]}"
        return text

    def _change_instruction(self, stage: str) -> str:
        """Delta modu: ajan mevcut belgeyi temel alır, yalnızca değişiklikten etkilenen yerleri günceller."""
        if not self.change:
            return ""
        if stage == "review":
            return (
                f"BU İNCELEME BİR DEĞİŞİKLİK İTERASYONU İÇİNDİR ({self.change['id']}). Özellikle değişikliğin "
                f"tasarıma doğru ve eksiksiz yansıyıp yansımadığını denetle.\n{self._change_text()}\n\n"
            )
        previous = self.previous_docs.get(stage)
        if not previous:
            return f"UYGULANAN DEĞİŞİKLİK TALEBİ:\n{self._change_text()}\n\n"
        return (
            f"DEĞİŞİKLİK MODU — {self._change_text()}\n\n"
            "Aşağıdaki MEVCUT BELGEYİ temel al. Yalnızca bu değişiklikten etkilenen bölümleri güncelle ya da "
            "yeni bölüm/gereksinim ekle; diğer bölümleri AYNEN koru. Belgenin tamamını döndür. "
            f"Değişen veya eklenen her yerin sonuna [{self.change['id']}] etiketi koy.\n"
            f"MEVCUT BELGE:\n{previous[:14000]}\n\n"
        )

    def _write_doc(self, filename: str, content: str) -> None:
        # Üzerine yazmadan önce eski hâli sakla (delta güncellemeleri ve bölüm düzenlemeleri geri alınabilsin).
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
                    raise ValueError(f"{stage} ajanı şu kararı istedi: {exc.question}") from exc
                self._event(stage, "question", exc.question)
                answer = self.on_question(self.run_id, stage, exc.question).strip()
                if not answer:
                    raise ValueError("Boş kullanıcı yanıtıyla devam edilemez")
                self.clarifications.append({"stage": stage, "question": exc.question, "answer": answer})
                self._write_doc("clarifications.yaml", yaml.safe_dump(self.clarifications, allow_unicode=True, sort_keys=False))
                self._event(stage, "answered", "Kullanıcı yanıtladı")
        raise ValueError(f"{stage} ajanı açıklama sonrasında da ilerleyemedi")

    def _context(self, stage: str) -> str:
        """Kısa ve kritik bilgiler önce, uzun belgeler sonra; her belge bütçeyle kırpılır."""
        parts = ["Mimari kararlar ve kullanıcı kısıtları:\n" + yaml.safe_dump(self.decisions, allow_unicode=True, sort_keys=False)]
        if self.change:
            parts.append("UYGULANAN DEĞİŞİKLİK TALEBİ (bu iterasyonun amacı):\n" + self._change_text())
        if self.clarifications:
            parts.append("Kullanıcının aşama sorularına verdiği yanıtlar (kesin kararlardır):\n" + "\n".join(
                f"- [{item['stage']}] Soru: {item['question']}\n  Yanıt: {item['answer']}" for item in self.clarifications
            ))
        notes = self.history.notes(self.run_id)
        if notes:
            parts.append("Kullanıcının chat üzerinden verdiği yönlendirmeler (dikkate al):\n" + "\n".join(
                f"- {note['content']}" for note in notes
            ))
        if stage in {"deployment", "maintenance"}:
            parts.append(
                f"Gerçek test sonucu: {'BAŞARILI' if self.test_passed else 'BAŞARISIZ'} ({self.attempts} deneme).\n"
                f"Son test çıktısının sonu:\n{self.last_test_output[-3000:]}"
            )
            if self.traceability:
                parts.append(self.traceability[:3000])
        for name in STAGE_INPUTS[stage]:
            if name in self.docs:
                doc = self.docs[name]
                if len(doc) > DOC_BUDGET:
                    doc = doc[:DOC_BUDGET] + "\n\n…(belge kısaltıldı)"
                parts.append(f"--- {name} belgesi ---\n{doc}")
        return "\n\n".join(parts)

    def planning(self) -> str:
        return self._document("planning", "planning.md")

    def analysis(self) -> str:
        return self._document("analysis", "requirements.md")

    def design(self) -> str:
        return self._document("design", "architecture.md")

    def review(self) -> str:
        """Mimari inceleme: REVİZYON kararında Tasarım Ajanı bulgulara göre düzeltir, inceleme tekrarlanır."""
        self._event("review", "started")
        rounds = []
        for number in range(1, MAX_REVIEW_ROUNDS + 1):
            report = self._generate("review")
            verdict = review_verdict(report)
            rounds.append(f"## İnceleme turu {number} — {verdict}\n\n{report}")
            self._event("review", "approved" if verdict == "ONAY" else "changes_requested", f"tur {number}")
            if verdict == "ONAY" or number == MAX_REVIEW_ROUNDS:
                break
            self._event("design", "revision_started", f"inceleme turu {number}")
            self.docs["design"] = self._generate("design", (
                "MİMARİ İNCELEME BULGULARI — mevcut tasarımı bu bulgulara göre revize et ve "
                f"belgenin tamamını yeniden yaz:\n{report[:6000]}\n\n"
                f"Mevcut tasarım belgesi:\n{self.docs['design'][:DOC_BUDGET]}\n\n"
            ))
            self._write_doc("architecture.md", self.docs["design"])
            self._event("design", "revision_completed", "architecture.md")
        summary = "ONAYLANDI" if verdict == "ONAY" else "AÇIK BULGULARLA DEVAM EDİLDİ"
        content = (
            f"# Mimari inceleme raporu\n\nSonuç: **{summary}** ({len(rounds)} tur).\n\n"
            + "\n\n".join(rounds)
        )
        self.docs["review"] = content
        self._write_doc("design_review.md", content)
        self._event("review", "completed", "design_review.md")
        return summary

    def development(self) -> str:
        self._event("development", "started")
        if self.change and self.code_files:
            return self._develop_change()
        self.code_files = self._with_questions("development", lambda: self.engine.code(self.idea, self._context("development")))
        write_generated_files(self.project_dir, self.code_files)
        self._save_code_files()
        self.manifest = read_project_manifest(self.project_dir, self.decisions)
        write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
        self._event("development", "completed", ", ".join(self.code_files))
        return "uygulama hazır"

    def _develop_change(self) -> str:
        """Değişiklik talebi: mevcut kod korunur; hata ise önce hatayı yeniden üreten test yazılır (TDD)."""
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
                    f"\nUYARI: Yeni test dosyaları project.yaml test komutlarınca çalıştırılmıyor: {', '.join(missing)}. "
                    "project.yaml'daki test komutlarını tek dosya yerine test klasörünü çalıştıracak şekilde güncelle."
                )
            self._event("testing", "bug_reproduced" if code else "bug_not_reproduced", ", ".join(tests) or "test yok")
            feedback = output
        replacements = self._with_questions("development", lambda: self.engine.code(
            self.idea, self._context("development"), feedback=feedback, change=description
        ))
        rejected = sorted(name for name in replacements if name.startswith(TEST_DIRS))
        replacements = {name: content for name, content in replacements.items() if name not in rejected}
        if rejected:
            self._event("development", "tests_protected", ", ".join(rejected))
        if replacements:
            write_generated_files(self.project_dir, replacements)
            self.code_files.update(replacements)
            self._save_code_files()
            self.manifest = read_project_manifest(self.project_dir, self.decisions)
            write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
        self._event("development", "completed", ", ".join(replacements) or "değişen dosya yok")
        return "değişiklik uygulandı"

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
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _save_code_files(self) -> None:
        (self.output_dir / "code_files.json").write_text(
            json.dumps(self.code_files, ensure_ascii=False), encoding="utf-8"
        )

    def testing(self) -> str:
        self._event("testing", "started")
        existing = self._test_files_on_disk() if self.change else {}
        if existing:
            # Değişiklik talebi: mevcut testler korunur, yalnızca yeni/güncellenen testler yazılır; sonra hepsi koşar (regresyon).
            new_tests = self._with_questions("testing", lambda: self.engine.tests(
                self.idea, self._context("testing"), {**self.code_files, **existing}, change=self._change_text()
            ))
            if any(not name.startswith(TEST_DIRS) for name in new_tests):
                raise ValueError("Test ajanı yalnızca test dosyası yazabilir")
            if new_tests:
                write_generated_files(self.project_dir, new_tests)
            tests = {**existing, **new_tests}
            self._event("testing", "regression", f"{len(new_tests)} yeni/güncellenen, toplam {len(tests)} test dosyası")
        else:
            tests = self._with_questions(
                "testing", lambda: self.engine.tests(self.idea, self._context("testing"), self.code_files)
            )
            if not any(name.startswith(TEST_DIRS) for name in tests):
                raise ValueError("Test ajanı tests/ veya test/ altında dosya üretmeli")
            if any(not name.startswith(TEST_DIRS) for name in tests):
                raise ValueError("Test ajanı yalnızca test dosyası yazabilir")
            write_generated_files(self.project_dir, tests)
        self.test_files = tests
        if hasattr(self.engine, "current_files"):
            self.engine.current_files.update(tests)
        missing = uncovered_tests(self.manifest, list(tests))
        if missing:
            # Çalışmayan test, geçmiş gibi görünmesin: raporda ve olaylarda açıkça belirtilir.
            self._event("testing", "tests_not_covered", ", ".join(missing))
        self.traceability, coverage = build_matrix(self.docs.get("analysis", ""), tests)
        self._write_doc("traceability.md", self.traceability)
        self._event("testing", "traceability", f"FR kapsamı %{coverage['FR'] * 100:.0f}")
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
                f"## Deneme {number}\nÇıkış kodu: {code}\n\n```text\n{output}\n```\n"
            )
            self._event("testing", "passed" if passed else "failed", f"attempt {number}")
            if passed:
                self.test_passed = True
                break
            if number <= self.max_repairs:
                self._event("development", "repair_started", f"from test attempt {number}")
                replacements = self._with_questions("development", lambda: self.engine.code(
                    self.idea, self._context("development"), feedback=output
                ))
                # Testler Test Ajanı'na aittir; geliştirici testi değiştirerek geçiremez.
                rejected = sorted(name for name in replacements if name.startswith(TEST_DIRS))
                replacements = {name: content for name, content in replacements.items() if name not in rejected}
                if hasattr(self.engine, "current_files"):
                    self.engine.current_files.update(tests)
                if rejected:
                    self._event("development", "tests_protected", ", ".join(rejected))
                if not replacements:
                    continue
                write_generated_files(self.project_dir, replacements)
                self.code_files.update(replacements)
                self._save_code_files()
                self.manifest = read_project_manifest(self.project_dir, self.decisions)
                self.setup_done = False
                self._event("development", "repair_completed", ", ".join(replacements))
        summary = "BAŞARILI" if self.test_passed else "BAŞARISIZ"
        report = f"# Test raporu\n\nSonuç: **{summary}** ({self.attempts} deneme).\n\n"
        if missing:
            report += f"> ⚠️ Şu test dosyaları project.yaml komutlarınca çalıştırılmadı: {', '.join(missing)}\n\n"
        report += (
            f"İzlenebilirlik: FR kapsamı %{coverage['FR'] * 100:.0f}, "
            f"NFR kapsamı %{coverage['NFR'] * 100:.0f} ([matris](traceability.md)).\n\n"
        )
        report += "\n".join(reports)
        self._write_doc("test_report.md", report)
        return summary

    def _run_checks(self, env: dict[str, str]) -> tuple[str, int]:
        """Execute stack-specific commands from the validated project manifest."""
        env["PYTHONPATH"] = str(self.project_dir)
        temp_dir = self.output_dir / "tmp"
        temp_dir.mkdir(exist_ok=True)
        env.update({"TEMP": str(temp_dir), "TMP": str(temp_dir), "TMPDIR": str(temp_dir)})
        if self.manifest.get("setup") and self.manifest["setup"][0] in PYTHON_TOOLS and self.python_executable == sys.executable:
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
        server_log = temp_dir / "server.log"
        for label, command in commands:
            server = None
            try:
                if label in {"API", "Arayüz"} and self.manifest.get("start"):
                    port = self.manifest["port"]
                    if _port_open(port):
                        return "\n".join(logs + [f"Port {port} başka bir süreç tarafından kullanılıyor; eski sunucuya karşı test yapılmadı"]), -1
                    env["BASE_URL"] = f"http://127.0.0.1:{port}"
                    with server_log.open("w", encoding="utf-8") as log_file:
                        server = subprocess.Popen(
                            self._command(self.manifest["start"], env), cwd=self.project_dir, env=env,
                            stdout=log_file, stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                        )
                    deadline = time.monotonic() + 60
                    while not _port_open(port):
                        if server.poll() is not None:
                            return "\n".join(logs + ["Uygulama sunucusu testlerden önce kapandı", _tail(server_log)]), -1
                        if time.monotonic() > deadline:
                            return "\n".join(logs + ["Uygulama sunucusu 60 saniyede başlamadı", _tail(server_log)]), -1
                        time.sleep(0.2)
                process = subprocess.run(self._command(command, env), cwd=self.project_dir, env=env, capture_output=True, text=True, timeout=300, check=False)
                logs.append(f"### {label} testleri (çıkış {process.returncode})\n{process.stdout}\n{process.stderr}"[-20_000:])
                if process.returncode:
                    if server is not None:
                        logs.append("### Uygulama sunucusu günlüğü\n" + _tail(server_log))
                    return "\n".join(logs), process.returncode
                if label == "Kurulum":
                    self.setup_done = True
            except (subprocess.TimeoutExpired, OSError) as exc:
                return "\n".join(logs + [f"{label} komutu başarısız: {exc}"]), -1
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
        # Windows'ta npm/pnpm .cmd uzantılıdır; kabuksuz çalıştırmak için tam yol çözülür (macOS/Linux'ta da zararsız).
        return [shutil.which(executable, path=env.get("PATH")) or executable, *args]

    def deployment(self) -> str:
        write_deployment_files(self.project_dir, self.decisions, self.manifest["port"])
        report = self._document("deployment", "deployment.md")
        return report

    def maintenance(self) -> str:
        return self._document("maintenance", "maintenance_report.md")

    def result(self, error: str | None = None) -> RunResult:
        status = "failed" if error or not self.test_passed else "completed"
        if self.change:
            self._close_change(status, error)
        self._write_project_readme(status)
        self.history.finish(self.run_id, status, self.attempts, error)
        return RunResult(self.run_id, status, self.output_dir, self.project_dir, self.attempts, error)

    def _close_change(self, status: str, error: str | None) -> None:
        change = self.change
        done = status == "completed"
        self.history.update_change(self.run_id, change["id"], status="done" if done else "failed")
        entry = (
            f"## {change['id']} · {CHANGE_KINDS.get(change['kind'], change['kind'])} · {change['title']}\n\n"
            f"- Tarih: {time.strftime('%Y-%m-%d %H:%M')}\n"
            f"- Sonuç: {'Tamamlandı, testler geçti' if done else 'Başarısız' + (f' ({error[:200]})' if error else ' (testler geçmedi)')}\n"
            f"- Başlangıç aşaması: {change.get('start_stage') or '—'}\n"
            f"- Test denemesi: {self.attempts}\n\n{change['description']}\n"
        )
        path = self.output_dir / "CHANGELOG.md"
        previous = path.read_text(encoding="utf-8") if path.is_file() else "# Değişiklik günlüğü\n"
        self._write_doc("CHANGELOG.md", previous.rstrip() + "\n\n" + entry)
        self._event("workflow", "change_completed" if done else "change_failed", f"{change['id']} · {change['title']}")

    def _write_project_readme(self, status: str) -> None:
        links = [
            ("Mimari kararlar", "decisions.yaml"), ("Proje planı", "planning.md"),
            ("Aşama soruları ve yanıtları", "clarifications.yaml"),
            ("Gereksinimler ve izlenebilirlik", "requirements.md"), ("Mimari ve diyagramlar", "architecture.md"),
            ("Mimari inceleme raporu", "design_review.md"), ("Değişiklik günlüğü", "CHANGELOG.md"),
            ("İzlenebilirlik matrisi", "traceability.md"),
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
    # Kalp atışı: süreç ölürse arayüz çalışmayı "kesintiye uğradı" olarak duraklatabilir.
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
        session._event("workflow", "failed", str(exc))
        return session.result(error=str(exc))
    finally:
        stop.set()

