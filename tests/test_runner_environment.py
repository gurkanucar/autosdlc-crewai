from pathlib import Path

from autosdlc.runner import RunSession


class Engine:
    language = "en"


def test_sqlite_api_check_gets_isolated_database_path(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TASK_DB_PATH", raising=False)
    decisions = {
        "project_type": "backend",
        "stack": "Node.js / Express",
        "database": "sqlite",
        "cache": "none",
        "observability": "none",
        "ui": "none",
        "scale": "small",
        "language": "en",
    }
    session = RunSession(
        "Task API",
        tmp_path,
        Engine(),
        0,
        decisions,
        output=tmp_path / "projects" / "task-api",
    )

    assert session._test_env()["TASK_DB_PATH"] == str(
        session.output_dir / "tmp" / "autosdlc-test.sqlite3"
    )
