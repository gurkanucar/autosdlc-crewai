"""Validate the generated project's tool commands before executing them."""

import shlex
from pathlib import Path

import yaml


COMMANDS = {"setup", "unit_test", "api_test", "ui_test", "start", "package"}
# Project types that run on the user's own machine: no server, tested through unit tests only.
LOCAL_TYPES = {"cli", "desktop", "library"}
SHELL_OPERATORS = {"&&", "||", ";", "|", ">", ">>", "<", "&"}
# Programs that start a command of their own; one in the middle of an argument list is a second command.
PROGRAMS = {"node", "npm", "npx", "pnpm", "yarn", "corepack", "python", "python3", "pip", "pip3", "go", "cargo", "sh",
            "bash"}
# Arguments after which a program name is what the command runs, not a second command (pnpm exec node, python -m pip).
RUNS_NEXT = {"exec", "run", "dlx", "x", "-m", "--", "-c", "uv", "env"}


def chained_command(command: list[str]) -> str | None:
    """Why an argument list that runs without a shell holds more than one command; None when it holds one.

    `a && b` written as one list once lost its && and ran `pnpm exec playwright install chromium node --test ...`:
    Playwright answered "unknown option '--test'" and no UI test ran."""
    if command[:1] in (["sh"], ["bash"]) and "-c" in command[1:2]:
        return None
    operator = next((arg for arg in command if arg in SHELL_OPERATORS), None)
    if operator:
        return f"contains {operator!r}"
    # The first program may be a wrapper (xvfb-run python, timeout 60 node, cross-env A=1 node).
    for previous, arg in zip(command[1:], command[2:]):
        if (arg in PROGRAMS and previous not in RUNS_NEXT and not previous.startswith("-") and "=" not in previous
                and not previous.isdigit()):
            return f"starts a second command at {arg!r}"
    return None


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
        chained = chained_command(command)
        if chained:
            raise ValueError(f"{key} in project.yaml {chained}, but the command runs without a shell; write several "
                             f"commands as [sh, -c, \"first && second\"] (or put them in a script run with sh)")
    if decisions["project_type"] in LOCAL_TYPES and decisions["ui"] != "web":
        # Models sometimes fill every key; a CLI or desktop app has no server to start and no HTTP API to test, and
        # launching it as a "server" exits at once (or opens a window). unit_test is the whole contract for these types.
        for key in ("api_test", "ui_test", "start"):
            manifest.pop(key, None)
    port = manifest.get("port", 8000)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("The project.yaml port must be between 1024 and 65535")
    manifest["port"] = port
    if decisions["project_type"] in {"backend", "fullstack"} and not manifest.get("api_test"):
        raise ValueError("project.yaml must contain api_test in a backend/fullstack project")
    if decisions["ui"] == "web" and (not manifest.get("ui_test") or not manifest.get("start")):
        raise ValueError("project.yaml must contain ui_test and start in a project with a web UI")
    if decisions["project_type"] in LOCAL_TYPES and not manifest.get("unit_test"):
        raise ValueError("project.yaml must contain unit_test in a CLI/desktop/library project")
    if decisions.get("delivery", "docker") == "docker" and not (project_dir / "Dockerfile").is_file():
        raise ValueError("The development agent must produce a Dockerfile for the chosen technology")
    return manifest
