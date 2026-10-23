"""Removes the record and files of a completed or paused run."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

from .storage import History


def _remove_tree(path: Path) -> None:
    """On Windows read-only files (.git objects, node_modules) must not stop rmtree."""
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
        raise ValueError("Project not found")
    if row["status"] == "running":
        raise ValueError("Stop the running project before deleting it")
    reports = Path(row["output_dir"])
    project = Path(row["project_dir"])
    expected_reports = root / "runs" / run_id
    if reports.resolve() != expected_reports.resolve() or reports.is_symlink():
        raise ValueError("The report folder is not in the expected location")
    resolved_project = project.resolve()
    if project.is_symlink() or resolved_project in {root, root / "projects", root / "runs", reports.parent, reports} or resolved_project in root.parents:
        raise ValueError("The project folder is not a safe deletion target")
    if project.is_dir():
        _remove_tree(project)
    if reports.is_dir():
        _remove_tree(reports)
    db.delete_record(run_id)
