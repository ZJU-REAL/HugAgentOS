"""Who may install a marketplace item into their own account."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set

from sqlalchemy.orm import Session


def market_install_gate(
    db: Session, owner_user_id: Optional[str], *, caps: Optional[Dict[str, Any]] = None
) -> Callable[[str, str], bool]:
    """Return ``allowed(kind, slug)``: the owner's market bit for ``kind`` and the item's visibility.

    ``kind`` is ``skill`` / ``mcp`` / ``plugin`` / ``agent``. Capabilities are resolved once and
    each kind's hidden set is read once, so checking many items costs a few queries in all.
    Installs with no owner (administrator / global) are always allowed.
    """
    if not owner_user_id:
        return lambda kind, slug: True
    from core.auth.capabilities import MARKET_INSTALL_FLAG, resolve_user_capabilities
    from features.marketplace import edition as ml

    caps = caps if caps is not None else resolve_user_capabilities(db, owner_user_id)
    market_kind = {"skill": ml.KIND_SKILL, "mcp": ml.KIND_MCP, "plugin": ml.KIND_PLUGIN, "agent": ml.KIND_AGENT}
    hidden: Dict[str, Set[str]] = {}

    def allowed(kind: str, slug: str) -> bool:
        if not caps.get(MARKET_INSTALL_FLAG[kind]):
            return False
        if kind not in hidden:
            hidden[kind] = ml.get_hidden_item_ids(db, market_kind[kind], owner_user_id)
        return str(slug) not in hidden[kind]

    return allowed
