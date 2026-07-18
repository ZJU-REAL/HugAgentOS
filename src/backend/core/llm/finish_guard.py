"""finish_pin_guard helpers: auto-pin the files a model forgot when it "dumbly exits".

Models often finish generating files and then exit directly, forgetting the
``pin_to_workspace`` delivery. ``FinishPinGuardMiddleware`` (core.llm.middlewares),
when a reasoning turn makes no tool calls at all (i.e. the model is about to
finish), calls this module's ``_collect_unpinned`` + ``_direct_pin`` and
**writes to the ContextVar by calling ``workspace.pin()`` directly**, bypassing
the ReAct main loop's _acting mechanism. ``chat_run_executor`` calls
``workspace.get_pinned()`` when wrapping up the stream to pick up this line and
deliver it as an attachment over SSE.

Design trade-offs:
- **Do not inject a reminder** to the model — it has already decided to exit,
  and an injection into context would not survive this turn; the next turn's new
  agent would not see it. The auto-pin side effect itself is the only remedy the
  user perceives.
- **At most one auto-pin per reply turn** (the middleware uses a ``_fired`` flag
  to prevent an infinite loop when all file_ids are garbage); when every pin
  fails, ``_fired`` is not set, leaving it for retry.
- Under batch_mode the middleware skips directly, without disturbing anything.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _collect_unpinned() -> list[str]:
    """From the file_id set already collected by pin_hint, pick those not yet pinned and return a sorted list."""
    try:
        from core.llm import workspace as _ws
        from core.llm.hooks import _get_pin_hint_state
        state = _get_pin_hint_state()
        seen = state.get("seen") or set()
        if not seen:
            return []
        pinned = set(_ws.get_pinned_file_ids())
        return sorted(seen - pinned)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[finish_guard] _collect_unpinned failed: %s", exc)
        return []


def _direct_pin(file_ids: list[str]) -> int:
    """Write file_ids directly to the workspace ContextVar, bypassing the
    pin_to_workspace tool + _acting path. Returns the number of pins that actually succeeded.

    Metadata comes from ``core.artifacts.store.get_artifact`` — on failure that id is skipped.
    """
    try:
        from core.artifacts.store import get_artifact
        from core.llm import workspace as _ws
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
            from core.llm import workspace as _ws
            _ws.mark_active()
        except Exception:
            pass
    return count
