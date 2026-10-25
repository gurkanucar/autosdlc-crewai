"""Artifact writing with a narrow path contract for generated source files."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


RESERVED_DIRECTORIES = {"docs", "node_modules", "runs", ".venv", ".git"}
MAX_FILE_BYTES = 50_000
# Common dotfiles such as .gitignore and .github/ are allowed; secrets and environment folders are not.
FORBIDDEN_PARTS = {".git", ".venv", ".autosdlc-venv", ".env", ".ssh"}


def _forbidden_part(part: str) -> bool:
    return part in FORBIDDEN_PARTS or (part.startswith(".env.") and part != ".env.example")


def write_generated_files(project_dir: Path, files: dict[str, str]) -> None:
    if not files or len(files) > 40:
        raise ValueError("The agent must return 1–40 files")
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("Generated file names and contents must be text")
        path = PurePosixPath(name)
        if (
            not path.parts
            or
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != name
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or any(char in name for char in '<>"|?*')
            or any(part in {".", ".."} or _forbidden_part(part) for part in path.parts)
            or path.parts[0] in RESERVED_DIRECTORIES
        ):
            raise ValueError(f"Generated file not allowed: {name}")
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValueError(f"Generated file too large: {name} ({size // 1000} KB, at most {MAX_FILE_BYTES // 1000} KB)")
        target = project_dir.joinpath(*path.parts)
        if not target.resolve().is_relative_to(project_dir.resolve()):
            raise ValueError(f"The generated file escapes the project folder: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

