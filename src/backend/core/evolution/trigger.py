"""When to run an evolution cycle (automatic triggering).

The obvious design — run a cycle after every conversation — is wrong for three
concrete reasons, and each one shapes what is here instead:

1. **It usually cannot produce anything.**  A pattern needs ``MIN_SUPPORT``
   similar episodes before it can be distilled at all.  Firing after turns 1
   through 4 does the full scan-and-attribute pass and provably finds nothing.
2. **It costs O(corpus) per turn.**  Discovery reads back hundreds of episodes
   and their events.  Paying that on every turn to learn nothing is the kind of
   background load that only shows up as unexplained database pressure.
3. **It contradicts the design's own stability rule.**  Skills sit on an
   hours-to-days cadence precisely so the system does not become non-stationary;
   a per-turn learner is the "joint jitter" the multi-timescale layer exists to
   prevent.

So the trigger is **threshold plus debounce**: after an episode lands, ask a
cheap question — has enough *new* evidence accumulated since the last cycle, and
has the debounce elapsed? Almost always the answer is no and the cost is one
indexed count. When it is yes, a cycle runs in the background, off the response
path.

Note the debounce here governs *candidate generation*, which is harmless — a
candidate is a proposal. Activation stays on the far slower skill-layer cooldown
in ``timescales``; the two are deliberately different numbers because they carry
different risk.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from core.db.engine import SessionLocal

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# Automatic triggering is on by default; the thresholds below keep it cheap.
AUTO_TRIGGER_ENABLED = _env_bool("EVOLUTION_AUTO_TRIGGER", True)

# Below MIN_SUPPORT new episodes a cycle provably cannot find a new pattern, so
# this is not a tuning knob so much as the point beneath which running is waste.
MIN_NEW_EPISODES = _env_int("EVOLUTION_TRIGGER_MIN_EPISODES", 5)

# Stops a burst of conversations from queueing a cycle per turn.
DEBOUNCE_SECONDS = _env_int("EVOLUTION_TRIGGER_DEBOUNCE_S", 600)

_state_lock = threading.Lock()
# Per-user debounce, so a busy person cannot crowd out everyone else's cycle.
_personal_last_run: Dict[str, datetime] = {}
_last_cycle_at: Optional[datetime] = None
_last_cycle_episode_count: int = 0
_running = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _episode_count() -> int:
    """Cheap indexed count of mineable episodes."""
    from core.db.models.evolution import EvolutionEpisode
    from core.evolution.user_settings import PRIVACY_PRIVATE

    with SessionLocal() as db:
        return (
            db.query(EvolutionEpisode)
            .filter(EvolutionEpisode.privacy_class != PRIVACY_PRIVATE)
            .count()
        )


def should_trigger(*, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Whether a cycle is worth running right now.

    Returns ``(decision, reason)`` — the reason is kept so an operator asking
    "why has it not run?" gets an answer instead of silence.
    """
    if not AUTO_TRIGGER_ENABLED:
        return False, "auto_trigger_disabled"

    with _state_lock:
        if _running:
            return False, "cycle_already_running"
        last_at, last_count = _last_cycle_at, _last_cycle_episode_count

    moment = now or _now()
    if last_at is not None:
        elapsed = (moment - last_at).total_seconds()
        if elapsed < DEBOUNCE_SECONDS:
            return False, f"debounce:{int(DEBOUNCE_SECONDS - elapsed)}s"

    try:
        total = _episode_count()
    except Exception as exc:
        logger.debug("[evo-trigger] episode count failed: %s", exc)
        return False, "count_unavailable"

    new_since = total - last_count
    if new_since < MIN_NEW_EPISODES:
        # Not a threshold to tune: fewer than this cannot form a pattern.
        return False, f"insufficient_new_evidence:{new_since}/{MIN_NEW_EPISODES}"

    return True, f"ready:{new_since}_new_episodes"


def _mark_started(total: int, *, now: Optional[datetime] = None) -> None:
    """Record that a cycle began.

    Takes the clock as a parameter so the debounce can be exercised without
    sleeping — a debounce tested only against the real clock is a debounce
    nobody tests.
    """
    global _last_cycle_at, _last_cycle_episode_count, _running
    with _state_lock:
        _running = True
        _last_cycle_at = now or _now()
        _last_cycle_episode_count = total


def _mark_finished() -> None:
    global _running
    with _state_lock:
        _running = False


def run_cycle_if_due(*, limit: int = 400) -> Optional[Dict[str, Any]]:
    """Run a cycle when the trigger says so. Returns the report, or ``None``.

    Synchronous and safe to call from a background task. Never raises: this runs
    after the user already has their answer, and a failed cycle must leave the
    product untouched.
    """
    due, reason = should_trigger()
    if not due:
        logger.debug("[evo-trigger] skipped: %s", reason)
        return None

    try:
        total = _episode_count()
    except Exception:
        total = _last_cycle_episode_count

    _mark_started(total)
    try:
        from core.evolution.loop import run_evolution_cycle

        report = run_evolution_cycle(limit=limit)
        logger.info(
            "[evo-trigger] cycle ran: patterns=%s candidates=%s no_update=%s",
            report.get("patterns"),
            len(report.get("candidates") or []),
            report.get("no_update"),
        )
        return report
    except Exception as exc:
        logger.warning("[evo-trigger] cycle failed: %s", exc)
        return None
    finally:
        _mark_finished()


def schedule_personal(user_id: str) -> bool:
    """Run this user's own cycle after their episode landed.

    Personal evolution is what every edition gets, so this is the path that
    matters for most deployments. It is gated by the same debounce, but scoped
    per user so one busy person cannot starve everyone else's cycle.
    """
    if not user_id or not AUTO_TRIGGER_ENABLED:
        return False

    moment = _now()
    with _state_lock:
        last = _personal_last_run.get(user_id)
        if last is not None and (moment - last).total_seconds() < DEBOUNCE_SECONDS:
            return False
        _personal_last_run[user_id] = moment

    def _run() -> None:
        try:
            from core.evolution.personal import run_personal_cycle

            report = run_personal_cycle(user_id)
            if report.get("candidates"):
                logger.info(
                    "[evo-trigger] personal cycle for %s produced %d candidate(s)",
                    user_id,
                    len(report["candidates"]),
                )
        except Exception as exc:
            logger.warning("[evo-trigger] personal cycle failed for %s: %s", user_id, exc)

    try:
        import asyncio

        asyncio.get_running_loop().run_in_executor(None, _run)
    except RuntimeError:
        _run()
    return True


def schedule_if_due(*, limit: int = 400) -> bool:
    """Fire-and-forget entry point for the post-response path.

    Returns whether a cycle was scheduled. The cheap check runs inline so the
    common case — nothing to do — costs one count and no task at all.
    """
    due, reason = should_trigger()
    if not due:
        logger.debug("[evo-trigger] not scheduling: %s", reason)
        return False

    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop (a script, a worker): run it here rather than dropping it.
        run_cycle_if_due(limit=limit)
        return True

    loop.run_in_executor(None, lambda: run_cycle_if_due(limit=limit))
    return True


def status() -> Dict[str, Any]:
    """Current trigger state, for the console and for answering "why not?"."""
    due, reason = should_trigger()
    with _state_lock:
        last_at, last_count = _last_cycle_at, _last_cycle_episode_count
    return {
        "enabled": AUTO_TRIGGER_ENABLED,
        "due": due,
        "reason": reason,
        "min_new_episodes": MIN_NEW_EPISODES,
        "debounce_seconds": DEBOUNCE_SECONDS,
        "last_cycle_at": last_at.isoformat() if last_at else None,
        "episodes_at_last_cycle": last_count,
    }


def reset_for_tests() -> None:
    global _last_cycle_at, _last_cycle_episode_count, _running
    with _state_lock:
        _last_cycle_at = None
        _last_cycle_episode_count = 0
        _running = False
        _personal_last_run.clear()
