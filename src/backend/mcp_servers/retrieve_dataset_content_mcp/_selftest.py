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
    os.environ.setdefault("DIFY_API_KEY", "DUMMY")
    os.environ.setdefault("DIFY_URL", "http://localhost")

    try:
        importlib.import_module("mcp_servers.retrieve_dataset_content_mcp.server")
        _ok("import server")
    except Exception as e:
        _fail(f"import server failed: {e!r}")

    try:
        mod = importlib.import_module("mcp_servers.retrieve_dataset_content_mcp.impl")
        fn = getattr(mod, "retrieve_dataset_content", None)
        if fn is None:
            _fail("impl.retrieve_dataset_content not found")
        _ok("import impl.retrieve_dataset_content")
    except Exception as e:
        _fail(f"import impl.retrieve_dataset_content failed: {e!r}")

    try:
        sig = inspect.signature(fn)
        for k in ["dataset_id", "query"]:
            if k not in sig.parameters:
                _fail(f"retrieve_dataset_content signature missing '{k}'")
        _ok("signature check")
    except Exception as e:
        _fail(f"signature check failed: {e!r}")

    print("SELFTEST_PASS")


if __name__ == "__main__":
    main()
