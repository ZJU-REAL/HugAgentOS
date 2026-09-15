"""How many uvicorn workers this deployment may actually run.

A backend process is single-threaded where it matters: one event loop, one GIL.
Serving from several processes is the only way a multi-core host contributes
more than one core to the API — but it is only correct when nothing the request
path depends on lives *inside* one of those processes.

Three things can live in-process, and each is a hard veto rather than a
preference, because none of them degrades gracefully:

* ``SESSION_STORE=memory`` — a login handled by worker A is invisible to worker
  B, so a browser is randomly signed out on roughly ``1 - 1/N`` of its requests.
* No Redis — the per-run SSE event log falls back to an in-process buffer
  (:mod:`orchestration.run_event_stream`), and so does the mutex behind
  :mod:`core.infra.leader`, so neither the stream nor the leader lease would
  actually cross a process boundary.
* ``DEPLOY_PROFILE=local`` — the no-Docker desktop profile hosts MCP and sandbox
  sidecars as child processes of the backend; N parents would start N copies.

The list is the whole contract: anything the request path keeps in process has to
appear here or be made shareable. The sandbox session table was the case that
proved it — a container-backed provider bound a chat to a container purely in
memory, so consecutive rounds served by different workers each built their own
container, and each worker's idle reaper judged "nobody is using this" from its
own half of the traffic. Neither provider is a veto here because the binding was
moved instead, to :mod:`core.sandbox.session_registry`, which keeps it in the
TTL keyspace of :mod:`core.infra.ephemeral` — Redis wherever there is one, which
is anywhere this module allows more than one worker.

So ``WEB_CONCURRENCY`` is a ceiling, not an instruction, and :func:`resolve` —
not the raw variable — is what anything that cares about the process count must
read. The entrypoint sizes ``--workers`` from it, but it is deliberately not the
only launch path: ``cli.py`` and ``api.app.main`` call ``uvicorn.run`` in
process, where nothing exported the resolved value. Deriving the answer instead
of trusting the environment keeps those paths from believing they are a cluster.
"""

from __future__ import annotations

import os

from core.config.local_mode import local_mode_enabled
from core.config.settings import settings
from core.infra.logging import get_logger
from core.infra.redis import redis_configured

logger = get_logger(__name__)


def requested() -> int:
    """The worker count the operator asked for, via uvicorn's own variable."""
    try:
        return max(1, int(os.getenv("WEB_CONCURRENCY", "1")))
    except ValueError:
        return 1


def vetoes() -> list[str]:
    """Why this deployment cannot serve from more than one process, if it cannot."""
    reasons = []
    if settings.session.store_type == "memory":
        reasons.append("SESSION_STORE=memory (sessions would not be shared between workers)")
    if not redis_configured():
        reasons.append("no REDIS_URL (the run event log and the leader lease stay in-process)")
    if local_mode_enabled():
        reasons.append("DEPLOY_PROFILE=local (sidecars are children of the backend process)")
    return reasons


def resolve() -> int:
    """The number of workers it is safe to start here."""
    count = requested()
    return 1 if count > 1 and vetoes() else count


if __name__ == "__main__":  # invoked by the entrypoint to size `--workers`
    # The clamp is announced here rather than inside ``resolve``: this runs once,
    # at boot, in the one place an operator reads. A deployment that asked for
    # eight workers and silently got one would look like the change did nothing.
    asked, effective = requested(), resolve()
    if effective < asked:
        logger.warning(
            "startup_worker_count_clamped",
            requested=asked,
            effective=effective,
            reasons="; ".join(vetoes()),
        )
    print(effective)
