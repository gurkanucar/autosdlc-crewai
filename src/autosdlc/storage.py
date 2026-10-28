"""SQLite history for runs and stage events."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Istanbul"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def display_zone() -> tzinfo:
    """The time zone times are shown in (AUTOSDLC_TIMEZONE, an IANA name); stored times stay UTC.
    An unknown name falls back to UTC, so a typo shows obviously shifted times instead of failing."""
    try:
        return ZoneInfo(os.getenv("AUTOSDLC_TIMEZONE") or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def local_time(value: str | None = None) -> datetime:
    """A stored UTC timestamp (or now, without one) in the display time zone."""
    moment = datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(display_zone())


def clock(value: str, with_date: bool = False) -> str:
    return local_time(value).strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


TITLE_LIMIT = 120


def suggested_title(idea: str) -> str:
    """The idea cut to fit a name: at most TITLE_LIMIT characters, ending at a word boundary.

    A name field pre-filled with a longer text cannot be edited in a browser: it only allows deleting until the text
    is below the limit, never typing.
    """
    idea = " ".join(idea.split())
    if len(idea) <= TITLE_LIMIT:
        return idea
    cut = idea[:TITLE_LIMIT]
    space = cut.rfind(" ")
    return (cut[:space] if space >= TITLE_LIMIT // 2 else cut).rstrip(" ,.;:-")


def stage_models(row: dict) -> dict[str, str]:
    """The run's per-stage model choices; {} for a run that uses one model everywhere."""
    try:
        value = json.loads(row.get("stage_models") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def project_title(row: dict) -> str:
    """What the UI calls a project: its display name, or the idea when no name was set."""
    return row.get("title") or row["idea"]


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
                CREATE TABLE IF NOT EXISTS changes (
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    impact TEXT,
                    start_stage TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, id)
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
            if "title" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN title TEXT")
            # Optional model per stage as JSON ({"testing": "glm-5.3-flash"}); a stage not listed uses the run's model.
            if "stage_models" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN stage_models TEXT")
            change_columns = {row[1] for row in db.execute("PRAGMA table_info(changes)")}
            if "clarifications" not in change_columns:
                db.execute("ALTER TABLE changes ADD COLUMN clarifications TEXT")
            if "skip_stages" not in change_columns:
                db.execute("ALTER TABLE changes ADD COLUMN skip_stages TEXT")
            # Version history: every finished iteration (first generation, CR, bugfix) leaves a record and a snapshot.
            db.execute(
                """CREATE TABLE IF NOT EXISTS iterations (
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    number INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    change_id TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    first_event_id INTEGER NOT NULL,
                    last_event_id INTEGER NOT NULL,
                    snapshot_dir TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, number)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def create(self, run_id: str, idea: str, output_dir: Path, project_dir: Path, model: str | None = None,
               stage_models: dict[str, str] | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO runs (id, idea, provider, status, output_dir, project_dir, created_at, model, heartbeat_at, stage_models) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                (run_id, idea, "opencode-go", str(output_dir), str(project_dir), now(), model, now(),
                 json.dumps(stage_models) if stage_models else None),
            )

    def set_stage_models(self, run_id: str, stage_models: dict[str, str]) -> None:
        """The per-stage models a paused or finished run continues with; empty means every stage uses the run's model."""
        with self._connect() as db:
            db.execute("UPDATE runs SET stage_models=? WHERE id=? AND status != 'running'",
                       (json.dumps(stage_models) if stage_models else None, run_id))

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
        """Marks runs whose process died (UI restarted, computer shut down) as paused."""
        limit = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat(timespec="seconds")
        with self._connect() as db:
            stale = [row[0] for row in db.execute(
                "SELECT id FROM runs WHERE status='running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (limit,)
            )]
            for run_id in stale:
                db.execute("UPDATE runs SET status='paused', pause_requested=0 WHERE id=?", (run_id,))
                db.execute(
                    "INSERT INTO events (run_id, stage, status, detail, created_at) VALUES (?, 'workflow', 'interrupted', ?, ?)",
                    (run_id, "The running process ended; it can be resumed where it stopped", now()),
                )
            return stale

    def reset_from(self, run_id: str, stage: str) -> bool:
        """Pauses a finished/paused run so that the given stage and the ones after it are regenerated."""
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

    def set_model(self, run_id: str, model: str) -> None:
        """The model a paused or finished run continues with; a resume reads it from here."""
        with self._connect() as db:
            db.execute("UPDATE runs SET model=? WHERE id=? AND status != 'running'", (model, run_id))

    def update_idea(self, run_id: str, idea: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE runs SET idea=? WHERE id=? AND status != 'running'", (idea, run_id))

    def rename(self, run_id: str, title: str) -> None:
        """Sets the display name; an empty name goes back to showing the idea. Allowed while running: agents never read it."""
        title = title.strip()
        if len(title) > TITLE_LIMIT:
            raise ValueError(f"The project name must be at most {TITLE_LIMIT} characters")
        with self._connect() as db:
            db.execute("UPDATE runs SET title=? WHERE id=?", (title or None, run_id))

    def delete_record(self, run_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM questions WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM notes WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM changes WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM iterations WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
            db.execute("DELETE FROM runs WHERE id=?", (run_id,))

    # Change requests: feature (new feature), bug, change, edit (propagation of section edits).
    # Status: draft → analyzed → in_progress → done | failed; the user can set rejected.
    def create_change(self, run_id: str, kind: str, title: str, description: str, status: str = "draft") -> str:
        if kind not in {"feature", "bug", "change", "edit"}:
            raise ValueError("Unknown change type")
        if not title.strip() or len(title) > 200 or len(description) > 4000:
            raise ValueError("The title must be 1–200 and the description at most 4000 characters")
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM changes WHERE run_id=?", (run_id,)).fetchone()[0]
            change_id = f"CR-{count + 1:03d}"
            db.execute(
                "INSERT INTO changes (run_id, id, kind, title, description, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, change_id, kind, title.strip(), description.strip(), status, now(), now()),
            )
            return change_id

    def update_change(self, run_id: str, change_id: str, **fields: str | None) -> None:
        allowed = {"status", "impact", "start_stage", "description", "clarifications", "skip_stages"}
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid change field")
        assignments = ", ".join(f"{name}=?" for name in fields)
        with self._connect() as db:
            db.execute(
                f"UPDATE changes SET {assignments}, updated_at=? WHERE run_id=? AND id=?",
                (*fields.values(), now(), run_id, change_id),
            )

    def changes(self, run_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM changes WHERE run_id=? ORDER BY created_at, id", (run_id,))]

    def change(self, run_id: str, change_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM changes WHERE run_id=? AND id=?", (run_id, change_id)).fetchone()
            return dict(row) if row else None

    def reopen_failed_change(self, run_id: str) -> str | None:
        """Retrying a run whose last version was a failed change continues that change: retried as a plain
        regeneration, CR-001 passed its tests but stayed "Failed"."""
        with self._connect() as db:
            last = db.execute(
                "SELECT change_id, status FROM iterations WHERE run_id=? ORDER BY number DESC LIMIT 1", (run_id,)
            ).fetchone()
            if not last or not last["change_id"] or last["status"] != "failed":
                return None
            result = db.execute(
                "UPDATE changes SET status='in_progress', updated_at=? WHERE run_id=? AND id=? AND status='failed'",
                (now(), run_id, last["change_id"]),
            )
            return last["change_id"] if result.rowcount else None

    def active_change(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM changes WHERE run_id=? AND status='in_progress' ORDER BY updated_at DESC LIMIT 1", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def add_iteration(self, run_id: str, label: str, kind: str, change_id: str | None, status: str,
                      attempts: int, snapshot_dir: Path) -> int:
        """Records the finished iteration; the event range runs from where the previous version ended until now."""
        with self._connect() as db:
            previous = db.execute(
                "SELECT MAX(number), MAX(last_event_id) FROM iterations WHERE run_id=?", (run_id,)
            ).fetchone()
            first = db.execute(
                "SELECT COALESCE(MIN(id), 0) FROM events WHERE run_id=? AND id > ?", (run_id, previous[1] or 0)
            ).fetchone()[0]
            last = db.execute("SELECT COALESCE(MAX(id), 0) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
            number = (previous[0] or 0) + 1
            db.execute(
                "INSERT INTO iterations (run_id, number, label, kind, change_id, status, attempts, first_event_id, "
                "last_event_id, snapshot_dir, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, number, label, kind, change_id, status, attempts, first, last, str(snapshot_dir), now()),
            )
            return number

    def iterations(self, run_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM iterations WHERE run_id=? ORDER BY number", (run_id,))]

    def next_iteration_number(self, run_id: str) -> int:
        with self._connect() as db:
            return (db.execute("SELECT MAX(number) FROM iterations WHERE run_id=?", (run_id,)).fetchone()[0] or 0) + 1

    def add_note(self, run_id: str, content: str) -> None:
        """Guidance the user gives the agents through the chat; it enters the context of later stages."""
        text = content.strip()
        if not text or len(text) > 2000:
            raise ValueError("The note must be 1–2000 characters")
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
                "UPDATE questions SET answer='Previous question was superseded', answered_at=? WHERE run_id=? AND answer IS NULL",
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
            raise ValueError("The answer must be 1–2000 characters")
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
                    "SELECT id, stage, status, detail, created_at FROM events "
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

