import io
import zipfile

from autosdlc.exports import export_zip


def test_packages_include_all_sources_and_latest_stage_documents(tmp_path):
    project, output = tmp_path / "project", tmp_path / "run"
    (project / "docs").mkdir(parents=True)
    output.mkdir()
    (project / "README.md").write_text("# Proje", encoding="utf-8")
    (project / "docs" / "planning.md").write_text("old", encoding="utf-8")
    (output / "planning.md").write_text("latest", encoding="utf-8")
    (output / "test_report.md").write_text("passed", encoding="utf-8")
    for index in range(305):
        (project / f"source{index}.py").write_text("pass", encoding="utf-8")
    for name in (".env", ".env.local", ".env.production"):
        (project / name).write_text("SECRET=private", encoding="utf-8")
    (project / ".env.example").write_text("SECRET=", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "dependency.js").write_text("ignored", encoding="utf-8")
    with zipfile.ZipFile(io.BytesIO(export_zip(project, output))) as archive:
        names = archive.namelist()
        assert "project/source304.py" in names
        assert "sdlc/test_report.md" in names
        assert "project/.env.example" in names
        assert not any("node_modules" in name or name.endswith(("/.env", ".env.local", ".env.production")) for name in names)
    with zipfile.ZipFile(io.BytesIO(export_zip(project, output, "documents"))) as archive:
        assert archive.read("documents/planning.md") == b"latest"
        assert "README.md" in archive.namelist()
        assert not any(name.endswith(".py") for name in archive.namelist())
    with zipfile.ZipFile(io.BytesIO(export_zip(project, output, "code"))) as archive:
        assert len([name for name in archive.namelist() if name.endswith(".py")]) == 305
        assert not any("/docs/" in name for name in archive.namelist())


def test_failure_report_shows_the_failing_model_call_and_traceback(tmp_path, monkeypatch):
    import json

    from autosdlc.cli import main as cli_main
    from autosdlc.crewai_engine import CrewAIEngine
    from autosdlc.exports import diagnostics_zip
    from autosdlc.runner import run_project
    from autosdlc.storage import History

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    class Stream:
        # A reasoning model that spends its whole output budget on reasoning: no content, finish_reason "length".
        status_code, headers = 200, {"content-type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "thinking " * 50}}]})
            yield "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]})
            yield "data: [DONE]"

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.stream", lambda *args, **kwargs: Stream())
    monkeypatch.setattr("autosdlc.crewai_engine.time.sleep", lambda seconds: None)
    engine = CrewAIEngine(api_key="sk-secret-key-123456")
    decisions = {"project_type": "backend", "stack": "Python / FastAPI", "database": "sqlite", "cache": "none",
                 "observability": "none", "ui": "none"}
    result = run_project("Book tracker", workspace=tmp_path, engine=engine, decisions=decisions)
    assert result.status == "failed" and "empty response" in result.error
    (result.output_dir / "tmp").mkdir(exist_ok=True)
    (result.output_dir / "tmp" / "compile.log").write_text("Authorization: Bearer abcdefghijkl\nerror: boom", encoding="utf-8")
    db = History(tmp_path / "runs" / "history.sqlite3")
    with zipfile.ZipFile(io.BytesIO(diagnostics_zip(db.get(result.id)))) as archive:
        names = archive.namelist()
        summary = archive.read("summary.md").decode()
        assert "Stopped at stage: **planning**" in summary and "empty response" in summary
        assert "Traceback" in archive.read("failure.txt").decode()
        call = json.loads(archive.read("llm_calls.jsonl").decode().splitlines()[-1])
        assert call["stage"] == "planning" and call["outcome"] == "empty"
        assert call["finish_reason"] == "length" and call["reasoning_chars"] > 0 and call["prompt_tail"]
        log = archive.read("tmp/compile.log").decode()
        assert "abcdefghijkl" not in log and "error: boom" in log
        assert {"run.json", "timeline.txt", "project_files.txt"} <= set(names)
        assert not any("sk-secret-key-123456" in archive.read(name).decode() for name in names)
    from autosdlc.exports import redact
    assert redact("PGPASSWORD=hunter22 x") == "PGPASSWORD=*** x"
    assert redact("postgresql://app:hunter22@127.0.0.1/db") == "postgresql://app:***@127.0.0.1/db"
    assert redact("export const TOKEN_KEY = 'token';") == "export const TOKEN_KEY = 'token';"
    target = tmp_path / "report.zip"
    assert cli_main(["--workspace", str(tmp_path), "diagnose", result.id, "--out", str(target)]) == 0
    assert zipfile.is_zipfile(target)
