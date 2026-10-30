"""Reset before same-user reuse: the command the provider sends into the sandbox."""

import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.sandbox.providers.opensandbox import lifecycle
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider


def _provider_and_session(exit_code=0):
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    run = AsyncMock(return_value=SimpleNamespace(exit_code=exit_code))
    sess = SimpleNamespace(sandbox=SimpleNamespace(id="sbx", commands=SimpleNamespace(run=run)))
    return provider, sess, run


def test_reset_script_compiles():
    compile(lifecycle._RESET_SCRIPT, "reset", "exec")


async def test_reset_runs_the_script_with_run_identity_and_kept_names():
    provider, sess, run = _provider_and_session()
    assert await provider._wipe_workspace(sess) is True
    argv = shlex.split(run.await_args.args[0])
    assert argv[1:3] == ["-c", lifecycle._RESET_SCRIPT]
    assert argv[3:] == ["1000", "1000", *provider.workspace_mount_names]


async def test_failed_reset_refuses_reuse():
    provider, sess, _ = _provider_and_session(exit_code=1)
    assert await provider._wipe_workspace(sess) is False
