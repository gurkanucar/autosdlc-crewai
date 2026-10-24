"""Executables are built only for the operating systems the user picks; Windows alone by default."""

from pathlib import Path

import yaml

from autosdlc.intake import applies, choice_label, questions, templates, validate_decisions

TASKS = Path(__file__).parents[1] / "src" / "autosdlc" / "config" / "tasks.yaml"


def test_platforms_default_to_windows_for_executables():
    decisions = validate_decisions({"project_type": "desktop", "stack": "Rust", "delivery": "binary"})
    assert decisions["platforms"] == "windows"
    assert validate_decisions({**decisions, "platforms": "all"})["platforms"] == "all"
    assert choice_label("platforms", "macos") == "macOS"


def test_platforms_only_apply_to_executables():
    item = questions()["platforms"]
    assert applies(item, {"project_type": "cli", "delivery": "binary"})
    assert not applies(item, {"project_type": "cli", "delivery": "script"})
    decisions = validate_decisions({"project_type": "cli", "stack": "Python", "delivery": "script", "platforms": "linux"})
    assert decisions["platforms"] == "none"
    docker = validate_decisions({"project_type": "backend", "stack": "Go", "database": "none", "cache": "none",
                                 "observability": "none", "ui": "none", "delivery": "docker"})
    assert docker["platforms"] == "none"
    # A stored project is validated again when it resumes; its "none" must still be accepted.
    assert validate_decisions(docker) == docker


def test_the_build_prompt_follows_the_platforms_decision():
    development = yaml.safe_load(TASKS.read_text(encoding="utf-8"))["development"]["description"]
    assert "`platforms` decision" in development


def test_binary_templates_name_their_platforms():
    binary = [item for item in templates() if item["decisions"]["delivery"] == "binary"]
    assert binary and all(item["decisions"].get("platforms") for item in binary)
