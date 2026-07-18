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
    os.environ.setdefault("TAVILY_API_KEY", "DUMMY")
    os.environ.setdefault("INTERNET_SEARCH_ENGINE", "tavily")

    try:
        importlib.import_module("mcp_servers.internet_search_mcp.server")
        _ok("import server")
    except Exception as e:
        _fail(f"import server failed: {e!r}")

    try:
        mod = importlib.import_module("mcp_servers.internet_search_mcp.impl")
        fn = getattr(mod, "internet_search", None)
        if fn is None:
            _fail("impl.internet_search not found")
        _ok("import impl.internet_search")
    except Exception as e:
        _fail(f"import impl.internet_search failed: {e!r}")

    try:
        sig = inspect.signature(fn)
        if "query" not in sig.parameters:
            _fail("internet_search signature missing 'query'")
        _ok("signature check")
    except Exception as e:
        _fail(f"signature check failed: {e!r}")

    print("SELFTEST_PASS")


if __name__ == "__main__":
    main()
