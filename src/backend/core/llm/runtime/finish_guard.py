"""Apply neutral finish-pin decisions in the parent execution context.

Workspace pins use ContextVars and must be written in the framework task, after
all finish hooks return. A failed pin leaves the guard eligible for retry.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def pin_files(file_ids: list[str]) -> int:
    """Write file_ids directly to the workspace ContextVar, bypassing the
    pin_to_workspace tool + _acting path. Returns the number of pins that actually succeeded.

    Metadata comes from ``core.files.artifacts.store.get_artifact`` — on failure that id is skipped.
    """
    try:
        from core.files.artifacts.store import get_artifact
        from core.llm.runtime import workspace as _ws
    except Exception as exc:  # noqa: BLE001
        logger.warning("[finish_guard] _direct_pin import failed: %s", exc)
        return 0

    count = 0
    for fid in file_ids:
        try:
            art = get_artifact(fid)
            if not art:
                logger.warning("[finish_guard] artifact not found: %s", fid)
                continue
            ok = _ws.pin(
                fid,
                name=art.get("name"),
                mime_type=art.get("mime_type"),
                size=art.get("size") or art.get("size_bytes"),
                url=f"/files/{fid}",
            )
            if ok:
                count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[finish_guard] _direct_pin failed for %s: %s", fid, exc)
    # Only mark_active when at least one pin succeeded — avoid an empty set still
    # calling mark_active, which would leave the workspace in an "active but pinned=[]"
    # state (although downstream currently does not rely on the active flag for
    # gating, this is a more robust invariant).
    if count > 0:
        try:
            from core.llm.runtime import workspace as _ws
            _ws.mark_active()
        except Exception:
            logger.warning("[finish_guard] failed to mark workspace active", exc_info=True)
    return count
