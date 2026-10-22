"""Strict filesystem and loopback guards for isolated observation fixtures only."""

import os
import re
import sys
from pathlib import Path

from fastapi import HTTPException, Request

ROOT = Path(__file__).resolve().parents[5]
SCRATCH = ROOT / ".scratch"


def isolated_path(name):
    raw = os.environ.get(name, "")
    path = Path(raw)
    if not raw or not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Absolute isolated test path required")
    # Check lexical containment first, then every existing path component.
    try:
        relative = path.relative_to(SCRATCH)
    except ValueError:
        raise RuntimeError("Test path must be inside this checkout scratch root") from None
    if not relative.parts or not re.fullmatch(
        r"workbench-[A-Za-z0-9][A-Za-z0-9_-]*", relative.parts[0]
    ):
        raise RuntimeError("Dedicated workbench test root required")
    current = SCRATCH
    if current.is_symlink():
        raise RuntimeError("Symlink test roots are prohibited")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError("Symlink test paths are prohibited")
    if path.exists() and not path.is_dir():
        raise RuntimeError("Isolated directory required")
    resolved = path.resolve()
    if not resolved.is_relative_to(SCRATCH.resolve()) or resolved == SCRATCH.resolve():
        raise RuntimeError("Resolved test path escapes isolated scratch root")
    return resolved


def guard_environment(path_name):
    if os.environ.get("WB19_METRICS_TESTONLY") != "1":
        raise RuntimeError("Explicit isolated test marker required")
    if not (os.environ.get("SANDBOX_RUNNER_TOKEN") or "").strip():
        raise RuntimeError("Isolated control token required")
    return isolated_path(path_name)


def guard_cli_binding():
    if os.environ.get("UVICORN_HOST", "127.0.0.1") != "127.0.0.1":
        raise RuntimeError("Test service must bind 127.0.0.1")
    for index, value in enumerate(sys.argv):
        if value.startswith("--host=") and value != "--host=127.0.0.1":
            raise RuntimeError("Test service must bind 127.0.0.1")
        if value == "--host" and (index + 1 == len(sys.argv) or sys.argv[index + 1] != "127.0.0.1"):
            raise RuntimeError("Test service must bind 127.0.0.1")


def require_loopback(request: Request):
    if not request.scope.get("server") or request.scope["server"][0] != "127.0.0.1":
        raise HTTPException(403, detail={"code": "test_observer_loopback_required"})
    if not request.client or request.client.host != "127.0.0.1":
        raise HTTPException(403, detail={"code": "test_observer_loopback_required"})


class LoopbackOnly:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            server, client = scope.get("server"), scope.get("client")
            if not server or server[0] != "127.0.0.1" or not client or client[0] != "127.0.0.1":
                from fastapi.responses import JSONResponse

                await JSONResponse(
                    status_code=403, content={"code": "test_observer_loopback_required"}
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)
