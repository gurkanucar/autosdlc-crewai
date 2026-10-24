"""Project display name: optional, independent of the idea, never touches the stages."""

import pytest

from autosdlc.storage import TITLE_LIMIT, History, project_title


@pytest.fixture
def db(tmp_path):
    history = History(tmp_path / "runs" / "history.sqlite3")
    history.create("run1", "A calculator with a typo in the idea", tmp_path / "runs" / "run1", tmp_path / "projects" / "p1")
    return history


def test_title_defaults_to_the_idea(db):
    assert project_title(db.get("run1")) == "A calculator with a typo in the idea"


def test_rename_changes_only_the_title(db):
    db.rename("run1", "  Calculator  ")
    row = db.get("run1")
    assert project_title(row) == "Calculator"
    assert row["idea"] == "A calculator with a typo in the idea"
    assert db.list()[0]["title"] == "Calculator"


def test_empty_name_goes_back_to_the_idea(db):
    db.rename("run1", "Calculator")
    db.rename("run1", "   ")
    assert project_title(db.get("run1")) == "A calculator with a typo in the idea"


def test_rename_is_allowed_while_running(db):
    db.rename("run1", "Renamed during a run")
    assert project_title(db.get("run1")) == "Renamed during a run"


def test_too_long_name_is_rejected(db):
    with pytest.raises(ValueError):
        db.rename("run1", "x" * (TITLE_LIMIT + 1))


def test_old_database_gets_the_title_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, idea TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL, "
                    "output_dir TEXT NOT NULL, project_dir TEXT, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
                    "completed_at TEXT, error TEXT)")
        raw.execute("INSERT INTO runs (id, idea, provider, status, output_dir, created_at) VALUES ('old','An old idea','p','completed','o','t')")
    history = History(path)
    assert project_title(history.get("old")) == "An old idea"
    history.rename("old", "Named")
    assert project_title(history.get("old")) == "Named"
