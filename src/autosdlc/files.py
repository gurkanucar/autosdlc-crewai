"""Artifact writing with a narrow path contract for generated source files."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


ALLOWED_PREFIXES = ("app/", "tests/")


def write_generated_files(project_dir: Path, files: dict[str, str]) -> None:
    if not files or len(files) > 12:
        raise ValueError("Agent must return between 1 and 12 files")
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("Generated file names and contents must be strings")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or not name.startswith(ALLOWED_PREFIXES)
            or path.suffix != ".py"
            or len(content.encode("utf-8")) > 50_000
        ):
            raise ValueError(f"Disallowed generated file: {name}")
        target = project_dir.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

