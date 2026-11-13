"""Syncs that do not block the request that caused them: an admin or market change, a grant that
went away, built-in skills that changed with a deploy.

One daemon thread per process drains a queue of owners. Requests that arrive while it waits are
merged (a plugin install refreshes the skill caches several times), and one sweep reads each skill's
files once for all the agents it syncs. Nothing runs until install() starts the queue, so a process
without the Personal Agent extension, or a test that never installs it, never syncs in the background.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.0
ALL = object()


class Queue:
    def __init__(self, debounce=DEBOUNCE_SECONDS):
        self.debounce = debounce
        self.cond = threading.Condition()
        self.pending, self.everyone = set(), False
        self.started = False
        self.busy = False

    def start(self):
        with self.cond:
            if self.started: return
            self.started = True
        threading.Thread(target=self._run, name="pa-capability-sync", daemon=True).start()

    def add(self, user_id):
        """user_id, or ALL for every owner whose binding names a platform skill or plugin."""
        with self.cond:
            if not self.started: return False
            if user_id is ALL: self.everyone = True
            else: self.pending.add(str(user_id))
            self.cond.notify()
            return True

    def idle(self, timeout=10.0):
        """For tests: wait until nothing is queued or running."""
        with self.cond:
            return self.cond.wait_for(lambda: not (self.pending or self.everyone or self.busy), timeout)

    def _run(self):
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.pending or self.everyone)
                self.busy = True
            # Let a burst of requests land before reading the catalog once for all of them.
            time.sleep(self.debounce)
            with self.cond:
                owners, everyone = self.pending, self.everyone
                self.pending, self.everyone = set(), False
            try:
                run(owners, everyone)
            except Exception:  # noqa: BLE001 - the thread outlives one bad sweep
                logger.exception("personal agent capability sync sweep failed")
            finally:
                with self.cond:
                    self.busy = False
                    self.cond.notify_all()


def run(owners, everyone=False):
    """Sync the given owners (and with everyone, every owner with a platform binding), each in its
    own session. One failing agent does not stop the others."""
    from core.db.engine import SessionLocal
    from personal_agent.capability_sync.runner import sync
    if everyone:
        with SessionLocal() as db:
            owners = set(owners) | bound_owners(db)
    sources = {}
    for user_id in sorted(owners):
        try:
            with SessionLocal() as db:
                sync(db, user_id, sources=sources)
        except Exception:  # noqa: BLE001 - recorded per agent; the sweep goes on
            logger.exception("personal agent capability sync failed user=%s", user_id)


def bound_owners(db):
    """Owners whose binding, or whose workspace from the last sync, holds a platform skill or plugin:
    an admin change can only touch those. Built-in changes are found per agent by check_builtins."""
    from sqlalchemy import select
    from personal_agent.models import PersonalAgent
    out = set()
    for user_id, config in db.execute(select(PersonalAgent.user_id, PersonalAgent.config)):
        config = config or {}
        chosen = config.get("capability_selection") or {}
        if (chosen.get("skill_ids") or chosen.get("plugin_ids") or config.get("skill_ids") or config.get("plugins")):
            out.add(user_id)
    return out


queue = Queue()
_checked, _checked_lock = set(), threading.Lock()


def check_builtins(agent_id, user_id):
    """Once per agent per process (a deploy restarts the process): queue a sync when the built-in
    skills in the workspace manifest are not the ones this release ships, including a workspace
    never synced at all. Cheap: one small file read, never per task."""
    with _checked_lock:
        if agent_id in _checked or not queue.started: return False
        _checked.add(agent_id)
    import os
    from personal_agent.capability_sync import plan, store
    from personal_agent.workspace import files
    try: root = files.local_workspace(agent_id)
    except files.WorkspaceUnavailable: return False
    try: fd = files._open_dir(root, [store.SKILLS])
    except OSError: return queue.add(user_id)  # never synced
    try: manifest = store.read_manifest(fd, store.SKILLS)
    finally: os.close(fd)
    synced = {name: entry.get("hash") for name, entry in manifest.items() if entry.get("source") == "builtin"}
    return queue.add(user_id) if synced != plan.builtin_hashes() else False
