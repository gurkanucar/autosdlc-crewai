"""SQLite history for runs and stage events."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class History:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    idea TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    project_dir TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    model TEXT,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    heartbeat_at TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    stage TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    answer TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT
                );
                """
            )
            if "project_dir" not in {row[1] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN project_dir TEXT")
            columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
            if "model" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN model TEXT")
            if "pause_requested" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0")
            if "heartbeat_at" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN heartbeat_at TEXT")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def create(self, run_id: str, idea: str, output_dir: Path, project_dir: Path, model: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO runs (id, idea, provider, status, output_dir, project_dir, created_at, model, heartbeat_at) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                (run_id, idea, "opencode-go", str(output_dir), str(project_dir), now(), model, now()),
            )

    def event(self, run_id: str, stage: str, status: str, detail: str = "") -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO events (run_id, stage, status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, stage, status, detail[:4000], now()),
            )

    def finish(self, run_id: str, status: str, attempts: int, error: str | None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE runs SET status=?, attempts=?, completed_at=?, error=?, pause_requested=0 WHERE id=?",
                (status, attempts, now(), error, run_id),
            )

    def request_pause(self, run_id: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                "UPDATE runs SET pause_requested=1 WHERE id=? AND status='running'",
                (run_id,),
            )
            return result.rowcount == 1

    def pause_requested(self, run_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT pause_requested FROM runs WHERE id=?", (run_id,)).fetchone()
            return bool(row and row[0])

    def pause(self, run_id: str, attempts: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE runs SET status='paused', attempts=?, pause_requested=0 WHERE id=? AND status='running'",
                (attempts, run_id),
            )

    def resume(self, run_id: str) -> bool:
        with self._connect() as db:
            result = db.execute(
                "UPDATE runs SET status='running', pause_requested=0, error=NULL, heartbeat_at=? WHERE id=? AND status='paused'",
                (now(), run_id),
            )
            return result.rowcount == 1

    def heartbeat(self, run_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE runs SET heartbeat_at=? WHERE id=? AND status='running'", (now(), run_id))

    def recover_stale(self, max_age_seconds: int = 45) -> list[str]:
        """Süreci ölmüş (arayüz yeniden başlatıldı, bilgisayar kapandı) çalışmaları duraklatılmış yapar."""
        limit = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat(timespec="seconds")
        with self._connect() as db:
            stale = [row[0] for row in db.execute(
                "SELECT id FROM runs WHERE status='running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (limit,)
            )]
            for run_id in stale:
                db.execute("UPDATE runs SET status='paused', pause_requested=0 WHERE id=?", (run_id,))
                db.execute(
                    "INSERT INTO events (run_id, stage, status, detail, created_at) VALUES (?, 'workflow', 'interrupted', ?, ?)",
                    (run_id, "Çalışan süreç sona erdi; kaldığı yerden devam ettirilebilir", now()),
                )
            return stale

    def reset_from(self, run_id: str, stage: str) -> bool:
        """Bitmiş/duraklatılmış çalışmayı, verilen aşama ve sonrası yeniden üretilecek biçimde duraklatır."""
        with self._connect() as db:
            result = db.execute(
                "UPDATE runs SET status='paused', pause_requested=0, error=NULL, completed_at=NULL "
                "WHERE id=? AND status IN ('completed', 'failed', 'paused')",
                (run_id,),
            )
            if result.rowcount != 1:
                return False
            db.execute(
                "INSERT INTO events (run_id, stage, status, detail, created_at) VALUES (?, 'workflow', 'reset', ?, ?)",
                (run_id, stage, now()),
            )
            return True

    def update_idea(self, run_id: str, idea: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE runs SET idea=? WHERE id=? AND status != 'running'", (idea, run_id))

    def delete_record(self, run_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM questions WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM notes WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))

    def add_note(self, run_id: str, content: str) -> None:
        """Kullanıcının chat üzerinden ajanlara verdiği yönlendirme; sonraki aşamaların bağlamına girer."""
        text = content.strip()
        if not text or len(text) > 2000:
            raise ValueError("Not 1–2000 karakter olmalı")
        with self._connect() as db:
            db.execute("INSERT INTO notes (run_id, content, created_at) VALUES (?, ?, ?)", (run_id, text, now()))

    def notes(self, run_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT content, created_at FROM notes WHERE run_id=? ORDER BY id", (run_id,)
            )]

    def ask(self, run_id: str, stage: str, prompt: str) -> int:
        with self._connect() as db:
            previous = db.execute(
                "SELECT id FROM questions WHERE run_id=? AND stage=? AND prompt=? AND answer IS NULL ORDER BY id DESC LIMIT 1",
                (run_id, stage, prompt[:2000]),
            ).fetchone()
            if previous:
                return previous[0]
            db.execute(
                "UPDATE questions SET answer='Önceki soru yenilendi', answered_at=? WHERE run_id=? AND answer IS NULL",
                (now(), run_id),
            )
            result = db.execute(
                "INSERT INTO questions (run_id, stage, prompt, created_at) VALUES (?, ?, ?, ?)",
                (run_id, stage, prompt[:2000], now()),
            )
            return result.lastrowid

    def answer(self, run_id: str, question_id: int, answer: str) -> bool:
        response = answer.strip()
        if not response or len(response) > 2000:
            raise ValueError("Yanıt 1–2000 karakter olmalı")
        with self._connect() as db:
            result = db.execute(
                "UPDATE questions SET answer=?, answered_at=? "
                "WHERE id=? AND run_id=? AND answer IS NULL",
                (response, now(), question_id, run_id),
            )
            return result.rowcount == 1

    def question(self, question_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
            return dict(row) if row else None

    def pending_question(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM questions WHERE run_id=? AND answer IS NULL ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return dict(row) if row else None

    def get(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["events"] = [
                dict(event)
                for event in db.execute(
                    "SELECT stage, status, detail, created_at FROM events "
                    "WHERE run_id=? ORDER BY id", (run_id,)
                )
            ]
            result["notes"] = [dict(note) for note in db.execute(
                "SELECT content, created_at FROM notes WHERE run_id=? ORDER BY id", (run_id,)
            )]
            result["questions"] = [
                dict(question) for question in db.execute(
                    "SELECT * FROM questions WHERE run_id=? ORDER BY id", (run_id,)
                )
            ]
            return result

    def list(self, limit: int = 30) -> list[dict]:
        with self._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            ]

