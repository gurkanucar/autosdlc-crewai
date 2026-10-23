"""Validate the generated project's tool commands before executing them."""

from pathlib import Path

import yaml


COMMANDS = {"setup", "unit_test", "api_test", "ui_test", "start"}


def read_project_manifest(project_dir: Path, decisions: dict[str, str]) -> dict:
    path = project_dir / "project.yaml"
    if not path.is_file():
        raise ValueError("Geliştirme ajanı project.yaml üretmeli")
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("project.yaml bir sözlük içermeli")
    for key in COMMANDS:
        command = manifest.get(key)
        if command is not None and (not isinstance(command, list) or not command or len(command) > 20 or not all(isinstance(arg, str) and 0 < len(arg) < 300 for arg in command)):
            raise ValueError(f"project.yaml içindeki {key}, komut argümanları listesi olmalı")
    port = manifest.get("port", 8000)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("project.yaml port değeri 1024–65535 arasında olmalı")
    manifest["port"] = port
    if decisions["project_type"] in {"backend", "fullstack"} and not manifest.get("api_test"):
        raise ValueError("Backend/fullstack projede project.yaml api_test içermeli")
    if decisions["ui"] == "web" and (not manifest.get("ui_test") or not manifest.get("start")):
        raise ValueError("Web UI'lı projede project.yaml ui_test ve start içermeli")
    if decisions["project_type"] in {"cli", "library"} and not manifest.get("unit_test"):
        raise ValueError("CLI/library projesinde project.yaml unit_test içermeli")
    if not (project_dir / "Dockerfile").is_file():
        raise ValueError("Geliştirme ajanı seçilen teknoloji için Dockerfile üretmeli")
    return manifest
