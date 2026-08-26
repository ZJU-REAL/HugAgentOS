"""Unit test for ``backfill_navigation_entries``.

Covers:
1. Row missing / malformed → no-op, returns 0
2. Missing entries → seeded into their declared bucket (sidebar_items or menu_items)
   plus panel_titles / panel_subtitles
3. Already up-to-date → idempotent, returns 0, custom copy preserved
4. Anchor absent → degrade to appending at the end
5. Entry parked in the *other* bucket → treated as present, never duplicated
"""
from __future__ import annotations

from unittest.mock import MagicMock

from core.content.content_blocks import (
    DEFAULT_PAGE_CONFIG,
    backfill_navigation_entries,
)


def _make_row(payload: dict) -> MagicMock:
    row = MagicMock()
    row.payload = payload
    return row


def _make_db(row):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    return db


def _nav(sidebar: list[str], menu: list[str], titles=None, subtitles=None) -> dict:
    """Build a navigation payload that already contains every whitelisted entry except
    the ones the individual test wants to exercise, so each test isolates one behaviour."""
    return {
        "navigation": {
            "sidebar_items": sidebar,
            "menu_items": menu,
            "panel_titles": titles if titles is not None else {},
            "panel_subtitles": subtitles if subtitles is not None else {},
        },
    }


# Every key in _NAV_BACKFILL_ENTRIES; used to build "steady state" fixtures.
_ALL_TITLES = {
    "projects": "项目",
    "automation": "定时任务",
    "sites": "站点",
}
_ALL_SUBTITLES = {k: f"{k} sub" for k in _ALL_TITLES}


def test_homepage_defaults_focus_on_suggestions():
    homepage = DEFAULT_PAGE_CONFIG["homepage"]
    assert homepage["show_logo"] is True
    assert homepage["logo_url"] == "/icon.png"
    assert homepage["show_suggestions"] is True
    assert len(homepage["suggested_questions"]) >= 6


def test_no_row_returns_zero():
    db = _make_db(None)
    assert backfill_navigation_entries(db) == 0
    db.commit.assert_not_called()


def test_malformed_payload_returns_zero():
    row = _make_row({"navigation": "not_a_dict"})
    db = _make_db(row)
    assert backfill_navigation_entries(db) == 0
    db.commit.assert_not_called()


def test_seeds_missing_entries_into_their_declared_bucket():
    """A legacy row that predates all three entries gets each one seeded into the bucket
    its whitelist entry declares — projects into the user menu, automation/sites into the
    sidebar — each next to its anchor."""
    row = _make_row(_nav(
        sidebar=["ability_center", "my_space"],
        menu=["settings", "app_center", "lab"],
    ))
    db = _make_db(row)
    changed = backfill_navigation_entries(db)
    # 3 entries × (list + title + subtitle) = 9 fields
    assert changed == 9
    nav = row.payload["navigation"]
    # projects → menu_items, right after its 'app_center' anchor
    assert nav["menu_items"] == ["settings", "app_center", "projects", "lab"]
    # automation after 'ability_center', then sites after 'automation'
    assert nav["sidebar_items"] == ["ability_center", "automation", "sites", "my_space"]
    assert nav["panel_titles"]["automation"] == "定时任务"
    assert nav["panel_subtitles"]["sites"].startswith("在对话里描述需求")
    db.commit.assert_called_once()


def test_idempotent_when_already_present():
    row = _make_row(_nav(
        sidebar=["ability_center", "automation", "sites", "my_space"],
        menu=["settings", "app_center", "projects", "lab"],
        titles={**_ALL_TITLES, "projects": "Custom Title"},
        subtitles={**_ALL_SUBTITLES, "projects": "Custom Sub"},
    ))
    db = _make_db(row)
    assert backfill_navigation_entries(db) == 0
    db.commit.assert_not_called()
    # Never overwrite operator-customised copy
    assert row.payload["navigation"]["panel_titles"]["projects"] == "Custom Title"


def test_anchor_missing_falls_back_to_append():
    """'automation' anchors after 'ability_center'; with the anchor gone it appends."""
    row = _make_row(_nav(
        sidebar=["my_space"],  # no 'ability_center' anchor
        menu=["settings", "app_center", "projects", "lab"],
        titles=dict(_ALL_TITLES),
        subtitles=dict(_ALL_SUBTITLES),
    ))
    db = _make_db(row)
    changed = backfill_navigation_entries(db)
    # only automation + sites lists mutate (titles/subtitles already present)
    assert changed == 2
    # automation appended, then sites lands after its 'automation' anchor
    assert row.payload["navigation"]["sidebar_items"] == ["my_space", "automation", "sites"]


def test_partial_backfill_when_only_list_missing():
    row = _make_row(_nav(
        sidebar=["ability_center", "automation", "sites", "my_space"],
        menu=["settings", "app_center", "lab"],  # projects missing here only
        titles=dict(_ALL_TITLES),
        subtitles=dict(_ALL_SUBTITLES),
    ))
    db = _make_db(row)
    assert backfill_navigation_entries(db) == 1
    nav = row.payload["navigation"]
    assert nav["menu_items"] == ["settings", "app_center", "projects", "lab"]
    assert nav["panel_titles"]["projects"] == "项目"


def test_entry_moved_to_other_bucket_is_left_alone():
    """Regression: an operator who moved 'projects' out of the user menu and into the
    sidebar (or the reverse) must not have it re-seeded — that would resurrect it in the
    bucket the whitelist prefers and leave it duplicated in both on every restart."""
    row = _make_row(_nav(
        sidebar=["ability_center", "automation", "sites", "projects", "my_space"],
        menu=["settings", "app_center", "lab"],  # projects deliberately not here
        titles=dict(_ALL_TITLES),
        subtitles=dict(_ALL_SUBTITLES),
    ))
    db = _make_db(row)
    assert backfill_navigation_entries(db) == 0
    db.commit.assert_not_called()
    nav = row.payload["navigation"]
    assert "projects" not in nav["menu_items"]
    assert nav["sidebar_items"].count("projects") == 1


def test_sidebar_entry_moved_into_menu_is_left_alone():
    """Same protection in the other direction: 'automation' declares bucket=sidebar, but
    an operator may have tucked it into the user menu."""
    row = _make_row(_nav(
        sidebar=["ability_center", "sites", "my_space"],
        menu=["settings", "app_center", "projects", "automation", "lab"],
        titles=dict(_ALL_TITLES),
        subtitles=dict(_ALL_SUBTITLES),
    ))
    db = _make_db(row)
    assert backfill_navigation_entries(db) == 0
    assert "automation" not in row.payload["navigation"]["sidebar_items"]


def test_legacy_ability_center_titles_are_renamed():
    """能力中心统一表述后，存量 DB 里的旧出厂文案要跟着改名，管理员自定义的不动。"""
    titles = dict(_ALL_TITLES, skills="技能库", agents="子智能体", mcp="MCP 工具库", plugins="我的插件")
    subtitles = dict(_ALL_SUBTITLES, agents="选择与启用子智能体，并查看其职责边界与路由提示。")
    row = _make_row(_nav(
        sidebar=["ability_center", "automation", "sites", "my_space"],
        menu=["settings", "app_center", "projects", "lab"],
        titles=titles,
        subtitles=subtitles,
    ))
    db = _make_db(row)
    # skills / agents / mcp titles + agents subtitle
    assert backfill_navigation_entries(db) == 4
    nav = row.payload["navigation"]
    assert nav["panel_titles"]["skills"] == "技能"
    assert nav["panel_titles"]["agents"] == "智能体"
    assert nav["panel_titles"]["mcp"] == "连接器"
    # 管理员改过的标题不属于旧出厂值，保持原样
    assert nav["panel_titles"]["plugins"] == "我的插件"
    assert nav["panel_subtitles"]["agents"] == "选择与启用智能体，并查看其职责边界与路由提示。"


def test_renamed_titles_are_idempotent():
    titles = dict(_ALL_TITLES, skills="技能", agents="智能体", mcp="连接器", plugins="插件")
    row = _make_row(_nav(
        sidebar=["ability_center", "automation", "sites", "my_space"],
        menu=["settings", "app_center", "projects", "lab"],
        titles=titles,
        subtitles=dict(_ALL_SUBTITLES),
    ))
    db = _make_db(row)
    assert backfill_navigation_entries(db) == 0
    db.commit.assert_not_called()
