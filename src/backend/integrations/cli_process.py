"""Run one connector CLI (dws / lark-cli / himalaya) as a backend subprocess.

Shared by the per-user CLI integrations: each caller builds its own per-user
``env`` and passes the binary + args here. The contract every caller relies on:
returns ``(stdout, stderr, exit_code)``; a missing binary is ``127`` and a
timeout is ``124``. On timeout the process is killed and reaped, so no zombie
is left behind.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Sequence, Tuple

from core.infra.proc import no_window_kwargs

__all__ = ["run_cli"]


async def run_cli(
    binary: str,
    args: List[str],
    *,
    env: Dict[str, str],
    timeout: float,
    lead_args: Sequence[str] = (),
    stdin_data: Optional[str] = None,
    label: Optional[str] = None,
) -> Tuple[str, str, int]:
    """Run ``binary *args``; returns (stdout, stderr, exit_code).

    ``lead_args`` go before ``args`` on the command line (e.g. ``--config <path>``)
    but stay out of the timeout message. ``label`` names the CLI in the 127/124
    messages (defaults to ``binary``); the timeout message carries the first two
    ``args``, e.g. ``"dws auth login timeout"``.
    """
    name = label or binary
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *lead_args, *args,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **no_window_kwargs(),
        )
    except FileNotFoundError:
        return "", f"{name} binary not found on backend", 127
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode("utf-8") if stdin_data is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        return "", f"{name} {' '.join(args[:2])} timeout", 124
    return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode or 0
