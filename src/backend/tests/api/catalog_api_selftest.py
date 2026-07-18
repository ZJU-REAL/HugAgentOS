"""Selftest: catalog API contract (kind strictness + toggle)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def main() -> int:
    try:
        from fastapi.testclient import TestClient
        from api.app import app
    except ModuleNotFoundError as e:
        print(f"catalog_api_selftest: SKIP (missing dependency: {e})")
        return 0

    import core.config.catalog as cat

    original_catalog_path = cat._CATALOG_PATH  # type: ignore[attr-defined]
    old_auth_mode = os.environ.get("AUTH_MODE")
    os.environ["AUTH_MODE"] = "mock"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "catalog.json"
        try:
            cat._CATALOG_PATH = tmp  # type: ignore[attr-defined]
            cat.ensure_default_catalog()

            client = TestClient(app)
            r = client.get("/v1/catalog")
            assert r.status_code == 200
            payload = r.json()
            data = payload.get("data", {})
            assert set(data.keys()) >= {"skills", "agents", "mcp", "kb"}

            skills = data.get("skills") or []
            if not skills:
                raise AssertionError("skills list should not be empty")
            skill_id = skills[0]["id"]

            ok = client.patch(f"/v1/catalog/skills/{skill_id}", json={"enabled": False})
            assert ok.status_code == 200, ok.text
            assert ok.json()["data"]["enabled"] is False

            bad = client.patch(f"/v1/catalog/unsupported/{skill_id}", json={"enabled": True})
            assert bad.status_code == 400
        finally:
            cat._CATALOG_PATH = original_catalog_path  # type: ignore[attr-defined]
            if old_auth_mode is None:
                os.environ.pop("AUTH_MODE", None)
            else:
                os.environ["AUTH_MODE"] = old_auth_mode

    print("catalog_api_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
