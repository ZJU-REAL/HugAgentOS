"""Per-agent memory state kept in PersonalAgent.config.

    memory_lease    {"owner", "until"}: Dreaming holds it while it writes the wiki; online
                    writes and editor saves are refused until it ends or expires.
    memory_genesis  {"status": "requested" | "running" | "done", "since": ISO time}: the
                    one-off replay of history that builds the first wiki of an existing user.

Callers lock the agent row (SELECT ... FOR UPDATE) before changing either.
"""
import time

LEASE = "memory_lease"
GENESIS = "memory_genesis"
LEASE_SECONDS = 600   # renewed before every model step (loop.STEP_TIMEOUT) and tool call


def _set(agent, key, value):
    config = dict(agent.config or {})
    if value is None: config.pop(key, None)
    else: config[key] = value
    agent.config = config


def busy(agent, now=None):
    lease = (agent.config or {}).get(LEASE) or {}
    return lease.get("until", 0) > (time.time() if now is None else now)


def hold(agent, owner, now=None, seconds=LEASE_SECONDS):
    """Take or renew the lease; False when another owner holds a live one."""
    now = time.time() if now is None else now
    lease = (agent.config or {}).get(LEASE) or {}
    if lease.get("owner") not in (None, owner) and lease.get("until", 0) > now: return False
    _set(agent, LEASE, {"owner": owner, "until": now + seconds})
    return True


def release(agent, owner):
    if ((agent.config or {}).get(LEASE) or {}).get("owner") == owner: _set(agent, LEASE, None)


def genesis(agent):
    return dict((agent.config or {}).get(GENESIS) or {})


def request_genesis(agent, since):
    """Ask the next Dreaming run to replay history from `since` (an ISO time) first."""
    if genesis(agent).get("status") in ("requested", "running", "done"): return False
    _set(agent, GENESIS, {"status": "requested", "since": since})
    return True


def set_genesis(agent, status, **extra):
    _set(agent, GENESIS, {**genesis(agent), "status": status, **extra})
