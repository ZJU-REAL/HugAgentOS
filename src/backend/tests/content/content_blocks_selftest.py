"""Selftest: docs content snapshot export/import helpers and API routes."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
ROUTES_V1_DIR = PROJECT_ROOT / "api" / "routes" / "v1"
if str(ROUTES_V1_DIR) not in sys.path:
    sys.path.insert(0, str(ROUTES_V1_DIR))


@dataclass
class FakeRow:
    id: str
    payload: list[Any]
    updated_at: Any = None
    updated_by: str | None = None


class FakeQuery:
    def __init__(self, session: "FakeSession"):
        self.session = session
        self._lookup: Any = None

    def filter(self, condition):
        self._lookup = getattr(condition.right, "value", None)
        return self

    def first(self):
        return self.session.rows.get(self._lookup)

    def all(self):
        if isinstance(self._lookup, list):
            return [self.session.rows[row_id] for row_id in self._lookup if row_id in self.session.rows]
        return list(self.session.rows.values())


class FakeSession:
    def __init__(self, rows: dict[str, Any] | None = None):
        self.rows = rows or {}
        self.committed = False

    def query(self, _model):
        return FakeQuery(self)

    def add(self, row):
        self.rows[row.id] = row

    def commit(self):
        self.committed = True


def main() -> int:
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import content as content_route
        from core.content.content_blocks import build_docs_snapshot, import_docs_snapshot
        from core.db.engine import get_db
    except ModuleNotFoundError as exc:
        print(f"content_blocks_selftest: SKIP (missing dependency: {exc})")
        return 0

    old_token = os.environ.get("ADMIN_TOKEN")
    os.environ["ADMIN_TOKEN"] = "content-test-token"
    content_route.ADMIN_TOKEN = "content-test-token"

    fake_db = FakeSession(
        rows={
            "docs_updates": FakeRow("docs_updates", [{"title": "old"}]),
            "docs_capabilities": FakeRow("docs_capabilities", [{"title": "cap"}]),
        }
    )

    snapshot = build_docs_snapshot(fake_db)
    assert snapshot["schema_version"] == 1
    assert snapshot["blocks"]["updates"]["payload"] == [{"title": "old"}]
    assert snapshot["blocks"]["capabilities"]["payload"] == [{"title": "cap"}]

    result = import_docs_snapshot(
        fake_db,
        {
            "schema_version": 1,
            "blocks": {
                "updates": {"payload": [{"title": "new"}]},
                "capabilities": {"payload": [{"title": "cap2"}]},
            },
        },
        overwrite=True,
        default_updated_by="selftest",
    )
    assert result["count"] == 2
    assert fake_db.rows["docs_updates"].payload == [{"title": "new"}]
    assert fake_db.rows["docs_updates"].updated_by == "selftest"

    def override_get_db():
        yield fake_db

    app = FastAPI()
    app.include_router(content_route.router)
    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)

        export_resp = client.get(
            "/v1/content/docs/export",
            headers={"Authorization": "Bearer content-test-token"},
        )
        assert export_resp.status_code == 200, export_resp.text
        exported = export_resp.json()["data"]
        assert exported["blocks"]["updates"]["payload"] == [{"title": "new"}]

        import_resp = client.post(
            "/v1/content/docs/import",
            headers={"Authorization": "Bearer content-test-token"},
            json={
                "schema_version": 1,
                "blocks": {
                    "updates": {"payload": [{"title": "via-api"}]},
                },
            },
        )
        assert import_resp.status_code == 200, import_resp.text
        assert fake_db.rows["docs_updates"].payload == [{"title": "via-api"}]
    finally:
        app.dependency_overrides.pop(get_db, None)
        if old_token is None:
            os.environ.pop("ADMIN_TOKEN", None)
            content_route.ADMIN_TOKEN = ""
        else:
            os.environ["ADMIN_TOKEN"] = old_token
            content_route.ADMIN_TOKEN = old_token

    print("content_blocks_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
