"""Durability without an application volume, including remote-write failures."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from core.files.artifacts import store
from core.files.content import public_assets
from core.files.storage.entries import StorageEntry


class Objects:
    def __init__(self):
        self.objects = {}
        self.fail = False

    def upload_bytes(self, content, key):
        if self.fail:
            raise ConnectionError("isolated object storage failure")
        self.objects[key] = content
        return key

    def exists(self, key):
        if self.fail:
            raise ConnectionError("isolated object storage failure")
        return key in self.objects

    def download_bytes(self, key):
        if self.fail:
            raise ConnectionError("isolated object storage failure")
        return self.objects[key]

    def download(self, key, path):
        Path(path).write_bytes(self.download_bytes(key))

    def stat_object(self, key):
        if not self.exists(key):
            return None
        return StorageEntry(name=Path(key).name, key=key, size=len(self.objects[key]),
                            modified_at=datetime.now(timezone.utc))


@pytest.fixture
def objects(tmp_path, monkeypatch):
    backend = Objects()
    monkeypatch.setenv("STORAGE_TYPE", "s3")
    monkeypatch.setattr(store, "_STORE_DIR", tmp_path / "records")
    monkeypatch.setattr(store.process_state(), "known_buckets", set())
    monkeypatch.setattr(store, "_get_oss_storage", lambda: backend)
    monkeypatch.setattr(public_assets, "get_storage", lambda: backend)
    return backend


def test_record_failure_is_not_acknowledged_or_cached(objects):
    objects.fail = True
    with pytest.raises(ConnectionError):
        store._record_artifact({"file_id": "aa11", "storage_key": "artifacts/aa11"})
    assert not store._record_path("aa11").exists()


def test_failed_update_preserves_last_durable_record(objects):
    store._record_artifact({"file_id": "aa11", "name": "old", "storage_key": "artifacts/aa11"})
    objects.fail = True
    with pytest.raises(ConnectionError):
        store._record_artifact({"file_id": "aa11", "name": "new", "storage_key": "artifacts/aa11"})
    assert store._read_record("aa11")["name"] == "old"


def test_artifact_record_recovers_in_a_new_empty_container(objects, tmp_path, monkeypatch):
    result = store.save_artifact_bytes(content=b"durable bytes", name="test.txt", extension="txt")
    monkeypatch.setattr(store, "_STORE_DIR", tmp_path / "new-container")
    restored = store.get_artifact(result["file_id"])
    assert restored == result
    assert objects.download_bytes(restored["storage_key"]) == b"durable bytes"


def test_cold_read_propagates_outage_and_rejects_wrong_record_identity(objects):
    objects.fail = True
    with pytest.raises(ConnectionError):
        store.get_artifact("aa11")
    objects.fail = False
    objects.objects[store._record_oss_key("aa11")] = json.dumps({"file_id": "../escape"}).encode()
    with pytest.raises(ValueError):
        store.get_artifact("aa11")
    assert store.get_artifact("../escape") is None


def test_public_assets_survive_without_local_directories_and_support_ranges(objects):
    from api.routes.v1.content import router
    url = public_assets.upload("manual", "manual.pdf", b"%PDF-1.7 test document")
    assert public_assets.manual_info()["url"] == url
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get(url.removeprefix("/api"), headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content == b"%PDF"
    assert response.headers["content-range"] == "bytes 0-3/22"


def test_public_asset_ranges_reuse_one_download_until_the_object_changes(objects, tmp_path, monkeypatch):
    from api.routes.v1.content import router
    monkeypatch.setattr(public_assets, "_cache_root", lambda: tmp_path / "asset-cache")
    downloads = []
    original = objects.download
    monkeypatch.setattr(objects, "download", lambda key, path: downloads.append(key) or original(key, path))
    stamp = datetime(2026, 10, 27, tzinfo=timezone.utc)
    monkeypatch.setattr(objects, "stat_object", lambda key: StorageEntry(
        name=Path(key).name, key=key, size=len(objects.objects[key]), modified_at=stamp,
        etag=str(hash(objects.objects[key]))) if key in objects.objects else None)
    url = public_assets.upload("manual", "manual.pdf", b"%PDF-1.7 first version")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for start in range(0, 20, 4):
            response = client.get(url.removeprefix("/api"), headers={"Range": f"bytes={start}-{start + 3}"})
            assert response.status_code == 206
        assert len(downloads) == 1
        public_assets.upload("manual", "manual.pdf", b"%PDF-1.7 second version!")
        assert client.get(url.removeprefix("/api")).content == b"%PDF-1.7 second version!"
    assert len(downloads) == 2
    # Only the current version is kept in the disposable cache.
    assert len(list((tmp_path / "asset-cache").iterdir())) == 1


def test_public_asset_survives_version_swap_and_cache_removal_mid_response(objects, tmp_path, monkeypatch):
    import os
    from api.routes.v1.content import router
    monkeypatch.setattr(public_assets, "_cache_root", lambda: tmp_path / "asset-cache")
    url = public_assets.upload("manual", "manual.pdf", b"%PDF-1.7 document body")
    real_link, calls = os.link, []

    def link(source, target):
        calls.append(source)
        if len(calls) == 1:  # another request replaced the cached version just now
            Path(source).unlink()
        return real_link(source, target)

    monkeypatch.setattr(public_assets.os, "link", link)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get(url.removeprefix("/api"))
    assert response.status_code == 200 and response.content == b"%PDF-1.7 document body"
    assert len(calls) == 2
    # Per-response links are removed once sent; only the cached version remains.
    assert [p.name.startswith(".serving-") for p in (tmp_path / "asset-cache").iterdir()] == [False]


def test_public_asset_policies_keep_pdf_viewable_and_svg_inert(objects, tmp_path, monkeypatch):
    from api.routes.v1.content import router
    monkeypatch.setattr(public_assets, "_cache_root", lambda: tmp_path / "asset-cache")
    manual = public_assets.upload("manual", "manual.pdf", b"%PDF-1.7 document")
    logo = public_assets.upload("page_config", "logo_123456789abc.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        pdf = client.get(manual.removeprefix("/api"))
        svg = client.get(logo.removeprefix("/api"))
    assert "content-security-policy" not in pdf.headers
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["cache-control"] == "no-cache"
    assert svg.headers["content-security-policy"].startswith("sandbox")
    assert svg.headers["x-content-type-options"] == "nosniff"


def test_public_assets_reject_arbitrary_bucket_keys(objects):
    from fastapi import HTTPException
    for kind, name in [("artifacts", "aa11"), ("manual", "other.pdf"),
                       ("page_config", "../secrets.json"), ("page_config", "logo_bad.svg")]:
        with pytest.raises(HTTPException) as error:
            public_assets.response(kind, name)
        assert error.value.status_code == 404


def test_asset_upload_propagates_storage_failure(objects):
    objects.fail = True
    with pytest.raises(ConnectionError):
        public_assets.upload("page_config", "logo_123456789abc.png", b"image")


def test_catalog_choices_survive_cache_and_local_file_loss(db_session, tmp_path, monkeypatch):
    from core.db import engine
    from core.capabilities.catalog import common, loader, api
    monkeypatch.setenv("STORAGE_TYPE", "s3")
    monkeypatch.setattr(engine, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(common, "CATALOG_PATH", tmp_path / "absent.json")
    defaults = {"skills": [{"id": "a", "enabled": True}, {"id": "b", "enabled": True}],
                "agents": [], "mcp": [], "kb": []}
    monkeypatch.setattr(loader, "_default_catalog", lambda: copy.deepcopy(defaults))
    loader.invalidate_catalog_cache()
    try:
        api.set_enabled("skills", "a", False)
        api.reorder_items("skills", ["b", "a"])
        loader.invalidate_catalog_cache()
        result = loader.load_catalog(include_runtime_details=False)
        assert [item["id"] for item in result["skills"]] == ["b", "a"]
        assert result["skills"][1]["enabled"] is False
        assert not common.CATALOG_PATH.exists()
    finally:
        loader.invalidate_catalog_cache()


def test_catalog_db_outage_does_not_fall_back_to_ephemeral_write(tmp_path, monkeypatch):
    from core.db import engine
    from core.capabilities.catalog import common, loader
    monkeypatch.setenv("STORAGE_TYPE", "s3")
    monkeypatch.setattr(common, "CATALOG_PATH", tmp_path / "absent.json")

    def unavailable():
        raise ConnectionError("isolated database failure")

    monkeypatch.setattr(engine, "SessionLocal", unavailable)
    with pytest.raises(ConnectionError):
        loader._write_catalog({"skills": []})
    assert not common.CATALOG_PATH.exists()
