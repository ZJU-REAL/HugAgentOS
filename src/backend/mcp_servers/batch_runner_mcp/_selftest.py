from __future__ import annotations

import importlib
import inspect
import os
import sys


def _ok(msg: str) -> None:
    print(f"[OK] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    os.environ.setdefault("BACKEND_INTERNAL_URL", "http://localhost:3001")

    try:
        importlib.import_module("mcp_servers.batch_runner_mcp.server")
        _ok("import server")
    except Exception as e:
        _fail(f"import server failed: {e!r}")

    try:
        mod = importlib.import_module("mcp_servers.batch_runner_mcp._planner")
        fn = getattr(mod, "create_plan", None)
        if fn is None:
            _fail("_planner.create_plan not found")
        _ok("import _planner.create_plan")
    except Exception as e:
        _fail(f"import _planner failed: {e!r}")

    try:
        sig = inspect.signature(fn)
        for required in ("instruction", "file_ids", "text_items", "chat_id"):
            if required not in sig.parameters:
                _fail(f"create_plan signature missing '{required}'")
        _ok("signature check")
    except Exception as e:
        _fail(f"signature check failed: {e!r}")

    print("SELFTEST_PASS")


if __name__ == "__main__":
    main()
