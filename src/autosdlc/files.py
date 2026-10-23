"""Artifact writing with a narrow path contract for generated source files."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


RESERVED_DIRECTORIES = {"docs", "node_modules", "runs", ".venv", ".git"}


def write_generated_files(project_dir: Path, files: dict[str, str]) -> None:
    if not files or len(files) > 40:
        raise ValueError("Agent must return between 1 and 40 files")
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("Generated file names and contents must be strings")
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
            or any(part.startswith(".") for part in path.parts)
            or path.parts[0] in RESERVED_DIRECTORIES
            or len(content.encode("utf-8")) > 50_000
        ):
            raise ValueError(f"Disallowed generated file: {name}")
        target = project_dir.joinpath(*path.parts)
        if not target.resolve().is_relative_to(project_dir.resolve()):
            raise ValueError(f"Generated file escapes project: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

