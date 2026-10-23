"""Validate the generated project's tool commands before executing them."""

import shlex
from pathlib import Path

import yaml


COMMANDS = {"setup", "unit_test", "api_test", "ui_test", "start"}


def read_project_manifest(project_dir: Path, decisions: dict[str, str]) -> dict:
    path = project_dir / "project.yaml"
    if not path.is_file():
        raise ValueError("The development agent must produce project.yaml")
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("project.yaml must contain a mapping")
    for key in COMMANDS:
        command = manifest.get(key)
        # Models write an unused command as an empty list/string ("ui_test: []"): that means "none".
        if command in (None, [], ""):
            manifest.pop(key, None)
            continue
        # A single string such as "python -m pytest -q" is accepted too; it is split into arguments without a shell.
        if isinstance(command, str):
            command = manifest[key] = shlex.split(command)
        if not isinstance(command, list) or len(command) > 20 or not all(isinstance(arg, str) and 0 < len(arg) < 300 for arg in command):
            raise ValueError(f"{key} in project.yaml must be a list of command arguments (e.g. [python, -m, pytest, -q, tests])")
    port = manifest.get("port", 8000)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("The project.yaml port must be between 1024 and 65535")
    manifest["port"] = port
    if decisions["project_type"] in {"backend", "fullstack"} and not manifest.get("api_test"):
        raise ValueError("project.yaml must contain api_test in a backend/fullstack project")
    if decisions["ui"] == "web" and (not manifest.get("ui_test") or not manifest.get("start")):
        raise ValueError("project.yaml must contain ui_test and start in a project with a web UI")
    if decisions["project_type"] in {"cli", "library"} and not manifest.get("unit_test"):
        raise ValueError("project.yaml must contain unit_test in a CLI/library project")
    if not (project_dir / "Dockerfile").is_file():
        raise ValueError("The development agent must produce a Dockerfile for the chosen technology")
    return manifest
