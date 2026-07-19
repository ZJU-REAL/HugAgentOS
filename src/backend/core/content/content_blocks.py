"""Helpers for exporting and importing editable docs content blocks."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

from core.config.settings import settings
from core.db.models import ContentBlock
from sqlalchemy.orm import Session

# Branding source of truth (seam C7): in-code defaults stay neutral; the concrete
# brand (e.g. "HugAgentOS") is injected via env BRAND_PRODUCT_NAME / the deploy-time
# content_blocks DB seed.
_BRAND_NAME = settings.branding.product_name

DOCS_BLOCK_MAP = {
    "updates": "docs_updates",
    "capabilities": "docs_capabilities",
    "prompt_hub": "prompt_hub",
    "page_config": "page_config",
    "app_config": "app_config",
    "homepage_shortcuts": "homepage_shortcuts",
}

# Blocks whose payload is a dict instead of a list (default is list).
DICT_PAYLOAD_BLOCKS = {"page_config", "prompt_versions", "app_config"}

# Prompt versions block — managed separately from DOCS_BLOCK_MAP so a routine
# docs export/import never silently clobbers the system prompt pool. It rides
# its own opt-in snapshot (build_prompt_snapshot / import_prompt_snapshot).
PROMPT_VERSIONS_BLOCK_ID = "prompt_versions"

# Opt-in migration set for prompt content: the system prompt version pool
# (prompt_versions) plus the prompt hub (prompt_hub). Same ContentBlock storage
# and snapshot format as docs blocks, but a separate map keeps the two apart so
# a prompts migration never drags page_config / branding fields along with it.
# Note: prompt_hub also appears in DOCS_BLOCK_MAP — that is intentional and
# harmless (a docs snapshot and a prompts snapshot are imported independently).
PROMPT_BLOCK_MAP = {
    "prompt_versions": PROMPT_VERSIONS_BLOCK_ID,
    "prompt_hub": "prompt_hub",
}

SNAPSHOT_SCHEMA_VERSION = 1


class ContentSnapshotError(ValueError):
    """Raised when a docs content snapshot is invalid."""


DEFAULT_PAGE_CONFIG: dict[str, Any] = {
    "branding": {
        "product_name": _BRAND_NAME,
        "product_subtitle": "AI 智能助手",
        "logo_url": "/home/header.svg",
        "favicon_url": "/icon.png",
        "page_title": _BRAND_NAME,
        "hero_title": f"你好，我是{_BRAND_NAME}",
        "hero_subtitle": "基于 AI 能力的场景化智能工作平台",
        "disclaimer": "本平台生成内容由AI大模型生成，不构成任何建议；涉及业务决策请以权威信息为准。",
    },
    "navigation": {
        "panel_titles": {
            "ability_center": "能力中心",
            "skills": "技能库",
            "agents": "子智能体",
            "mcp": "MCP工具库",
            "kb": "知识库",
            "docs": "更新记录",
            "app_center": "应用中心",
            "projects": "项目",
            "lab": "实验室",
            "settings": "系统设置",
            "my_space": "我的空间",
        },
        "panel_subtitles": {
            "ability_center": "智能体基础能力管理，包含技能库以及MCP工具库",
            "skills": "启用/停用技能，并查看详细介绍、输入输出与示例。",
            "agents": "选择与启用子智能体，并查看其职责边界与路由提示。",
            "mcp": "管理 MCP 工具服务，并查看其作用范围与可靠性影响。",
            "kb": "浏览知识库、查看文档列表，并支持文档内检索。",
            "docs": "查看功能更新、能力中心与平台说明。",
            "app_center": "基于 AI 能力的场景化智能应用",
            "projects": "把对话、文件和指令打包成专属工作空间",
            "lab": "AI 能力实验性应用",
            "settings": "",
            "my_space": "",
        },
        "admin_header": {
            "title": f"{_BRAND_NAME} — 后台管理",
            "subtitle": "后台管理",
        },
        # Unified branding + per-page labels for the admin platform (content
        # management / system config / API docs) — product_name drives the header
        # brand and browser tab title of all three admin pages, replacing the old
        # admin_header.
        "admin_platform": {
            "product_name": _BRAND_NAME,
            "content_label": "内容管理",
            "config_label": "系统配置",
            "apidoc_label": "接口文档",
        },
        "sidebar_items": ["agents", "kb", "app_center", "projects", "my_space"],
        "menu_items": ["settings", "ability_center", "lab"],
    },
    "texts": {
        "input_placeholder": "请输入您的问题…",
        "input_placeholder_agent": "请输入您的问题…",
        "search_placeholder": "搜索对话",
        "btn_new_chat": "新建对话",
        "btn_logout": "退出",
        "history_label": "历史对话",
        "sidebar_empty_state": "暂无对话记录",
        "search_no_results": "无匹配结果",
        "dialog_logout_confirm_title": "确认退出登录？",
        "dialog_logout_confirm_content": "退出登录不会丢失任何数据，重新登录后可继续使用。",
        "dialog_logout_confirm_ok": "退出登录",
        "recommend_banner_text": "",
    },
    "defaults": {
        # Initial mode on every user login / new chat:
        # - chat_mode: 'fast' / 'medium' / 'high' / 'max' (recommended)
        # - thinking_mode: bool (legacy field, fallback compat; true→medium, false→fast)
        "chat_mode": "fast",
        "thinking_mode": False,
    },
    "auth": {
        # Whether the login page shows the "register" entry (tab + register subpage).
        # When off, the login page keeps only login, and backend register submissions
        # are rejected outright (not just hidden on the frontend).
        "allow_register": True,
    },
}


# No external sub-apps are built in by default — the app config is entirely managed
# by admins via the "app config" panel. (Historically two presets, "enterprise
# profile / enterprise survey", were seeded here, which caused deleted apps to be
# re-injected on refresh.)
DEFAULT_APP_CONFIG: dict[str, Any] = {
    "apps": [],
}


def normalize_app_config(payload: Any) -> dict[str, Any]:
    """Normalize app_config payload to the canonical `{"apps": [...]}` shape.

    An empty list (admin deleted all apps) is respected as "no apps" and no longer
    falls back to any default entries.
    """
    if not isinstance(payload, dict):
        return {"apps": list(DEFAULT_APP_CONFIG["apps"])}

    apps_raw = payload.get("apps")
    if isinstance(apps_raw, list):
        apps: list[dict[str, Any]] = []
        for item in apps_raw:
            if not isinstance(item, dict):
                continue
            app_id = str(item.get("id") or "").strip()
            if not app_id:
                continue
            apps.append(
                {
                    "id": app_id,
                    "enabled": bool(item.get("enabled", True)),
                    "name": str(item.get("name") or app_id),
                    "description": str(item.get("description") or ""),
                    "url": str(item.get("url") or ""),
                    "icon": str(item.get("icon") or ""),
                }
            )
        return {"apps": apps}

    return {"apps": list(DEFAULT_APP_CONFIG["apps"])}


DEFAULT_HOMEPAGE_SHORTCUTS: list[dict[str, Any]] = [
    {
        "id": "knowledge",
        "enabled": True,
        "label": "知识检索",
        "icon": "/home/company-research.svg",
        "url": "",
    },
    {
        "id": "portrait",
        "enabled": True,
        "label": "企业画像",
        "icon": "/home/company-profile.svg",
        "url": "",
    },
    {"id": "policy", "enabled": True, "label": "政策对比", "icon": "/home/icon3.svg", "url": ""},
    {"id": "compare", "enabled": True, "label": "材料对比", "icon": "/home/icon1.svg", "url": ""},
    {"id": "data", "enabled": True, "label": "数据分析", "icon": "/home/icon2.svg", "url": ""},
]


def normalize_homepage_shortcuts(payload: Any) -> list[dict[str, Any]]:
    """Normalize homepage shortcut list; fall back to defaults when missing/empty."""
    if not isinstance(payload, list) or not payload:
        return [dict(c) for c in DEFAULT_HOMEPAGE_SHORTCUTS]
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        if not cid:
            continue
        out.append(
            {
                "id": cid,
                "enabled": bool(item.get("enabled", True)),
                "label": str(item.get("label") or cid),
                "icon": str(item.get("icon") or ""),
                "url": str(item.get("url") or ""),
            }
        )
    return out or [dict(c) for c in DEFAULT_HOMEPAGE_SHORTCUTS]


DEFAULT_PROMPT_VERSIONS: dict[str, Any] = {
    "active": {
        "system": "default",
        "code_exec": "default",
        "distillation": "default",
        "plan_mode": "default",
    },
    "versions": [],
}


def _default_payload(alias: str) -> Any:
    if alias == "page_config":
        return DEFAULT_PAGE_CONFIG
    if alias == "app_config":
        return DEFAULT_APP_CONFIG
    if alias == "prompt_versions":
        return DEFAULT_PROMPT_VERSIONS
    if alias == "homepage_shortcuts":
        return [dict(c) for c in DEFAULT_HOMEPAGE_SHORTCUTS]
    return {} if alias in DICT_PAYLOAD_BLOCKS else []


def seed_page_config_if_missing(db: Session) -> bool:
    """Insert default page_config row if it does not yet exist. Returns True if inserted."""
    row = db.query(ContentBlock).filter(ContentBlock.id == "page_config").first()
    if row:
        return False
    db.add(
        ContentBlock(
            id="page_config",
            payload=DEFAULT_PAGE_CONFIG,
            updated_at=datetime.now(timezone.utc),
            updated_by="system_seed",
        )
    )
    db.commit()
    return True


def enforce_ce_branding(db: Session) -> bool:
    """Keep the fixed CE product identity and auth policy consistent.

    ``page_config`` survives image upgrades, so merely changing the code/env
    defaults does not repair an already-running CE instance. CE does not expose
    the content-management console and therefore has no supported per-instance
    branding override or self-service registration; normalize both the small
    set of branding fields and the registration switch on startup. EE
    deployments are never touched.
    """
    if settings.edition.edition != "ce":
        return False

    row = db.query(ContentBlock).filter(ContentBlock.id == "page_config").first()
    if row is None or not isinstance(row.payload, dict):
        return False

    product_name = "HugAgentOS"
    payload = dict(row.payload)
    branding = dict(payload.get("branding") or {})
    navigation = dict(payload.get("navigation") or {})
    auth = dict(payload.get("auth") or {})
    admin_header = dict(navigation.get("admin_header") or {})
    admin_platform = dict(navigation.get("admin_platform") or {})

    desired_branding = {
        "product_name": product_name,
        "product_subtitle": "AI 智能助手",
        "page_title": product_name,
        "hero_title": f"你好，我是 {product_name}",
    }
    changed = any(branding.get(key) != value for key, value in desired_branding.items())
    changed = changed or admin_header.get("title") != f"{product_name} — 后台管理"
    changed = changed or admin_platform.get("product_name") != product_name
    changed = changed or auth.get("allow_register") is not False
    if not changed:
        return False

    branding.update(desired_branding)
    admin_header["title"] = f"{product_name} — 后台管理"
    admin_platform["product_name"] = product_name
    navigation["admin_header"] = admin_header
    navigation["admin_platform"] = admin_platform
    auth["allow_register"] = False
    payload["branding"] = branding
    payload["navigation"] = navigation
    payload["auth"] = auth
    row.payload = payload
    row.updated_at = datetime.now(timezone.utc)
    row.updated_by = "system_seed"
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(row, "payload")
    db.commit()
    return True


# ── Idempotent backfill for navigation entries added after initial deploy ─
# Lesson learned: when the projects MVP shipped, only the frontend defaults and the
# backend DEFAULT_PAGE_CONFIG were changed, but **already existing** page_config rows
# do not upgrade automatically — the frontend's "use DB if present" override
# semantics pinned the old sidebar_items in place. This repeated for every new
# entry. The idempotent seeder is the safety net: on startup, backfill missing nav
# entries from a whitelist.
#
# Design principles:
# - Explicit whitelist; do **not** deep-merge the full defaults — avoids
#   "resurrecting" entries operators deleted on purpose (e.g. someone deliberately
#   turned off app_center) and avoids overwriting branding fields in reverse.
# - Each entry carries an insert_after anchor; if the anchor is absent, degrade to
#   appending at the end.
# - On failure, quietly return False; never block startup.
_NAV_BACKFILL_ENTRIES: list[dict[str, Any]] = [
    {
        "key": "projects",
        "insert_after": "app_center",
        "panel_title": "项目",
        "panel_subtitle": "把对话、文件和指令打包成专属工作空间",
    },
]


def backfill_navigation_entries(db: Session) -> int:
    """Patch existing page_config row to add navigation entries listed in
    ``_NAV_BACKFILL_ENTRIES`` if they are missing.

    Returns the number of *individual fields* mutated (across all entries
    and all three locations: sidebar_items / panel_titles / panel_subtitles).
    Zero means nothing changed (idempotent steady state).
    """
    row = db.query(ContentBlock).filter(ContentBlock.id == "page_config").first()
    if not row:
        return 0  # seed_page_config_if_missing handles fresh installs
    payload = row.payload if isinstance(row.payload, dict) else {}
    nav = payload.get("navigation")
    if not isinstance(nav, dict):
        return 0  # malformed payload — leave it alone, admin UI will surface

    changed = 0
    for entry in _NAV_BACKFILL_ENTRIES:
        key = entry["key"]
        items = nav.get("sidebar_items")
        if isinstance(items, list) and key not in items:
            anchor = entry.get("insert_after")
            new_items = list(items)
            if anchor and anchor in new_items:
                idx = new_items.index(anchor) + 1
                new_items.insert(idx, key)
            else:
                new_items.append(key)
            nav["sidebar_items"] = new_items
            changed += 1

        titles = nav.get("panel_titles")
        if isinstance(titles, dict) and key not in titles and entry.get("panel_title"):
            titles[key] = entry["panel_title"]
            changed += 1

        subtitles = nav.get("panel_subtitles")
        if isinstance(subtitles, dict) and key not in subtitles and entry.get("panel_subtitle"):
            subtitles[key] = entry["panel_subtitle"]
            changed += 1

    if changed:
        payload["navigation"] = nav
        row.payload = payload
        row.updated_at = datetime.now(timezone.utc)
        # Mark updated_by so audit can distinguish automatic backfill vs manual edits
        row.updated_by = "system_seed"
        # SQLAlchemy JSONB does not auto-detect mutation — flag explicitly
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(row, "payload")
        db.commit()
    return changed


def get_branding_info() -> dict[str, str]:
    """Read the branding fields from page_config (product_name + logo_url), falling back to defaults on failure.

    Reused by server-side rendering paths (e.g. the SSO login page); manages its
    own DB session internally.
    """
    defaults = {
        "product_name": DEFAULT_PAGE_CONFIG["branding"]["product_name"],
        "logo_url": DEFAULT_PAGE_CONFIG["branding"]["logo_url"],
    }
    try:
        from core.db.engine import SessionLocal

        db = SessionLocal()
        try:
            row = db.query(ContentBlock).filter(ContentBlock.id == "page_config").first()
            payload = (row.payload if row else {}) or {}
            branding = payload.get("branding", {}) if isinstance(payload, dict) else {}
            return {
                "product_name": branding.get("product_name") or defaults["product_name"],
                "logo_url": branding.get("logo_url") or defaults["logo_url"],
            }
        finally:
            db.close()
    except Exception:
        return defaults


def is_register_allowed() -> bool:
    """Return whether self-service local-account registration is enabled.

    CE is permanently single-account and therefore always returns ``False``.
    Other editions read ``page_config.auth.allow_register`` and keep the legacy
    default of ``True`` when the setting is missing or cannot be read. The value
    gates both the server-rendered UI and registration submissions.
    """
    if settings.edition.edition == "ce":
        return False

    try:
        from core.db.engine import SessionLocal

        db = SessionLocal()
        try:
            row = db.query(ContentBlock).filter(ContentBlock.id == "page_config").first()
            payload = (row.payload if row else {}) or {}
            auth = payload.get("auth", {}) if isinstance(payload, dict) else {}
            val = auth.get("allow_register") if isinstance(auth, dict) else None
            return True if val is None else bool(val)
        finally:
            db.close()
    except Exception:
        return True


def get_admin_platform_info() -> dict[str, str]:
    """Read page_config.navigation.admin_platform (admin platform brand + per-page labels).

    When admin_platform is missing, derive the brand from the legacy
    admin_header.title (shaped like "<brand> — 后台管理") as the product_name
    fallback, then fall back to code defaults — consistent with the frontend
    mergeAdminPlatform normalization. Reused by server-side rendering paths
    (Swagger / ReDoc doc page titles etc.); manages its own DB session internally.
    """
    ap_def = DEFAULT_PAGE_CONFIG["navigation"]["admin_platform"]
    defaults = {
        "product_name": ap_def["product_name"],
        "content_label": ap_def["content_label"],
        "config_label": ap_def["config_label"],
        "apidoc_label": ap_def["apidoc_label"],
    }
    try:
        from core.db.engine import SessionLocal

        db = SessionLocal()
        try:
            row = db.query(ContentBlock).filter(ContentBlock.id == "page_config").first()
            payload = (row.payload if row else {}) or {}
            nav = payload.get("navigation", {}) if isinstance(payload, dict) else {}
            ap = nav.get("admin_platform") if isinstance(nav, dict) else None
            ap = ap if isinstance(ap, dict) else {}
            header = nav.get("admin_header") if isinstance(nav, dict) else None
            derived = ""
            if isinstance(header, dict) and isinstance(header.get("title"), str):
                derived = re.split(r"\s+[—-]\s+", header["title"])[0].strip()

            def pick(key: str, fallback: str) -> str:
                v = ap.get(key)
                return v.strip() if isinstance(v, str) and v.strip() else fallback

            return {
                "product_name": pick("product_name", derived or defaults["product_name"]),
                "content_label": pick("content_label", defaults["content_label"]),
                "config_label": pick("config_label", defaults["config_label"]),
                "apidoc_label": pick("apidoc_label", defaults["apidoc_label"]),
            }
        finally:
            db.close()
    except Exception:
        return defaults


def _serialize_block(row: ContentBlock | None, alias: str | None = None) -> dict[str, Any]:
    return {
        "payload": row.payload if row else _default_payload(alias or ""),
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "updated_by": row.updated_by if row else None,
    }


def build_docs_snapshot(
    db: Session,
    block_map: Mapping[str, str] = DOCS_BLOCK_MAP,
) -> dict[str, Any]:
    """Build a portable JSON snapshot for content blocks.

    Defaults to the docs blocks; pass ``block_map=PROMPT_BLOCK_MAP`` (or use
    ``build_prompt_snapshot``) to snapshot the system prompt pool instead.
    """
    rows = db.query(ContentBlock).filter(ContentBlock.id.in_(list(block_map.values()))).all()
    row_map = {row.id: row for row in rows}

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "blocks": {
            alias: _serialize_block(row_map.get(db_id), alias) for alias, db_id in block_map.items()
        },
    }


def _parse_snapshot_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ContentSnapshotError("updated_at must be an ISO datetime string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContentSnapshotError(f"Invalid datetime: {value}") from exc


def normalize_docs_snapshot(
    snapshot: Mapping[str, Any],
    block_map: Mapping[str, str] = DOCS_BLOCK_MAP,
) -> dict[str, Any]:
    """Validate and normalize an incoming content snapshot."""
    if not isinstance(snapshot, Mapping):
        raise ContentSnapshotError("Snapshot body must be a JSON object")

    schema_version = snapshot.get("schema_version", SNAPSHOT_SCHEMA_VERSION)
    if schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise ContentSnapshotError(
            f"Unsupported schema_version: {schema_version}. Expected {SNAPSHOT_SCHEMA_VERSION}"
        )

    raw_blocks = snapshot.get("blocks")
    if not isinstance(raw_blocks, Mapping):
        raise ContentSnapshotError("Snapshot.blocks must be an object")

    unknown_blocks = set(raw_blocks.keys()) - set(block_map.keys())
    if unknown_blocks:
        raise ContentSnapshotError(
            f"Unknown blocks in snapshot: {', '.join(sorted(unknown_blocks))}"
        )

    normalized_blocks: dict[str, dict[str, Any]] = {}
    for alias in block_map:
        raw_block = raw_blocks.get(alias)
        if raw_block is None:
            continue
        if not isinstance(raw_block, Mapping):
            raise ContentSnapshotError(f"Snapshot block '{alias}' must be an object")

        payload = raw_block.get("payload", _default_payload(alias))
        expected_type = dict if alias in DICT_PAYLOAD_BLOCKS else list
        if not isinstance(payload, expected_type):
            kind = "object" if expected_type is dict else "list"
            raise ContentSnapshotError(f"Snapshot block '{alias}'.payload must be a {kind}")

        normalized_blocks[alias] = {
            "payload": payload,
            "updated_at": _parse_snapshot_datetime(raw_block.get("updated_at")),
            "updated_by": raw_block.get("updated_by"),
        }

    if not normalized_blocks:
        raise ContentSnapshotError("Snapshot does not contain any importable content blocks")

    exported_at = snapshot.get("exported_at")
    if exported_at not in (None, "") and not isinstance(exported_at, str):
        raise ContentSnapshotError("Snapshot.exported_at must be a string")

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "exported_at": exported_at,
        "blocks": normalized_blocks,
    }


def import_docs_snapshot(
    db: Session,
    snapshot: Mapping[str, Any],
    *,
    overwrite: bool = True,
    default_updated_by: str | None = None,
    block_map: Mapping[str, str] = DOCS_BLOCK_MAP,
) -> dict[str, Any]:
    """Import a content snapshot into the current database."""
    normalized = normalize_docs_snapshot(snapshot, block_map=block_map)
    imported: list[str] = []
    skipped: list[str] = []

    for alias, block in normalized["blocks"].items():
        db_id = block_map[alias]
        row = db.query(ContentBlock).filter(ContentBlock.id == db_id).first()
        if row and not overwrite:
            skipped.append(alias)
            continue

        updated_at = block["updated_at"] or datetime.now(timezone.utc)
        updated_by = block["updated_by"] or default_updated_by

        if row:
            row.payload = block["payload"]
            row.updated_at = updated_at
            row.updated_by = updated_by
        else:
            row = ContentBlock(
                id=db_id,
                payload=block["payload"],
                updated_at=updated_at,
                updated_by=updated_by,
            )
            db.add(row)
        imported.append(alias)

    db.commit()

    return {
        "schema_version": normalized["schema_version"],
        "imported": imported,
        "skipped": skipped,
        "count": len(imported),
        "overwrite": overwrite,
    }


# ── System prompt pool snapshot (opt-in migration) ──────────────────────────


def build_prompt_snapshot(db: Session) -> dict[str, Any]:
    """Build a portable snapshot of the system prompt pool (prompt_versions).

    Same format as the docs snapshot, but scoped to PROMPT_BLOCK_MAP so it can
    be migrated across environments independently of docs/page config.
    """
    return build_docs_snapshot(db, block_map=PROMPT_BLOCK_MAP)


def import_prompt_snapshot(
    db: Session,
    snapshot: Mapping[str, Any],
    *,
    overwrite: bool = True,
    default_updated_by: str | None = None,
) -> dict[str, Any]:
    """Import a system prompt pool snapshot (counterpart of build_prompt_snapshot)."""
    return import_docs_snapshot(
        db,
        snapshot,
        overwrite=overwrite,
        default_updated_by=default_updated_by,
        block_map=PROMPT_BLOCK_MAP,
    )
