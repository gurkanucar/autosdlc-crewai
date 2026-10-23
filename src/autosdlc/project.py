"""Validate the generated project's tool commands before executing them."""

from pathlib import Path

import yaml


COMMANDS = {"setup", "unit_test", "api_test", "ui_test", "start"}


def read_project_manifest(project_dir: Path, decisions: dict[str, str]) -> dict:
    path = project_dir / "project.yaml"
    if not path.is_file():
        raise ValueError("Developer must supply project.yaml")
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("project.yaml must contain a mapping")
    for key in COMMANDS:
        command = manifest.get(key)
        if command is not None and (not isinstance(command, list) or not command or len(command) > 20 or not all(isinstance(arg, str) and 0 < len(arg) < 300 for arg in command)):
            raise ValueError(f"project.yaml {key} must be a list of command arguments")
    port = manifest.get("port", 8000)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("project.yaml port must be 1024–65535")
    manifest["port"] = port
    if decisions["project_type"] in {"backend", "fullstack"} and not manifest.get("api_test"):
        raise ValueError("project.yaml requires api_test for backend/fullstack projects")
    if decisions["ui"] == "web" and (not manifest.get("ui_test") or not manifest.get("start")):
        raise ValueError("project.yaml requires ui_test and start for web UI projects")
    if decisions["project_type"] in {"cli", "library"} and not manifest.get("unit_test"):
        raise ValueError("project.yaml requires unit_test for CLI/library projects")
    if not (project_dir / "Dockerfile").is_file():
        raise ValueError("Developer must supply a Dockerfile for the selected stack")
    return manifest
