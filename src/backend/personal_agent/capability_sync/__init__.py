"""Capability sync: Hugo's skills and plugins as real files in its workspace.

- /workspace/skills/<id>/: built-in skills (personal_agent/skills/) and the catalog skills in the
  owner's binding; /workspace/skills/.synced.json records which ones the sync wrote.
- /workspace/plugins/<slug>/: each bound plugin (plugin.json + skills/<skill_id>/...);
  /workspace/plugins/.synced.json records them with their install id, skills and MCP ids.
- Anything else in those directories is the agent's own and is never touched.

There is no per-task sync. It runs on events only:
- binding saved (IdentityService.configure): sync(), in the request, so the response carries the status;
- grants changed under a binding (IdentityService.ensure reconciles a revoked skill or plugin): request();
- an admin or market change to any skill or plugin (core's "after_skill_refresh" cache stage):
  request_all(), debounced, for owners with a platform binding;
- built-in skills changed with a deploy: check_builtins(), once per agent per process, at startup
  and when an agent is first touched.

The outcome is config["capability_sync"] = {"status": "ok"|"failed", "synced_at", "error", "skipped"}.
"""
from personal_agent.capability_sync.background import ALL, check_builtins, queue
from personal_agent.capability_sync.runner import sync

__all__ = ["sync", "request", "request_all", "check_builtins", "install", "start"]


def request(user_id):
    """Sync one owner in the background. False when background sync is not running in this process."""
    return queue.add(user_id)


def request_all():
    """Sync every owner with a platform skill or plugin in their binding, in the background."""
    return queue.add(ALL)


def install():
    """Called from the extension's setup(): resync after skill and plugin changes."""
    from core.harness.registry.cache_invalidation import cache_invalidators
    cache_invalidators().register("after_skill_refresh", "personal_agent_capability_sync", request_all)


def start():
    """Called from the extension's lifecycle start: run the background queue, then check every
    agent's built-in skills once. The queue thread is a daemon, so stopping needs nothing: an
    interrupted copy leaves only a hidden staged directory, which the next sync removes."""
    import threading
    queue.start()
    # Not awaited: reading every agent's manifest must not hold up the service start.
    threading.Thread(target=_check_all, name="pa-builtin-skill-check", daemon=True).start()


def _check_all():
    from sqlalchemy import select
    from core.db.engine import SessionLocal
    from personal_agent.models import PersonalAgent
    try:
        with SessionLocal() as db:
            agents = db.execute(select(PersonalAgent.id, PersonalAgent.user_id)).all()
    except Exception:  # noqa: BLE001 - the same check runs when each agent is next touched
        import logging
        logging.getLogger(__name__).warning("personal agent built-in skill check skipped", exc_info=True)
        return
    for agent_id, user_id in agents:
        check_builtins(agent_id, user_id)
