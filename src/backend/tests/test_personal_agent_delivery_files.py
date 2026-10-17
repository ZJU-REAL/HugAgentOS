"""Exercise the real non-root workspace reader against symlinks, FIFOs and size limits."""
import asyncio
import base64
import shlex
import sys
from types import SimpleNamespace

import pytest

from core.sandbox.errors import SandboxError
from personal_agent.computer.sandbox import PersonalSandboxProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["regular", "symlink", "directory_symlink", "fifo", "oversized", "outside"])
async def test_delivery_workspace_reader_does_not_escape_or_block(tmp_path, kind):
    root = tmp_path / "workspace"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"private desktop credential")
    (root / "good.txt").write_bytes(b"public task file")
    (root / "link").symlink_to(secret)
    (root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    import os
    os.mkfifo(root / "fifo")
    if kind == "oversized": (root / "big").write_bytes(b"x" * (20 * 1024 * 1024 + 1))

    async def run(command, opts):
        assert opts.uid == 1000 and opts.gid == 1000
        parts = shlex.split(command)
        position = parts.index("-c")
        # Model the sandbox's /workspace mount in an isolated subprocess; execute
        # the production reader unchanged except for the physical mount location.
        code = parts[position + 1].replace("'/workspace'", repr(str(root)))
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, *parts[position + 2:],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        return SimpleNamespace(error=bool(process.returncode), logs=SimpleNamespace(
            stdout=[SimpleNamespace(text=stdout.decode())], stderr=[SimpleNamespace(text=stderr.decode())]))

    provider = object.__new__(PersonalSandboxProvider)
    async def session(ident, uid):
        assert ident == "box" and uid == "owner"
        return SimpleNamespace(sandbox=SimpleNamespace(commands=SimpleNamespace(run=run)))
    provider._get_or_create_session = session
    paths = {"regular": "/workspace/good.txt", "symlink": "/workspace/link",
             "directory_symlink": "/workspace/linked-dir/secret", "fifo": "/workspace/fifo",
             "oversized": "/workspace/big", "outside": str(secret)}
    if kind == "regular":
        assert await provider.get_file("box", paths[kind], user_id="owner") == b"public task file"
    else:
        with pytest.raises((SandboxError, ValueError)):
            await provider.get_file("box", paths[kind], user_id="owner")
