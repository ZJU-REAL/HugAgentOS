#!/usr/bin/env python3
"""Smoke-test the relocatable Python runtime embedded in desktop bundles."""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import sys
from pathlib import Path


REQUIRED_MODULES = (
    "agentscope",
    "fastapi",
    "fakeredis",
    "httpx",
    "mcp",
    "numpy",
    "pandas",
    "pikepdf",
    "pymilvus",
    "scipy",
    "sqlalchemy",
    "uvicorn",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()

    imported: list[str] = []
    for module_name in REQUIRED_MODULES:
        importlib.import_module(module_name)
        imported.append(module_name)

    if args.source:
        backend = args.source.resolve() / "src" / "backend"
        if not (backend / "cli.py").is_file():
            raise RuntimeError(f"CE source is missing cli.py: {backend}")
        sys.path.insert(0, str(backend))
        importlib.import_module("cli")
        imported.append("cli")

    print(
        json.dumps(
            {
                "ok": True,
                "python": platform.python_version(),
                "executable": sys.executable,
                "modules": imported,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
