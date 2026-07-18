"""Unit test for ``backfill_navigation_entries``.

Verifies the three states:
1. Row missing → no-op, returns 0
2. Row exists but missing 'projects' → adds to sidebar_items/panel_titles/panel_subtitles
3. Row already up-to-date → idempotent, returns 0
"""
from __future__ import annotations

from unittest.mock import MagicMock

from core.content.content_blocks import backfill_navigation_entries


def _make_row(payload: dict) -> MagicMock:
    row = MagicMock()
    row.payload = payload
    return row


def _make_db(row):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    return db


def test_no_row_returns_zero():
    db = _make_db(None)
    assert backfill_navigation_entries(db) == 0
    db.commit.assert_not_called()


def test_full_backfill_when_all_three_missing():
    row = _make_row({
        "navigation": {
            "sidebar_items": ["agents", "kb", "app_center", "my_space"],
            "panel_titles": {"app_center": "应用中心"},
            "panel_subtitles": {"app_center": "..."},
        },
    })
    db = _make_db(row)
    changed = backfill_navigation_entries(db)
    # 1 sidebar insert + 1 title + 1 subtitle = 3 fields
    assert changed == 3
    nav = row.payload["navigation"]
    # Inserted after 'app_center'
    assert nav["sidebar_items"] == ["agents", "kb", "app_center", "projects", "my_space"]
    assert nav["panel_titles"]["projects"] == "项目"
    assert nav["panel_subtitles"]["projects"] == "把对话、文件和指令打包成专属工作空间"
    db.commit.assert_called_once()


def test_idempotent_when_already_present():
    row = _make_row({
        "navigation": {
            "sidebar_items": ["agents", "kb", "app_center", "projects", "my_space"],
            "panel_titles": {"projects": "Custom Title"},
            "panel_subtitles": {"projects": "Custom Sub"},
        },
    })
    db = _make_db(row)
    changed = backfill_navigation_entries(db)
    assert changed == 0
    db.commit.assert_not_called()
    # Don't overwrite custom values
    assert row.payload["navigation"]["panel_titles"]["projects"] == "Custom Title"


def test_anchor_missing_falls_back_to_append():
    row = _make_row({
        "navigation": {
            "sidebar_items": ["agents", "kb"],  # no app_center anchor
            "panel_titles": {},
            "panel_subtitles": {},
        },
    })
    db = _make_db(row)
    changed = backfill_navigation_entries(db)
    assert changed == 3
    # Appended at end because 'app_center' not found
    assert row.payload["navigation"]["sidebar_items"] == ["agents", "kb", "projects"]


def test_malformed_payload_returns_zero():
    row = _make_row({"navigation": "not_a_dict"})
    db = _make_db(row)
    assert backfill_navigation_entries(db) == 0
    db.commit.assert_not_called()


def test_partial_backfill_when_only_sidebar_missing():
    row = _make_row({
        "navigation": {
            "sidebar_items": ["agents", "kb", "app_center", "my_space"],
            "panel_titles": {"projects": "项目"},  # already has
            "panel_subtitles": {"projects": "已有"},  # already has
        },
    })
    db = _make_db(row)
    changed = backfill_navigation_entries(db)
    # Only the sidebar list mutated
    assert changed == 1
    nav = row.payload["navigation"]
    assert "projects" in nav["sidebar_items"]
    assert nav["panel_titles"]["projects"] == "项目"
    assert nav["panel_subtitles"]["projects"] == "已有"
