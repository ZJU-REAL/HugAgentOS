"""Progressive plugin runtime: activation history."""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

_ACTIVATED_KEY = "activated_plugins"


from core.plugins.runtime import sticky_selection as plugin_sticky_selection


def load_activated_plugin_slugs(chat_id: Optional[str]) -> List[str]:
    """Read sticky activation tokens (installation ids; legacy slugs accepted).

    The historical function name is kept because it is internal and widely
    referenced, but newly written values are exact ``InstalledPlugin.install_id``
    strings. Existing chat rows containing slugs remain readable so an
    already-loaded plugin does not disappear mid-conversation after upgrade.
    """
    if not chat_id:
        return []
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatSession

        with SessionLocal() as db:
            row = db.query(ChatSession.extra_data).filter(ChatSession.chat_id == chat_id).first()
            if not row:
                return []
            data = row[0] or {}
            slugs = data.get(_ACTIVATED_KEY) or []
            return [str(s) for s in slugs if isinstance(s, str) and s.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] activated list read failed: %s", exc)
        return []


def record_plugin_activation(
    chat_id: Optional[str], install_ids: Sequence[str], *, user_id: Optional[str] = None
) -> None:
    """Append exact installation ids to the chat's sticky list (idempotent)."""
    if not chat_id or not install_ids:
        return
    if user_id:
        scoped = plugin_sticky_selection._cloud_sticky_selection(
            install_ids, user_id=user_id, allow_aliases=True
        )
        aliases = {
            alias: row.install_id
            for row in scoped
            for alias in (row.install_id, row.key, row.payload.get("cloud_install_id"))
        }
        install_ids = [aliases.get(item, item) for item in install_ids]
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatSession
        from sqlalchemy.orm.attributes import flag_modified

        with SessionLocal() as db:
            row = db.query(ChatSession).filter(ChatSession.chat_id == chat_id).first()
            if row is None:
                return
            data = dict(row.extra_data or {})
            current = [s for s in (data.get(_ACTIVATED_KEY) or []) if isinstance(s, str)]
            added = False
            for install_id in install_ids:
                if install_id and install_id not in current:
                    current.append(install_id)
                    added = True
            if not added:
                return
            data[_ACTIVATED_KEY] = current
            row.extra_data = data
            flag_modified(row, "extra_data")
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] activation persist failed: %s", exc)
