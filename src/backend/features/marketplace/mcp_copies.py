"""System and private copies of the same marketplace MCP.

Both copies expose the same tool names, so a session loads one. Installed
servers carry ``market_item`` (the item they serve; ``market_slug`` only marks
installs and blocks republishing) and, for private copies, ``credential_source``
(installer / admin / none). A private copy on the user's own credentials stays
in use; any other gives way while the system copy is installed and enabled.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.capabilities.mcp.management import decrypt_mcp_headers
from core.db.models import AdminMcpServer
from features.marketplace import mcp_credentials, mcp_validation
from features.marketplace.models import McpMarketInstallation, McpMarketItem, McpMarketVersion

_LIVE_STATUSES = ("active", "suspended")


def credential_source(
    db: Session,
    item: McpMarketItem,
    version: McpMarketVersion,
    *,
    auth_method: Optional[str],
    headers: Dict[str, str],
) -> str:
    """Where a private installation's credentials come from: installer / admin / none."""
    auth_config = mcp_validation.normalize_auth_config(version.auth_config, list(version.auth_schema or []))
    method_id = str(auth_method or auth_config["default_method"])
    method = next((m for m in auth_config["methods"] if m["id"] == method_id), None)
    if method is None or method["type"] == "none":
        return "none"
    managed = mcp_credentials.admin_managed_credential_headers(db, item, version, auth_method=method_id)
    if managed and all(headers.get(key) == value for key, value in managed.items()):
        return "admin"
    return "installer"


def system_server_ids(db: Session) -> Dict[str, str]:
    """{slug: server_id} of the enabled system copies."""
    rows = (
        db.query(McpMarketInstallation.slug, McpMarketInstallation.server_id)
        .join(AdminMcpServer, AdminMcpServer.server_id == McpMarketInstallation.server_id)
        .filter(
            McpMarketInstallation.owner_user_id.is_(None),
            McpMarketInstallation.status == "active",
            AdminMcpServer.is_enabled.is_(True),
        )
        .all()
    )
    return {str(slug): str(server_id) for slug, server_id in rows}


def is_superseded(server: AdminMcpServer, system_ids: Dict[str, str]) -> bool:
    """Whether a private copy gives way to the system copy at runtime."""
    extra = server.extra_config or {}
    slug = extra.get("market_item")
    return bool(slug in system_ids and extra.get("credential_source") in {"admin", "none"})


def reconcile_market_copies(db: Session, slug: Optional[str] = None) -> Dict[str, Dict[str, List[str]]]:
    """Stamp ``market_item`` / ``credential_source`` on installed servers and report the overlap.

    Idempotent. Servers linked as an installation without going through the
    install flow (admin-published servers, rows from before these stamps)
    are backfilled here. Returns, per slug that has an enabled system copy,
    the users whose private copy now gives way (``replaced``) and the users
    who keep their own-credential copy (``kept``).
    """
    query = (
        db.query(McpMarketInstallation, AdminMcpServer)
        .join(AdminMcpServer, AdminMcpServer.server_id == McpMarketInstallation.server_id)
        .filter(McpMarketInstallation.status.in_(_LIVE_STATUSES))
    )
    if slug is not None:
        query = query.filter(McpMarketInstallation.slug == slug)
    items: Dict[str, Optional[McpMarketItem]] = {}
    changed = False
    for install, server in query.all():
        extra = dict(server.extra_config or {})
        updates: Dict[str, Any] = {}
        if extra.get("market_item") != install.slug:
            updates["market_item"] = install.slug
        if install.owner_user_id is not None and not extra.get("credential_source"):
            if install.slug not in items:
                items[install.slug] = db.query(McpMarketItem).filter(McpMarketItem.slug == install.slug).first()
            item = items[install.slug]
            version = db.get(McpMarketVersion, install.version_id)
            if item is not None and version is not None:
                updates["credential_source"] = credential_source(
                    db,
                    item,
                    version,
                    auth_method=extra.get("auth_method"),
                    headers=decrypt_mcp_headers(server.headers),
                )
        if updates:
            server.extra_config = {**extra, **updates}
            changed = True
    if changed:
        db.commit()

    system_ids = system_server_ids(db)
    report: Dict[str, Dict[str, List[str]]] = {}
    private = (
        db.query(McpMarketInstallation, AdminMcpServer)
        .join(AdminMcpServer, AdminMcpServer.server_id == McpMarketInstallation.server_id)
        .filter(
            McpMarketInstallation.owner_user_id.isnot(None),
            McpMarketInstallation.status.in_(_LIVE_STATUSES),
            McpMarketInstallation.slug.in_(list(system_ids)),
        )
    )
    if slug is not None:
        private = private.filter(McpMarketInstallation.slug == slug)
    for install, server in private.order_by(McpMarketInstallation.owner_user_id).all():
        entry = report.setdefault(install.slug, {"replaced": [], "kept": []})
        entry["replaced" if is_superseded(server, system_ids) else "kept"].append(str(install.owner_user_id))
    return report
