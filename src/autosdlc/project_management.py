"""Tamamlanan veya duraklatılan çalışmanın kayıt ve dosyalarını kaldırır."""

from __future__ import annotations

import shutil
from pathlib import Path

from .storage import History


def delete_project(workspace: Path, run_id: str) -> None:
    root = Path(workspace).resolve()
    db = History(root / "runs" / "history.sqlite3")
    row = db.get(run_id)
    if row is None:
        raise ValueError("Proje bulunamadı")
    if row["status"] == "running":
        raise ValueError("Çalışan projeyi silmeden önce durdur")
    reports = Path(row["output_dir"])
    project = Path(row["project_dir"])
    expected_reports = root / "runs" / run_id
    if reports.resolve() != expected_reports.resolve() or reports.is_symlink():
        raise ValueError("Rapor klasörü beklenen konumda değil")
    resolved_project = project.resolve()
    if project.is_symlink() or resolved_project in {root, root / "projects", root / "runs", reports.parent, reports} or resolved_project in root.parents:
        raise ValueError("Proje klasörü güvenli bir silme hedefi değil")
    if project.is_dir():
        shutil.rmtree(project)
    if reports.is_dir():
        shutil.rmtree(reports)
    db.delete_record(run_id)
