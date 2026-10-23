"""Yerel masaüstü işlemleri: bir klasörü işletim sisteminin dosya yöneticisinde açmak."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def open_folder(path: Path | str) -> None:
    """Windows'ta Explorer, macOS'ta Finder, Linux'ta varsayılan dosya yöneticisiyle açar.

    Arayüz yerel bilgisayarda çalıştığı için klasör, kullanıcının kendi ekranında açılır.
    """
    folder = Path(path).resolve()
    if not folder.is_dir():
        raise ValueError(f"Klasör bulunamadı: {folder}")
    try:
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]  # Yalnızca Windows'ta vardır.
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)], check=True, timeout=15)
        else:
            subprocess.run(["xdg-open", str(folder)], check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Klasör açılamadı ({exc}). Yol: {folder}") from exc
