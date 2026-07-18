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
    # Import-time env guards for other modules.
    os.environ.setdefault("TAVILY_API_KEY", "DUMMY")
    os.environ.setdefault("DIFY_API_KEY", "DUMMY")
    os.environ.setdefault("DIFY_URL", "http://localhost")
    os.environ.setdefault("DATABASE_URL", "http://localhost")

    try:
        importlib.import_module("mcp_servers.generate_chart_tool_mcp.server")
        _ok("import server")
    except Exception as e:
        _fail(f"import server failed: {e!r}")

    try:
        chart_mod = importlib.import_module("mcp_servers.generate_chart_tool_mcp.chart")
        fn = getattr(chart_mod, "generate_chart_tool", None)
        if fn is None:
            _fail("chart.generate_chart_tool not found")
        if not (hasattr(fn, "invoke") or hasattr(fn, "run") or callable(fn)):
            _fail("chart.generate_chart_tool is not callable and has no invoke/run")
        _ok("import chart.generate_chart_tool")
    except Exception as e:
        _fail(f"import chart.generate_chart_tool failed: {e!r}")

    try:
        target = fn.func if hasattr(fn, "func") else fn
        sig = inspect.signature(target)
        for p in ("data", "query"):
            if p not in sig.parameters:
                _fail(f"generate_chart_tool signature missing {p!r}")
        _ok("signature check")
    except Exception as e:
        _fail(f"signature check failed: {e!r}")

    print("SELFTEST_PASS")


if __name__ == "__main__":
    main()
