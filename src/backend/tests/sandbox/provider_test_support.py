"""Shared isolated fixtures and provider fakes."""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock
import sys
import types
from types import SimpleNamespace


def _reload_settings_and_factory(monkeypatch, **env):
    """Force re-creation of settings + factory so env changes take effect."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # settings.py loads .env by itself: disable it for test isolation, otherwise
    # deployment config like SANDBOX_PROVIDER in the project's .env pollutes unit-test assumptions
    import dotenv

    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {})
    # settings is a frozen dataclass instance; re-import the module to rebuild it
    import importlib
    import core.config.settings as s

    importlib.reload(s)
    import core.sandbox.factory as f

    importlib.reload(f)
    import core.sandbox.providers.script_runner.provider as srp

    importlib.reload(srp)
    return f


def _import_opensandbox_module():
    """Import opensandbox_provider for unit testing (does NOT touch SDK init)."""
    import importlib
    import core.sandbox.providers.opensandbox.provider as osp

    importlib.reload(osp)
    return osp


def _install_fake_e2b(monkeypatch):
    """Inject fake e2b modules so cube_provider can import + run without the SDK."""

    class TimeoutException(Exception):
        pass

    class CommandExitException(Exception):
        def __init__(self, stdout="", stderr="", exit_code=1):
            super().__init__(f"exit {exit_code}")
            self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code

    AsyncSandbox = MagicMock(name="AsyncSandbox")
    eci = types.ModuleType("e2b_code_interpreter")
    eci.AsyncSandbox = AsyncSandbox
    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", eci)
    for n in ("e2b", "e2b.sandbox", "e2b.sandbox.commands"):
        monkeypatch.setitem(sys.modules, n, types.ModuleType(n))
    exc = types.ModuleType("e2b.exceptions")
    exc.TimeoutException = TimeoutException
    monkeypatch.setitem(sys.modules, "e2b.exceptions", exc)
    ch = types.ModuleType("e2b.sandbox.commands.command_handle")
    ch.CommandExitException = CommandExitException
    monkeypatch.setitem(sys.modules, "e2b.sandbox.commands.command_handle", ch)
    return AsyncSandbox, TimeoutException, CommandExitException


def _fake_sbx(sandbox_id="sbx-1", stdout="ok\n", stderr="", exit_code=0, run_exc=None):
    sbx = MagicMock(name="sandbox")
    sbx.sandbox_id = sandbox_id
    sbx.set_timeout = AsyncMock()

    async def run(command, **kwargs):
        if kwargs.get("background"):
            if run_exc is not None and not hasattr(run_exc, "exit_code"):
                raise run_exc
            kwargs["on_stdout"](getattr(run_exc, "stdout", stdout))
            kwargs["on_stderr"](getattr(run_exc, "stderr", stderr))
            return SimpleNamespace(
                wait=(
                    AsyncMock(side_effect=run_exc)
                    if run_exc
                    else AsyncMock(return_value=SimpleNamespace(exit_code=exit_code))
                ),
                kill=AsyncMock(),
            )
        return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code)

    sbx.commands.run = AsyncMock(side_effect=run)
    sbx.files.write = AsyncMock()
    sbx.files.read = AsyncMock(return_value=b"")
    sbx.kill = AsyncMock()
    return sbx


def _reload_cube(monkeypatch, **env):
    env.setdefault("SANDBOX_PROVIDER", "cube")
    env.setdefault("CUBE_TEMPLATE", "tpl-test")
    # Warm pool off by default: keeps existing contract tests' create call counts exact; pool behavior is enabled explicitly in separate tests.
    env.setdefault("CUBE_POOL_MIN_IDLE", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import dotenv

    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {})
    import importlib
    import core.config.settings as s

    importlib.reload(s)
    import core.sandbox.providers.cube.provider as cp

    importlib.reload(cp)
    import core.sandbox.factory as f

    importlib.reload(f)
    return f


def _bash_req(**over):
    from core.sandbox import ProcessRequest

    kw = dict(
        script_content="echo hi",
        script_name="_bash.sh",
        language="bash",
        timeout=30,
        session_id="cube-test",
    )
    kw.update(over)
    return ProcessRequest(**kw)


def _fake_listed(sandbox_id, owner):
    return SimpleNamespace(
        sandbox_id=sandbox_id, metadata={"hugagent-owner": owner}, state="running"
    )


def _fake_paginator(items):
    p = MagicMock(name="paginator")
    p.next_items = AsyncMock(return_value=items)
    p.has_next = False
    return p


def patch_open_settings(monkeypatch, value):
    """Isolate the configuration of the concrete OpenSandbox components under test."""
    from importlib import import_module
    for name in ("creation", "lifecycle", "snapshots", "volumes", "tracking", "reclamation"):
        module = import_module("core.sandbox.providers.opensandbox." + name)
        if hasattr(module, "settings"):
            monkeypatch.setattr(module, "settings", value)
    for owner in ("dingtalk", "lark", "email", "yida"):
        module = import_module(f"integrations.{owner}.mounts")
        monkeypatch.setattr(module, "settings", value)
def install_session_scope(provider):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def use(session_id, user_id=None):
        yield await provider._get_or_create_session(session_id, user_id=user_id)
    provider._session_use = use
