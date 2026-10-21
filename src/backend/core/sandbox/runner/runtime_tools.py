"""Resolve the desktop-owned shell before optional host interpreters."""

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional


def resolve_bash_executable() -> Optional[str]:
    """Find a native Bash, excluding Windows' WSL launcher stubs."""
    # The desktop runtime owns Bash. Never silently use developer tools when
    # a declared private runtime is damaged.
    executable = Path(sys.executable).resolve()
    for root in (executable.parent.parent, executable.parent.parent.parent):
        manifest = root / "native-tools.json"
        if not manifest.is_file():
            continue
        tools = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(tools, dict) or "git-bash" not in tools:
            continue
        entry = tools["git-bash"]
        expected = "native/git-bash/usr/bin/bash.exe"
        if not isinstance(entry, dict) or entry.get("path") != expected:
            raise RuntimeError("Bundled Bash manifest is invalid")
        binary = (root / expected).resolve()
        if not binary.is_relative_to(root) or not binary.is_file():
            raise RuntimeError("Bundled Bash is missing or outside the private runtime")
        return str(binary)

    configured = os.getenv("SCRIPT_RUNNER_BASH", "").strip()
    candidates = [configured, shutil.which("bash") or ""]
    if os.name == "nt":
        for root in (
            os.getenv("ProgramFiles", ""),
            os.getenv("ProgramFiles(x86)", ""),
            str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs"),
        ):
            if root:
                candidates.append(str(Path(root) / "Git" / "bin" / "bash.exe"))

    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        normalized = candidate.replace("/", "\\").casefold()
        if os.name == "nt" and (
            "\\windows\\system32\\bash.exe" in normalized
            or "\\microsoft\\windowsapps\\bash.exe" in normalized
        ):
            continue
        return candidate
    return None


def windows_tool_path_entries(bash_executable: Optional[str]) -> list[str]:
    """GNU file commands must win over Windows find.exe and sort.exe."""
    entries = []
    if bash_executable:
        git_bin = Path(bash_executable).parent
        git_root = git_bin.parent.parent if git_bin.parent.name == "usr" else git_bin.parent
        entries.extend((str(git_bin), str(git_root / "usr/bin"), str(git_root / "cmd")))
    system_root = os.getenv("SYSTEMROOT") or os.getenv("WINDIR")
    if system_root:
        root = Path(system_root)
        entries.extend(
            (str(root), str(root / "System32"), str(root / "System32/WindowsPowerShell/v1.0"))
        )
    return list(dict.fromkeys(entries))
