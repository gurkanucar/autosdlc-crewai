"""Proje ve SDLC çıktılarının taşınabilir indirme paketleri."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

SKIPPED_DIRS = {".autosdlc-venv", ".venv", "node_modules", ".git", "__pycache__",
                ".pytest_cache", "test-results", "dist", "build", "target"}


def export_files(root: Path) -> list[Path]:
    """Bağımlılıkları, yerel sırları ve sembolik bağlantıları pakete katmaz."""
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS
                         and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            secret = name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"})
            if not secret and not path.is_symlink() and path.is_file():
                files.append(path.relative_to(root))
    return files


def export_zip(project: Path, output: Path, kind: str = "all") -> bytes:
    """all: proje + tüm aşama çıktıları; code: kaynaklar; documents: belge ve raporlar."""
    if kind not in {"all", "code", "documents"}:
        raise ValueError(f"Bilinmeyen paket: {kind}")
    entries: dict[str, Path] = {}
    if kind in {"all", "code"}:
        for relative in export_files(project):
            if kind == "code" and relative.parts[0] == "docs":
                continue
            entries[f"proje/{relative.as_posix()}"] = project / relative
    if kind == "documents":
        for relative in export_files(project / "docs"):
            entries[f"belgeler/{relative.as_posix()}"] = project / "docs" / relative
        if (project / "README.md").is_file():
            entries["README.md"] = project / "README.md"
    if kind in {"all", "documents"}:
        for relative in export_files(output):
            if kind == "documents" and relative.suffix.lower() not in {".md", ".yaml", ".yml", ".txt", ".svg", ".png", ".pdf", ".html"}:
                continue
            prefix = "sdlc" if kind == "all" else "belgeler"
            entries[f"{prefix}/{relative.as_posix()}"] = output / relative
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(entries.items()):
            archive.write(path, name)
    return buffer.getvalue()
