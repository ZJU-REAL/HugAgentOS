"""MCP market items another feature owns (for example one per data source).

The owning feature creates the item and keeps it in step with its own records;
the marketplace still lists it, applies listing and visibility rules and records
installations. What differs for such an item:

- install is delegated to the owner, which writes the server row itself (its
  connection details are platform-fixed and never come from the installer);
- the periodic revalidation skips it (the owner tracks its health);
- the admin cannot rename, re-authenticate, suspend or delete it from the market,
  because those follow the owner's record.

An item is owned when its version's ``listing_notice.managed_by`` names a
registered owner. Owners register at feature setup (``register``).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from sqlalchemy.orm import Session

from core.infra.exceptions import BadRequestError, ResourceNotFoundError


class ManagedItemOwner(Protocol):
    label: str  # shown to admins: where to change this item instead
    # Optional id an agent package uses to bind "all connectors of this family".
    portable_binding: Optional[str]

    def install(self, db: Session, item, version, *, owner_user_id: Optional[str],
                installed_by: Optional[str]) -> Dict[str, Any]:
        """Create or refresh the installed server row; commits."""


_OWNERS: Dict[str, ManagedItemOwner] = {}


def register(managed_by: str, owner: ManagedItemOwner) -> None:
    _OWNERS[managed_by] = owner


def portable_bindings() -> Dict[str, str]:
    """{portable id: owner key} declared by the registered owners."""
    return {owner.portable_binding: key for key, owner in _OWNERS.items()
            if getattr(owner, "portable_binding", None)}


def managed_by_notice(listing_notice) -> Optional[str]:
    value = (listing_notice or {}).get("managed_by") if isinstance(listing_notice, dict) else None
    return str(value) if value else None


def managed_by(version) -> Optional[str]:
    return managed_by_notice(version.listing_notice) if version is not None else None


def owner_for(version) -> Optional[ManagedItemOwner]:
    key = managed_by(version)
    return _OWNERS.get(key) if key else None


def refuse_admin_change(version, action: str) -> None:
    """Raise when a market admin action must happen at the owning feature instead."""
    key = managed_by(version)
    if key is None:
        return
    owner = _OWNERS.get(key)
    where = owner.label if owner else "所属模块"
    raise BadRequestError(message=f"该条目由「{where}」维护，请到那里{action}")


def try_install(db: Session, slug: str, owner_user_id: Optional[str],
                installed_by: Optional[str]) -> Optional[Dict[str, Any]]:
    """Install an owned item through its owner; None when the item is an ordinary one."""
    from features.marketplace import edition as ml
    from features.marketplace import mcp_copies, mcp_versions
    from features.marketplace.models import McpMarketItem

    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if item is None:
        return None
    version = mcp_versions.version_for_item(db, item)
    owner = owner_for(version)
    if owner is None:
        return None
    if item.status != "active":
        raise BadRequestError(message=f"「{item.display_name}」当前已暂停，暂不能安装")
    if owner_user_id is not None:
        if slug in ml.get_disabled_ids(db, ml.KIND_MCP):
            raise ResourceNotFoundError("mcp_market_item", slug)
        ml.ensure_item_visible(db, ml.KIND_MCP, slug, owner_user_id, resource="mcp_market_item")
        if slug in mcp_copies.system_server_ids(db):
            raise BadRequestError(message=f"「{item.display_name}」已由系统统一提供，无需重复安装")
    return owner.install(db, item, version, owner_user_id=owner_user_id, installed_by=installed_by)
