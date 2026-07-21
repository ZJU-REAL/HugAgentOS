"""Helpers for locating the host Office conversion runtime."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def find_libreoffice_binary() -> Optional[str]:
    """Return a usable LibreOffice CLI path, including the standard macOS app path."""
    for command in ("libreoffice", "soffice"):
        resolved = shutil.which(command)
        if resolved:
            return resolved

    if os.name == "posix":
        macos_binary = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if macos_binary.is_file() and os.access(macos_binary, os.X_OK):
            return str(macos_binary)

    return None
