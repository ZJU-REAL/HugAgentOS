"""Helpers for locating private desktop and system Office conversion tools."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional


def _bundled_tool(name: str) -> Optional[str]:
    # Windows: runtime/python/python.exe; POSIX: runtime/python/bin/python3.11.
    executable = Path(sys.executable).resolve()
    for root in (executable.parent.parent, executable.parent.parent.parent):
        manifest = root / "native-tools.json"
        if not manifest.is_file():
            continue
        try:
            tools = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(tools, dict):
                continue
            entry = tools.get(name)
            if not isinstance(entry, dict):
                continue
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                continue
            binary = (root / relative).resolve()
            binary.relative_to(root)
            if binary.is_file() and (os.name == "nt" or os.access(binary, os.X_OK)):
                return str(binary)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return None


def find_pandoc_binary() -> Optional[str]:
    return _bundled_tool("pandoc") or shutil.which("pandoc")


def find_libreoffice_binary() -> Optional[str]:
    bundled = _bundled_tool("libreoffice")
    if bundled:
        return bundled
    for command in ("libreoffice", "soffice"):
        resolved = shutil.which(command)
        if resolved:
            return resolved
    if os.name == "posix":
        binary = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
    return None
