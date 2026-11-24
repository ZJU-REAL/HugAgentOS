#!/usr/bin/env python3
"""Check that every image pins the CLI tool versions from docker/tool-versions.env.

The backend writes per-user credentials for dws / lark-cli / himalaya and the
sandbox images read them, so a version drift between images is a silent
failure. Dockerfiles cannot share one file (the cube image is built from a flat
context on a remote node), so each keeps an ``ARG NAME=value`` default; this
script fails when one of them, or the desktop Playwright pin, differs.

Usage: python scripts/check_tool_versions.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS_FILE = ROOT / "docker" / "tool-versions.env"

# File -> tool names it must pin. Files missing from the tree (for example the
# cube image in the CE derivation) are skipped.
EXPECTED = {
    "docker/Dockerfile": ["DWS_VERSION", "LARK_CLI_VERSION", "HIMALAYA_VERSION"],
    "docker/sandbox/Dockerfile.opensandbox": [
        "DWS_VERSION",
        "LARK_CLI_VERSION",
        "HIMALAYA_VERSION",
        "OFFICECLI_VERSION",
        "PLAYWRIGHT_VERSION",
    ],
    "docker/sandbox/Dockerfile.cube-sandbox": [
        "DWS_VERSION",
        "LARK_CLI_VERSION",
        "HIMALAYA_VERSION",
        "OFFICECLI_VERSION",
        "PLAYWRIGHT_VERSION",
    ],
    "docker/sandbox/Dockerfile.script-runner": [
        "LARK_CLI_VERSION",
        "HIMALAYA_VERSION",
        "OFFICECLI_VERSION",
        "PLAYWRIGHT_VERSION",
    ],
}
DESKTOP_REQUIREMENTS = "desktop/requirements-desktop.txt"


def load_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        versions[name.strip()] = value.strip()
    return versions


def dockerfile_args(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for match in re.finditer(r"^ARG\s+([A-Z_]+)=(\S+)\s*$", text, re.M):
        found.setdefault(match.group(1), []).append(match.group(2))
    return found


def main() -> int:
    versions = load_versions(VERSIONS_FILE)
    errors: list[str] = []
    for rel, names in EXPECTED.items():
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        args = dockerfile_args(text)
        for name in names:
            values = args.get(name)
            if not values:
                errors.append(f"{rel}: missing `ARG {name}=...`")
                continue
            for value in values:
                if value != versions[name]:
                    errors.append(f"{rel}: {name}={value}, expected {versions[name]}")
        # Inline pins bypass the ARG and are the drift this check exists for.
        for pattern in (r"@larksuite/cli@\d", r"\bplaywright@\d"):
            for match in re.finditer(pattern, text):
                line_no = text.count("\n", 0, match.start()) + 1
                errors.append(f"{rel}:{line_no}: inline version pin; use the ARG instead")

    desktop = ROOT / DESKTOP_REQUIREMENTS
    if desktop.is_file():
        match = re.search(r"^playwright==(\S+)", desktop.read_text(encoding="utf-8"), re.M)
        if not match:
            errors.append(f"{DESKTOP_REQUIREMENTS}: missing `playwright==` pin")
        elif match.group(1) != versions["PLAYWRIGHT_VERSION"]:
            errors.append(
                f"{DESKTOP_REQUIREMENTS}: playwright=={match.group(1)}, "
                f"expected {versions['PLAYWRIGHT_VERSION']}"
            )

    if errors:
        print("Tool versions drift from docker/tool-versions.env:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"Tool versions match docker/tool-versions.env ({len(versions)} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
