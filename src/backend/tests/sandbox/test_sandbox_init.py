"""sandbox-init: no process of the sandbox user outlives the command that started it.

The sandbox's PID 1 and the subreaper each user command runs under, exercised
the way the container uses them, in a fresh PID namespace.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[4] / "docker" / "sandbox" / "sandbox-init.c"
NAMESPACE = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc"]

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or not shutil.which("cc")
    or not shutil.which("unshare")
    or not SOURCE.exists(),
    reason="needs Linux, a C compiler, unshare and the sandbox image sources",
)


def _running(argv: list[str]) -> bool:
    wanted = "\0".join(argv).encode() + b"\0"
    for entry in Path("/proc").iterdir():
        try:
            if entry.name.isdigit() and (entry / "cmdline").read_bytes() == wanted:
                return True
        except OSError:
            continue
    return False


def test_a_detached_process_lives_while_its_command_runs_and_dies_with_it(tmp_path):
    if subprocess.run([*NAMESPACE, "true"], capture_output=True).returncode != 0:
        pytest.skip("needs unprivileged PID namespaces")
    init = tmp_path / "sandbox-init"
    subprocess.run(["cc", "-O2", "-o", str(init), str(SOURCE)], check=True)
    leftover = ["sleep", str(10_000 + uuid.uuid4().int % 10_000)]
    # Detach the way a daemon does: double fork into a new session.
    command = (
        f"(setsid {shlex.join(leftover)} & echo $! > held); echo $$ > command; "
        "sleep 0.5; cut -d' ' -f4 /proc/$(cat held)/stat > seen && mv seen parent"
    )
    # As in the container: PID 1 runs a command as a subreaper, then becomes the init.
    # Root inside the namespace is the user here, so that is the uid it polices.
    pid1 = subprocess.Popen(
        [*NAMESPACE, "sh", "-c", f'"$0" --subreaper sh -c {shlex.quote(command)}; exec "$0" 0', str(init)],
        cwd=tmp_path,
    )
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "parent").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "parent").read_text().split() == (tmp_path / "command").read_text().split()
        while _running(leftover) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _running(leftover)
        assert pid1.poll() is None
    finally:
        pid1.kill()
        pid1.wait()
