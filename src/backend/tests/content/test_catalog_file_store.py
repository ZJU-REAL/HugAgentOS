"""Project-local catalog.json: default creation, enable toggles and legacy migration."""

from __future__ import annotations

import json

import pytest

import core.capabilities.catalog.api as cat
from core.capabilities.catalog import common as catalog_common
from core.capabilities.catalog.loader import invalidate_catalog_cache


@pytest.fixture
def catalog_path(tmp_path, monkeypatch):
    path = tmp_path / "catalog.json"
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setattr(catalog_common, "CATALOG_PATH", path)
    invalidate_catalog_cache()
    yield path
    invalidate_catalog_cache()


def test_default_catalog_is_written_with_frontend_shape(catalog_path):
    data = cat.ensure_default_catalog()

    for kind in ("skills", "agents", "mcp", "kb"):
        assert isinstance(data.get(kind), list)
    assert isinstance(json.loads(catalog_path.read_text(encoding="utf-8")), dict)


def test_set_enabled_persists_the_toggle(catalog_path):
    cat.ensure_default_catalog()

    cat.set_enabled("mcp", "internet_search", False)

    assert cat.is_enabled("mcp", "internet_search") is False
    stored = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert {"id": "internet_search", "enabled": False}.items() <= next(
        item for item in stored["mcp"] if item["id"] == "internet_search"
    ).items()


def test_set_enabled_rejects_unknown_kind(catalog_path):
    with pytest.raises(ValueError):
        cat.set_enabled("unsupported", "x", True)


def test_legacy_schema_is_migrated_and_router_strategy_dropped(catalog_path):
    legacy = {
        "router_strategy": {"enabled": True, "value": "rule_based"},
        "mcp_server": {"internet_search": {"enabled": False}},
    }
    catalog_path.write_text(json.dumps(legacy), encoding="utf-8")

    migrated = cat.get_catalog(fresh=True)

    assert set(migrated) >= {"skills", "agents", "mcp", "kb"}
    assert "router_strategy" not in migrated
    assert cat.is_enabled("mcp", "internet_search") is False
