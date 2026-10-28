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
    from autosdlc.i18n import translate_stored
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
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-secret-key-123456")
    engine = CrewAIEngine()
    decisions = {"project_type": "backend", "stack": "Python / FastAPI", "database": "sqlite", "cache": "none",
                 "observability": "none", "ui": "none"}
    result = run_project("Book tracker", workspace=tmp_path, engine=engine, decisions=decisions)
    assert result.status == "failed" and "empty response" in result.error
    # The error says that the model only reasoned and what to change.
    assert "spent its whole output limit on reasoning" in result.error and "planning stage" in result.error
    assert "başka bir model seçip devam et" in translate_stored(result.error, "tr")
    # The live activity view shows the last model call and how it ended.
    activity = json.loads((result.output_dir / "live" / "activity.json").read_text(encoding="utf-8"))
    assert activity["kind"] == "model" and activity["stage"] == "planning" and activity["phase"] == "failed"
    assert activity["outcome"] == "empty" and activity["reasoning_chars"] > 0
    (result.output_dir / "tmp").mkdir(exist_ok=True)
    (result.output_dir / "tmp" / "compile.log").write_text("Authorization: Bearer abcdefghijkl\nerror: boom", encoding="utf-8")
    for name, content in {"project.yaml": "api_test: [sh, scripts/api.sh]\n", "scripts/api.sh": "pytest tests\n",
                          "tests/test_api.py": "def test_x():\n    pass\n", "app.py": "SECRET_APP_CODE = 1\n"}.items():
        (result.project_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (result.project_dir / name).write_text(content, encoding="utf-8")
    db = History(tmp_path / "runs" / "history.sqlite3")
    with zipfile.ZipFile(io.BytesIO(diagnostics_zip(db.get(result.id)))) as archive:
        names = archive.namelist()
        # The tests and the commands that run them are included; the application code is not.
        assert {"project/project.yaml", "project/scripts/api.sh", "project/tests/test_api.py"} <= set(names)
        assert "project/app.py" not in names
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
    from autosdlc.exports import diagnostics_text, redact
    (result.output_dir / "build_checks").mkdir()
    (result.output_dir / "build_checks" / "check-01-development.txt").write_text(
        "[31m[MISSING_EXPORT][0m UNAUTHORIZED_EVENT", encoding="utf-8")
    text = diagnostics_text(db.get(result.id))
    assert "## Latest model calls" in text and "planning · empty · finish=length" in text
    assert "[MISSING_EXPORT] UNAUTHORIZED_EVENT" in text and "Traceback" in text
    assert redact("PGPASSWORD=hunter22 x") == "PGPASSWORD=*** x"
    assert redact("postgresql://app:hunter22@127.0.0.1/db") == "postgresql://app:***@127.0.0.1/db"
    assert redact("export const TOKEN_KEY = 'token';") == "export const TOKEN_KEY = 'token';"
    target = tmp_path / "report.zip"
    assert cli_main(["--workspace", str(tmp_path), "diagnose", result.id, "--out", str(target)]) == 0
    assert zipfile.is_zipfile(target)


def test_times_are_shown_in_the_configured_time_zone(monkeypatch):
    from autosdlc.storage import clock

    monkeypatch.delenv("AUTOSDLC_TIMEZONE", raising=False)
    assert clock("2026-10-24T17:28:21+00:00") == "20:28:21"  # Europe/Istanbul by default
    monkeypatch.setenv("AUTOSDLC_TIMEZONE", "Europe/Berlin")
    assert clock("2026-10-24T17:28:21+00:00", with_date=True) == "2026-10-24 19:28:21"
    monkeypatch.setenv("AUTOSDLC_TIMEZONE", "Not/AZone")
    assert clock("2026-10-24T17:28:21+00:00") == "17:28:21"  # an unknown name falls back to UTC


def test_a_stopped_run_can_continue_with_another_model(tmp_path):
    from autosdlc.storage import History

    db = History(tmp_path / "history.sqlite3")
    db.create("run123456789", "Idea", tmp_path / "out", tmp_path / "project", "glm-5.3")
    db.set_model("run123456789", "glm-5.3-flash")
    assert db.get("run123456789")["model"] == "glm-5.3"  # never while it runs
    db.finish("run123456789", "failed", 0, "OpenCode Go returned an empty response")
    db.set_model("run123456789", "glm-5.3-flash")
    assert db.get("run123456789")["model"] == "glm-5.3-flash"


def test_a_new_model_call_clears_the_previous_live_output(tmp_path):
    from types import SimpleNamespace

    from autosdlc.runner import RunSession

    (tmp_path / "live").mkdir()
    (tmp_path / "live" / "testing.md").write_text("tests of the previous call", encoding="utf-8")
    session = SimpleNamespace(output_dir=tmp_path, _activity_written=0.0, _live_written=99.0)
    RunSession._activity(session, {"kind": "model", "stage": "testing", "phase": "reasoning"})
    assert (tmp_path / "live" / "testing.md").is_file()
    session._activity_written = 0.0
    RunSession._activity(session, {"kind": "model", "stage": "testing", "phase": "waiting"})
    assert not (tmp_path / "live" / "testing.md").exists() and session._live_written == 0.0


def test_all_documents_become_one_page_with_mermaid_left_to_the_browser(tmp_path):
    from autosdlc.exports import documents_html

    (tmp_path / "requirements.md").write_text("# Gereksinimler\n\n| FR | Açıklama |\n|---|---|\n| FR-01 | Kayıt |\n",
                                              encoding="utf-8")
    (tmp_path / "architecture.md").write_text(
        "# Tasarım\n\n```mermaid\ngraph TD\n  A-->B\n```\n\n<script>alert(1)</script>\n\nDoğrulanan <!-- test-result -->",
        encoding="utf-8")
    html = documents_html(tmp_path, "Etkinlik", "<svg id='lifecycle'></svg>")
    # Reading order, with the missing documents left out.
    assert html.index("Gereksinimler") < html.index("Tasarım") and "planning" not in html
    assert '<a href="#doc2">Gereksinimler</a>' in html and "<svg id='lifecycle'></svg>" in html
    assert "<table>" in html and "<td>FR-01</td>" in html
    assert '<pre class="mermaid">graph TD\n  A--&gt;B\n</pre>' in html
    # The documents are model output: their HTML is shown as text, never run.
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "test-result" not in html


def test_the_documents_pdf_is_printed_by_chromium(tmp_path):
    import pytest

    from autosdlc.exports import documents_html, documents_pdf

    (tmp_path / "planning.md").write_text("# Plan\n\nİlk sürüm.", encoding="utf-8")
    try:
        pdf = documents_pdf(documents_html(tmp_path, "Etkinlik"))
    except Exception as exc:  # no Chromium installed on this machine
        pytest.skip(f"Chromium is not available: {exc}")
    assert pdf.startswith(b"%PDF")
