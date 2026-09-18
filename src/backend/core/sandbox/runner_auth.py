"""The shared secret between the backend and its script-execution sidecar.

The sidecar executes arbitrary commands as the signed-in user. In the Docker
profile it only listens inside the compose network; in the desktop local profile
it listens on loopback, where **every process on the machine can reach it** —
including a command the agent itself just launched inside the OS sandbox, since
loopback stays reachable there. Without a secret, that command can call
``/execute`` again with no confinement attached and walk straight out of the
sandbox it was placed in.

So the backend and the sidecar share one token:

* ``SANDBOX_RUNNER_TOKEN`` in the environment configures it (both processes read
  the same variable, which is how the Docker profile wires it);
* the desktop profile has no shared configuration file to put it in, so the
  backend mints one per launch in :func:`ensure_token` and hands it to the
  sidecar it spawns.

The sidecar rejects any request without it. A sidecar started with no token at
all is only reachable from inside its own private network, which is the Docker
profile's boundary — :func:`ensure_token` makes that case impossible on the
desktop, where there is no such boundary.
"""

from __future__ import annotations

import os
import secrets

ENV_VAR = "SANDBOX_RUNNER_TOKEN"

_token = (os.getenv(ENV_VAR) or "").strip()


def token() -> str:
    """The token this process uses when talking to the sidecar ("" = none)."""
    return _token


def ensure_token() -> str:
    """Return the token, minting one for this launch if none is configured.

    Called on the path that spawns the sidecar itself: the value returned here
    is what goes into the child's environment, so both ends agree without any
    on-disk state.
    """
    global _token
    if not _token:
        _token = secrets.token_urlsafe(32)
    return _token


def auth_headers() -> dict[str, str]:
    """Authorization header for sidecar calls, empty when no token is in use."""
    return {"Authorization": f"Bearer {_token}"} if _token else {}


__all__ = ["ENV_VAR", "auth_headers", "ensure_token", "token"]
