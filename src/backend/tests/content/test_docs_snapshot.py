"""Docs content snapshot export/import helpers and the /v1/content/docs routes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.db.models import ContentBlock
from core.files.content.content_blocks import (
    ContentSnapshotError,
    build_docs_snapshot,
    import_docs_snapshot,
)
from core.infra.time import utc_now


@pytest.fixture
def seeded(db_session):
    db_session.add_all(
        [
            ContentBlock(id="docs_updates", payload=[{"title": "old"}], updated_at=utc_now()),
            ContentBlock(id="docs_capabilities", payload=[{"title": "cap"}], updated_at=utc_now()),
        ]
    )
    db_session.commit()
    return db_session


def test_export_contains_stored_blocks(seeded):
    snapshot = build_docs_snapshot(seeded)

    assert snapshot["schema_version"] == 1
    assert snapshot["blocks"]["updates"]["payload"] == [{"title": "old"}]
    assert snapshot["blocks"]["capabilities"]["payload"] == [{"title": "cap"}]


def test_import_overwrites_and_stamps_default_author(seeded):
    result = import_docs_snapshot(
        seeded,
        {
            "schema_version": 1,
            "blocks": {
                "updates": {"payload": [{"title": "new"}]},
                "capabilities": {"payload": [{"title": "cap2"}]},
            },
        },
        overwrite=True,
        default_updated_by="tester",
    )

    assert result["count"] == 2
    row = seeded.get(ContentBlock, "docs_updates")
    assert row.payload == [{"title": "new"}]
    assert row.updated_by == "tester"


def test_import_without_overwrite_skips_existing(seeded):
    result = import_docs_snapshot(
        seeded,
        {"blocks": {"updates": {"payload": [{"title": "ignored"}]}}},
        overwrite=False,
    )

    assert result["skipped"] == ["updates"] and result["count"] == 0
    assert seeded.get(ContentBlock, "docs_updates").payload == [{"title": "old"}]


@pytest.mark.parametrize(
    "snapshot",
    [
        {"schema_version": 2, "blocks": {"updates": {"payload": []}}},
        {"blocks": {"no_such_block": {"payload": []}}},
        {"blocks": {"updates": {"payload": {"not": "a list"}}}},
        {"blocks": {}},
    ],
)
def test_invalid_snapshot_is_rejected(seeded, snapshot):
    with pytest.raises(ContentSnapshotError):
        import_docs_snapshot(seeded, snapshot)


def test_export_and_import_routes(seeded):
    from api.deps import require_admin
    from api.routes.v1 import content as content_route
    from core.db.engine import get_db

    app = FastAPI()
    app.include_router(content_route.router)
    app.dependency_overrides[get_db] = lambda: seeded
    app.dependency_overrides[require_admin] = lambda: None
    client = TestClient(app)

    exported = client.get("/v1/content/docs/export")
    assert exported.status_code == 200, exported.text
    assert exported.json()["data"]["blocks"]["updates"]["payload"] == [{"title": "old"}]

    imported = client.post(
        "/v1/content/docs/import",
        json={"schema_version": 1, "blocks": {"updates": {"payload": [{"title": "via-api"}]}}},
    )
    assert imported.status_code == 200, imported.text
    seeded.expire_all()
    row = seeded.get(ContentBlock, "docs_updates")
    assert row.payload == [{"title": "via-api"}]
    assert row.updated_by == "admin_import"
