"""A run can use a different model for single stages; every other stage keeps the run's model."""

import pytest

from autosdlc.cli import main as cli_main
from autosdlc.crewai_engine import CrewAIEngine
from autosdlc.runner import validate_stage_models
from autosdlc.storage import History, stage_models


def test_each_agent_call_uses_its_stage_model(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    models = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "# Document\n\nScope: one CRUD API."}}]}

    def fake_post(url, *, headers, json, timeout):
        models.append(json["model"])
        return Response()

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.post", fake_post)
    engine = CrewAIEngine(api_key="test-key", model="glm-5.3")
    engine.stage_models = {"analysis": "glm-5.3-flash"}
    engine.document("planning", "Book tracker", "")
    engine.document("analysis", "Book tracker", "")
    engine.document("design", "Book tracker", "")
    assert models == ["glm-5.3", "glm-5.3-flash", "glm-5.3"]


def test_stage_model_choices_are_validated_and_kept_with_the_run(tmp_path):
    assert validate_stage_models({"testing": " glm-5.3-flash ", "analysis": ""}) == {"testing": "glm-5.3-flash"}
    with pytest.raises(ValueError, match="Unknown stage"):
        validate_stage_models({"cooking": "glm-5.3"})
    db = History(tmp_path / "history.sqlite3")
    db.create("run123456789", "Idea", tmp_path / "out", tmp_path / "project", "glm-5.3", {"testing": "glm-5.3-flash"})
    assert stage_models(db.get("run123456789")) == {"testing": "glm-5.3-flash"}
    db.finish("run123456789", "failed", 0, "empty response")
    db.set_stage_models("run123456789", {})
    assert stage_models(db.get("run123456789")) == {}


def test_cli_rejects_a_stage_model_without_a_model(tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["--workspace", str(tmp_path), "run", "--idea", "Book tracker", "--no-prompt", "--stage-model", "testing"])
    assert exit_info.value.code == 2
