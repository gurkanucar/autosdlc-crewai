"""Local desktop operations: opening a folder in the operating system's file manager."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def can_open_folders() -> bool:
    """False on a server (a Linux container without a display or xdg-open), where the button only showed an error."""
    if sys.platform in {"win32", "darwin"}:
        return True
    return bool(shutil.which("xdg-open")) and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_folder(path: Path | str) -> None:
    """Opens it with Explorer on Windows, Finder on macOS and the default file manager on Linux.

    The UI runs on the local computer, so the folder opens on the user's own screen.
    """
    folder = Path(path).resolve()
    if not folder.is_dir():
        raise ValueError(f"Folder not found: {folder}")
    try:
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]  # Only exists on Windows.
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)], check=True, timeout=15)
        else:
            subprocess.run(["xdg-open", str(folder)], check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Could not open the folder ({exc}). Path: {folder}") from exc
