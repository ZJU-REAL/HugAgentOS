"""Clear ordinary detached workload processes before same-box evaluation.

The baseline lives in backend lease state, never a writable sandbox marker.
This guard handles background jobs, not a malicious root replacing the sandbox
interpreter or injecting into execd; that threat needs a host-side supervisor.
"""
from __future__ import annotations

import asyncio
import json
import shlex
from datetime import timedelta

from .errors import SandboxError

PROGRAM = r'''
import json
import os
import signal
import sys
import time

def scan():
    processes = {}
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open('/proc/' + name + '/stat') as stream:
                fields = stream.read().rsplit(')', 1)[1].split()
            processes[int(name)] = (fields[19], int(fields[1]), fields[0])
        except (FileNotFoundError, ProcessLookupError):
            continue
    return processes

def sweep(baseline, timeout=3):
    end = time.monotonic() + timeout
    clean_scans = 0
    while time.monotonic() < end:
        processes = scan()
        if processes.get(1, (None,))[0] != baseline.get('1'):
            raise RuntimeError('container identity changed')
        protected = {int(pid) for pid, started in baseline.items()
                     if processes.get(int(pid), (None,))[0] == started}
        current = os.getpid()
        visited = set()
        while current > 0:
            if current in visited or current not in processes:
                raise RuntimeError('control ancestry is unavailable')
            visited.add(current)
            protected.add(current)
            current = processes[current][1]
        remaining = [pid for pid, (_, _, state) in processes.items()
                     if pid not in protected and state not in ('Z', 'X')]
        if not remaining:
            clean_scans += 1
            if clean_scans >= 3:
                return
        else:
            clean_scans = 0
            for pid in remaining:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(0.05)
    raise RuntimeError('workload processes did not quiesce')

if __name__ == '__main__':
    if sys.argv[1] == 'capture':
        print(json.dumps({str(pid): started for pid, (started, _, state)
                          in scan().items() if state not in ('Z', 'X')}))
    elif sys.argv[1] == 'clear':
        sweep(json.loads(sys.argv[2]))
        print(json.dumps({'clean': True}))
    else:
        raise RuntimeError('invalid control operation')
'''


async def _run(sandbox, mode, baseline=None):
    from opensandbox.models.execd import RunCommandOpts
    argv = ["python3", "-I", "-S", "-c", PROGRAM, mode]
    if baseline is not None:
        argv.append(json.dumps(baseline))
    try:
        async with asyncio.timeout(15):
            result = await sandbox.commands.run(
                shlex.join(argv),
                opts=RunCommandOpts(uid=0, gid=0, timeout=timedelta(seconds=10),
                                    working_directory="/"),
            )
        if result.exit_code != 0:
            raise SandboxError("Evaluation process guard failed")
        text = "".join(item.text for item in result.logs.stdout)
        if len(text) > 1024 * 1024:
            raise SandboxError("Evaluation process guard response is too large")
        return json.loads(text)
    except SandboxError:
        raise
    except Exception as exc:
        raise SandboxError("Evaluation process guard failed: " + type(exc).__name__) from None


def _validate(baseline):
    if (not isinstance(baseline, dict) or "1" not in baseline
            or not baseline or len(baseline) > 10000
            or any(not isinstance(pid, str) or not pid.isdigit() or int(pid) <= 0
                   or not isinstance(started, str) or not started.isdigit()
                   for pid, started in baseline.items())):
        raise SandboxError("Evaluation process baseline is missing or invalid")


async def capture_baseline(sandbox) -> dict[str, str]:
    baseline = await _run(sandbox, "capture")
    _validate(baseline)
    return baseline


async def clear_processes(sandbox, protected_processes: dict[str, str]) -> None:
    _validate(protected_processes)
    result = await _run(sandbox, "clear", protected_processes)
    if not isinstance(result, dict) or result.get("clean") is not True:
        raise SandboxError("Evaluation process cleanup is unconfirmed")
