"""Tamamlanan veya duraklatılan çalışmanın kayıt ve dosyalarını kaldırır."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

from .storage import History


def _remove_tree(path: Path) -> None:
    """Windows'ta salt okunur dosyalar (.git nesneleri, node_modules) rmtree'yi durdurmasın."""
    def retry(function, target, _error):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def delete_project(workspace: Path, run_id: str) -> None:
    root = Path(workspace).resolve()
    db = History(root / "runs" / "history.sqlite3")
    db.recover_stale()
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
        _remove_tree(project)
    if reports.is_dir():
        _remove_tree(reports)
    db.delete_record(run_id)
