"""Sandbox side of a synchronized space: scan the copy and apply a plan.

Both scripts run as root through execd. Inputs and outputs travel as files
under ``/tmp`` instead of argv/stdout, so a large tree is not limited by the
command line or the log stream. The baseline lives under
``/workspace/.hugagent-sync`` (root, 0700), outside every space copy, and
survives the workspace reset that precedes reuse by the same account.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import uuid
from datetime import timedelta

from core.sandbox.workspace.identity import SANDBOX_RUN_GID, SANDBOX_RUN_UID

STATE_DIR_NAME = ".hugagent-sync"
STATE_DIR = "/workspace/" + STATE_DIR_NAME

_SCAN = r"""
import hashlib, json, os, stat, sys
spec = json.load(open(sys.argv[1]))
out = {}
def valid(rel):
    return rel and "\\" not in rel and all(ord(c) >= 32 for c in rel) and \
        all(p not in ("", ".", "..") for p in rel.split("/"))
for item in spec["roots"]:
    root, state = item["root"], item["state"]
    try:
        baseline = json.load(open(state))
    except (OSError, ValueError):
        baseline = None
    known = set((baseline or {}).get("files") or ())
    files, dirs = {}, []
    if os.path.isdir(root) and not os.path.islink(root):
        for base, children, names in os.walk(root, followlinks=False):
            rel_base = os.path.relpath(base, root)
            rel_base = "" if rel_base == "." else rel_base
            for name in list(children):
                rel = rel_base + "/" + name if rel_base else name
                path = os.path.join(base, name)
                if os.path.islink(path) or not valid(rel):
                    children.remove(name)
                else:
                    dirs.append(rel)
            for name in names:
                rel = rel_base + "/" + name if rel_base else name
                info = os.lstat(os.path.join(base, name))
                if not stat.S_ISREG(info.st_mode) or not valid(rel):
                    continue
                digest = None
                if rel not in known and info.st_size <= spec["md5_limit"]:
                    h = hashlib.md5()
                    with open(os.path.join(base, name), "rb") as stream:
                        for chunk in iter(lambda: stream.read(1 << 20), b""):
                            h.update(chunk)
                    digest = h.hexdigest()
                files[rel] = [info.st_size, info.st_mtime_ns, digest]
    out[root] = {"files": files, "dirs": dirs, "baseline": baseline}
with open(sys.argv[2], "w") as stream:
    json.dump(out, stream)
"""

_APPLY = r"""
import json, os, shutil, sys
plan = json.load(open(sys.argv[1]))
root, writable = plan["root"], plan["writable"]
uid, gid = (plan["uid"], plan["gid"]) if writable else (0, 0)
baseline = plan["baseline"]
raced = []

def own(path, mode):
    os.chown(path, uid, gid, follow_symlinks=False)
    os.chmod(path, mode)

def ensure_dir(path):
    missing = []
    while path != root and not os.path.isdir(path):
        missing.append(path)
        path = os.path.dirname(path)
    for item in reversed(missing):
        if os.path.lexists(item):
            os.unlink(item)
        os.mkdir(item)
        own(item, 0o755)

def current(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return [info.st_size, info.st_mtime_ns]

os.makedirs(root, exist_ok=True)
if os.path.islink(root):
    raise SystemExit("space root must not be a symlink")
own(root, 0o755)
for rel in plan["mkdirs"]:
    ensure_dir(os.path.join(root, rel))
for item in plan["writes"]:
    path = os.path.join(root, item["rel"])
    if current(path) != item["expect"]:
        raced.append(item["rel"])
        os.unlink(item["staged"])
        continue
    ensure_dir(os.path.dirname(path))
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    shutil.move(item["staged"], path)
    own(path, 0o644)
    info = os.lstat(path)
    baseline["files"][item["rel"]] = {"etag": item["etag"], "size": info.st_size,
                                      "mtime_ns": info.st_mtime_ns}
for item in plan["deletes"]:
    path = os.path.join(root, item["rel"])
    state = current(path)
    if state is not None and state != item["expect"]:
        raced.append(item["rel"])
        continue
    if state is not None:
        os.unlink(path)
    baseline["files"].pop(item["rel"], None)
for rel in plan["rmdirs"]:
    try:
        os.rmdir(os.path.join(root, rel))
    except OSError:
        pass
os.makedirs(os.path.dirname(plan["state"]), mode=0o700, exist_ok=True)
temporary = plan["state"] + ".tmp"
with open(temporary, "w") as stream:
    json.dump(baseline, stream)
os.chmod(temporary, 0o600)
os.replace(temporary, plan["state"])
with open(sys.argv[2], "w") as stream:
    json.dump({"raced": raced}, stream)
"""


async def _run(sandbox, script: str, payload: dict, *, timeout_s: int) -> dict:
    from opensandbox.models.execd import RunCommandOpts
    token = uuid.uuid4().hex
    source, target = f"/tmp/.jx-space-{token}.in", f"/tmp/.jx-space-{token}.out"
    await sandbox.files.write_file(source, json.dumps(payload).encode(), mode=600)
    try:
        result = await asyncio.wait_for(sandbox.commands.run(
            "python3 -c " + shlex.quote(script) + " " + source + " " + target,
            opts=RunCommandOpts(timeout=timedelta(seconds=timeout_s))), timeout=timeout_s + 10)
        if getattr(result, "exit_code", None) != 0:
            from core.sandbox.providers.opensandbox.runtime import _join_logs
            stderr = _join_logs(getattr(getattr(result, "logs", None), "stderr", None) or [])
            raise RuntimeError(stderr.strip()[-500:] or "space script failed")
        raw = await sandbox.files.read_bytes(target)
        return json.loads(raw if isinstance(raw, (bytes, str)) else bytes(raw))
    finally:
        try:
            await sandbox.commands.run("rm -f -- " + source + " " + target,
                                       opts=RunCommandOpts(timeout=timedelta(seconds=10)))
        except Exception:  # noqa: BLE001 - /tmp is cleared on reset anyway
            pass


async def scan(sandbox, roots: list[dict], *, md5_limit: int) -> dict:
    return await _run(sandbox, _SCAN, {"roots": roots, "md5_limit": md5_limit}, timeout_s=120)


async def apply(sandbox, plan: dict) -> dict:
    payload = dict(plan, uid=SANDBOX_RUN_UID, gid=SANDBOX_RUN_GID)
    return await _run(sandbox, _APPLY, payload, timeout_s=300)


async def stage(sandbox, items: list[tuple[str, object]]) -> None:
    """Upload downloaded objects to staging paths in as few requests as practical."""
    from opensandbox.models.filesystem import WriteEntry
    batch, size = [], 0
    for path, stream in items:
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(0)
        batch.append(WriteEntry(path=path, data=stream, mode=600))
        size += length
        if len(batch) >= 32 or size >= 32 * 1024 * 1024:
            await sandbox.files.write_files(batch)
            batch, size = [], 0
    if batch:
        await sandbox.files.write_files(batch)


async def read_into(sandbox, path: str, stream) -> None:
    chunks = await sandbox.files.read_bytes_stream(path, chunk_size=1024 * 1024)
    try:
        async for chunk in chunks:
            stream.write(chunk)
    finally:
        close = getattr(chunks, "aclose", None)
        if close is not None:
            await close()
