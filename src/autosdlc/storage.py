"""SQLite history for runs and stage events."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
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
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            if "project_dir" not in {row[1] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN project_dir TEXT")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def create(self, run_id: str, idea: str, output_dir: Path, project_dir: Path) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO runs (id, idea, provider, status, output_dir, project_dir, created_at) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (run_id, idea, "opencode-go", str(output_dir), str(project_dir), now()),
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
                "UPDATE runs SET status=?, attempts=?, completed_at=?, error=? WHERE id=?",
                (status, attempts, now(), error, run_id),
            )

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
            return result

    def list(self, limit: int = 30) -> list[dict]:
        with self._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            ]

