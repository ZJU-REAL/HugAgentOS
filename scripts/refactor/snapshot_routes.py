"""Print a deterministic route contract without starting application workers."""
from __future__ import annotations

import contextlib
import json
import sys


def snapshot(app):
    """Retain route order and duplicate routes, including non-OpenAPI endpoints."""
    return {
        "openapi_paths": sorted(app.openapi()["paths"]),
        "routes": [
            {
                "order": index,
                "path": getattr(route, "path", None),
                "methods": sorted(getattr(route, "methods", None) or []),
                "kind": type(route).__name__,
            }
            for index, route in enumerate(app.routes)
        ],
    }


def main():
    # Third-party import diagnostics must not contaminate the JSON stream.
    with contextlib.redirect_stdout(sys.stderr):
        from api.app import app

        result = snapshot(app)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
