"""branding responsibilities."""
from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Any
from core.config.settings import settings
from core.db.models import ContentBlock
from sqlalchemy.orm import Session
from core.files.content import content_blocks_state

def normalize_app_config(payload: Any) -> dict[str, Any]:
    """Normalize app_config payload to the canonical `{"apps": [...]}` shape.

    An empty list (admin deleted all apps) is respected as "no apps" and no longer
    falls back to any default entries.
    """
    if not isinstance(payload, dict):
        return {"apps": list(content_blocks_state.DEFAULT_APP_CONFIG["apps"])}

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

    return {"apps": list(content_blocks_state.DEFAULT_APP_CONFIG["apps"])}


def default_payload(alias: str) -> Any:
    if alias == "page_config":
        return content_blocks_state.DEFAULT_PAGE_CONFIG
    if alias == "app_config":
        return content_blocks_state.DEFAULT_APP_CONFIG
    if alias == "prompt_versions":
        return content_blocks_state.DEFAULT_PROMPT_VERSIONS
    return {} if alias in content_blocks_state.DICT_PAYLOAD_BLOCKS else []


def seed_page_config_if_missing(db: Session) -> bool:
    """Insert default page_config row if it does not yet exist. Returns True if inserted."""
    row = db.query(ContentBlock).filter(ContentBlock.id == "page_config").first()
    if row:
        return False
    db.add(
        ContentBlock(
            id="page_config",
            payload=content_blocks_state.DEFAULT_PAGE_CONFIG,
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

    default_nav = content_blocks_state.DEFAULT_PAGE_CONFIG["navigation"]

    changed = 0
    for entry in content_blocks_state.NAV_BACKFILL_ENTRIES:
        key = entry["key"]
        bucket_field = "menu_items" if entry["bucket"] == "menu" else "sidebar_items"
        items = nav.get(bucket_field)
        # Present in *either* list counts as placed: the operator may have moved the entry
        # between buckets on purpose, and re-seeding it would duplicate it across both.
        already_placed = any(
            key in value
            for field in ("sidebar_items", "menu_items")
            if isinstance(value := nav.get(field), list)
        )
        if isinstance(items, list) and not already_placed:
            anchor = entry.get("insert_after")
            new_items = list(items)
            if anchor and anchor in new_items:
                new_items.insert(new_items.index(anchor) + 1, key)
            else:
                new_items.append(key)
            nav[bucket_field] = new_items
            changed += 1

        # Copy comes from DEFAULT_PAGE_CONFIG so it is never re-typed here.
        for field in ("panel_titles", "panel_subtitles"):
            target = nav.get(field)
            text = default_nav[field].get(key)
            if isinstance(target, dict) and key not in target and text:
                target[key] = text
                changed += 1

    # 旧出厂文案改名（能力中心统一表述），与上面的补齐共用一次提交
    for field, renames in content_blocks_state.LEGACY_PANEL_TEXT_RENAMES.items():
        target = nav.get(field)
        if not isinstance(target, dict):
            continue
        for key, (legacy_values, new_text) in renames.items():
            if target.get(key) in legacy_values:
                target[key] = new_text
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
        "product_name": content_blocks_state.DEFAULT_PAGE_CONFIG["branding"]["product_name"],
        "logo_url": content_blocks_state.DEFAULT_PAGE_CONFIG["branding"]["logo_url"],
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
    ap_def = content_blocks_state.DEFAULT_PAGE_CONFIG["navigation"]["admin_platform"]
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
