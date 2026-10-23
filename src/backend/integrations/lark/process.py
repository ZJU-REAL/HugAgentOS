"""Lark process responsibilities."""
from __future__ import annotations
import asyncio
import os
from typing import Dict, List, Optional, Tuple
from core.infra.proc import no_window_kwargs
import logging
logger = logging.getLogger(__name__)
from integrations.lark import state as lark_state

def lark_env(user_id: str) -> Dict[str, str]:
    """Environment for running lark-cli subprocesses on the backend: per-user isolated ``HOME`` (the root of credential isolation).

    LARK_APP_ID/SECRET are **not** injected (with them in the env, lark-cli
    defaults to the tenant/bot token flow, breaking --as user); the app
    config lands in HOME via config init. OPENCLAW_HOME/HERMES_HOME are
    stripped — otherwise ``config init`` decides it is in an Agent context
    and refuses to run."""
    from integrations.lark.storage import lark_home_dir

    home = lark_home_dir(user_id)
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] mkdir home %s failed: %s", home, exc)
    env = dict(os.environ)
    env["HOME"] = str(home)
    for k in ("OPENCLAW_HOME", "HERMES_HOME", "LARK_APP_ID", "LARK_APP_SECRET"):
        env.pop(k, None)
    return env


async def run_lark(
    user_id: str, args: List[str], timeout: int = 40, stdin_data: Optional[str] = None,
) -> Tuple[str, str, int]:
    """Run one lark-cli subcommand on the backend as this user; returns (stdout, stderr, exit_code)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            lark_state.LARK_BIN, *args,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=lark_env(user_id),
            **no_window_kwargs(),
        )
    except FileNotFoundError:
        return "", "lark-cli binary not found on backend", 127
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode("utf-8") if stdin_data is not None else None),
            timeout=timeout,
        )
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return "", f"lark-cli {' '.join(args[:2])} timeout", 124
