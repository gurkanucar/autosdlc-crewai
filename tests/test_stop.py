"""Stop pauses a run at once: a model answer still streaming and a running command are cut off."""

import json
import sys
import time

import pytest

from autosdlc.crewai_engine import CrewAIEngine
from autosdlc.runner import RunPaused, _run_logged, run_project
from autosdlc.storage import History

BACKEND = {"project_type": "backend", "stack": "Python / FastAPI", "database": "sqlite", "cache": "none",
           "observability": "none", "ui": "none"}


def test_stop_cuts_off_a_model_call_that_is_still_reasoning(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    run_id = "stop12345678"
    db = History(tmp_path / "runs" / "history.sqlite3")
    calls = []

    class Stream:
        # A model that would reason for a very long time before answering.
        status_code, headers = 200, {"content-type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            for number in range(100_000):
                if number == 3:
                    db.request_pause(run_id)
                yield "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "thinking "}}]})
                time.sleep(0.01)

    def fake_stream(*args, **kwargs):
        calls.append(1)
        return Stream()

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.stream", fake_stream)
    started = time.monotonic()
    result = run_project("Book tracker", workspace=tmp_path, engine=CrewAIEngine(api_key="test-key"),
                         decisions=BACKEND, run_id=run_id)
    assert result.status == "paused" and db.get(run_id)["status"] == "paused"
    assert time.monotonic() - started < 30
    assert len(calls) == 1  # the abandoned call is not retried
    activity = json.loads((result.output_dir / "live" / "activity.json").read_text(encoding="utf-8"))
    assert activity["phase"] == "stopped"


def test_stop_ends_a_running_command(tmp_path):
    started = time.monotonic()
    with pytest.raises(RunPaused):
        _run_logged([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, {}, tmp_path / "x.log",
                    timeout=120, should_stop=lambda: time.monotonic() - started > 1)
    assert time.monotonic() - started < 15


def test_a_command_without_output_is_stopped_as_hung(tmp_path):
    started = time.monotonic()
    output, code = _run_logged([sys.executable, "-u", "-c", "print('ok 1'); import time; time.sleep(60)"], tmp_path,
                               {}, tmp_path / "x.log", timeout=120, silence=2)
    assert code is None and output.startswith("stopped after 2 seconds without output") and "ok 1" in output
    assert time.monotonic() - started < 15


def test_a_command_that_keeps_writing_is_not_stopped_by_the_silence_limit(tmp_path):
    script = "import time\nfor i in range(5):\n    print(i, flush=True)\n    time.sleep(1)\n"
    output, code = _run_logged([sys.executable, "-c", script], tmp_path, {}, tmp_path / "x.log", timeout=120,
                               silence=2)
    assert code == 0 and "4" in output
