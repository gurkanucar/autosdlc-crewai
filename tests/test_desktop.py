"""Desktop apps are their own project type: a "desktop app" idea must not be planned as a CLI with the GUI out of scope."""

from pathlib import Path

import yaml

from autosdlc.intake import choice_label, default_delivery, templates, validate_decisions
from autosdlc.project import read_project_manifest

TASKS = Path(__file__).parents[1] / "src" / "autosdlc" / "config" / "tasks.yaml"


def test_desktop_is_a_project_type_delivered_as_executables():
    decisions = validate_decisions({"project_type": "desktop", "stack": "Rust, eframe/egui"})
    assert decisions["database"] == "none" and decisions["ui"] == "none"
    assert default_delivery("desktop") == "binary"
    assert choice_label("project_type", "desktop") == "Desktop app"


def test_desktop_manifest_keeps_only_the_unit_test(tmp_path):
    manifest = {"unit_test": ["cargo", "test"], "start": ["cargo", "run"], "api_test": ["cargo", "run"], "port": 8080}
    (tmp_path / "project.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    decisions = validate_decisions({"project_type": "desktop", "stack": "Rust", "delivery": "binary"})
    assert set(read_project_manifest(tmp_path, decisions)) == {"unit_test", "port"}


def test_planning_keeps_what_the_idea_asks_for_in_scope():
    planning = yaml.safe_load(TASKS.read_text(encoding="utf-8"))["planning"]["description"]
    assert "never list it as out of scope" in planning


def test_desktop_templates_use_the_desktop_type():
    desktop = [item for item in templates() if "desktop" in item["id"]]
    assert desktop and all(item["decisions"]["project_type"] == "desktop" for item in desktop)
