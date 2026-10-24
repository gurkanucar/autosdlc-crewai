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


def test_uploaded_files_go_to_an_isolated_folder_during_the_tests(tmp_path: Path):
    decisions = {"project_type": "fullstack", "stack": "Node.js / Express", "database": "none", "cache": "none",
                 "observability": "none", "ui": "web", "scale": "small", "language": "en"}
    session = RunSession("Events", tmp_path, Engine(), 0, decisions, output=tmp_path / "projects" / "events")
    assert session._test_env()["UPLOADS_DIR"] == str(session.output_dir / "tmp" / "uploads")
    cli = {**decisions, "project_type": "cli", "ui": "none"}
    other = RunSession("Calc", tmp_path, Engine(), 0, cli, output=tmp_path / "projects" / "calc")
    assert "UPLOADS_DIR" not in other._test_env() or other._test_env()["UPLOADS_DIR"] != str(other.output_dir / "tmp" / "uploads")
