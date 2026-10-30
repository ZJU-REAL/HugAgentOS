"""Kubernetes without privileged mounts: keep a synchronized copy of each space.

Each use compares object storage, the sandbox copy and the baseline recorded
at the last synchronization (``manifest.plan``), then moves only what changed
in either direction over execd's HTTP file API. No volume is mounted, so the
sandbox Pod needs no FUSE, privileged helper or extra RBAC.

Consistency holds at command boundaries: remote changes appear before a
command starts, sandbox changes are stored after it ends (and periodically
while the session is kept alive). A change made on both sides is never lost;
the sandbox version is stored next to the remote one under a conflict name.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime

from core.files.spaces.access import grants_for
from core.files.spaces.transport.base import SpaceTransport, SyncReport
from core.files.spaces.transport.sync import sandbox_io
from core.files.spaces.transport.sync.config import sync_limits
from core.files.spaces.transport.sync.manifest import (
    Baseline, Base, Local, conflict_name, locally_changed, plan,
)
from core.files.spaces.transport.sync.remote import PreconditionFailed, RemoteSpace

logger = logging.getLogger(__name__)

_LOCK_ATTR = "_jx_space_sync_lock"
_FLUSHED_ATTR = "_jx_space_flushed_at"
_DOWNLOAD_BATCH = 64


def state_path(grant) -> str:
    digest = hashlib.sha256(grant.prefix.encode()).hexdigest()[:32]
    return f"{sandbox_io.STATE_DIR}/{digest}.json"


def _containing(grants, actor, path):
    """The personal grants whose copy holds ``path``."""
    for alias, target in (("/myspace", f"/workspace/myspace/{actor}"),):
        if path == alias or path.startswith(alias + "/"):
            path = target + path[len(alias):]
    return tuple(g for g in grants if path == g.mount_path or path.startswith(g.mount_path + "/"))


def _spooled():
    return tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)


class SyncTransport(SpaceTransport):
    name = "k8s-sync"
    synchronizes = True

    def validate(self):
        if os.getenv("STORAGE_TYPE", "").lower() != "s3":
            raise RuntimeError("Kubernetes space transports require STORAGE_TYPE=s3")
        if not os.getenv("S3_BUCKET"):
            raise RuntimeError("HUGAGENT_SPACE_MOUNT_MODE=sync requires S3_BUCKET")
        sync_limits()

    def volume(self, grant):
        return None

    async def before_use(self, sandbox, actor, *, path=None) -> SyncReport:
        from core.sandbox.errors import SandboxError
        try:
            return await self.synchronize(sandbox, actor, pull=True, path=path)
        except Exception as exc:
            logger.warning("[space-sync] pull failed sandbox=%s: %r", getattr(sandbox, "id", "?"), exc)
            raise SandboxError("空间文件同步失败，命令未执行，工作区已保留，请重试") from exc

    async def after_use(self, sandbox, actor, *, path=None) -> SyncReport:
        try:
            return await self.synchronize(sandbox, actor, pull=False, path=path)
        except Exception as exc:  # noqa: BLE001 - the command already ran; report, retry later
            logger.warning("[space-sync] push failed sandbox=%s: %r", getattr(sandbox, "id", "?"), exc)
            return SyncReport(errors=["空间写回失败，修改仍保留在沙盒中，将在下次命令前重试"])

    async def flush(self, sandbox, actor, *, force=False) -> SyncReport:
        if not force and time.monotonic() - getattr(sandbox, _FLUSHED_ATTR, 0.0) < sync_limits().flush_interval_s:
            return SyncReport()
        report = await self.after_use(sandbox, actor)
        # Also after a failure, which ``synchronize`` does not record: throttles retries.
        setattr(sandbox, _FLUSHED_ATTR, time.monotonic())
        return report

    async def synchronize(self, sandbox, actor, *, pull: bool, path=None) -> SyncReport:
        """Bring each grant's copy and its objects to the same state.

        Without ``pull`` (after a command) a grant whose copy is unchanged since
        the last sync is skipped without listing storage; remote changes reach it
        before the next command."""
        report = SyncReport()
        if not actor:
            return report
        grants = await asyncio.to_thread(grants_for, actor)
        if path is not None:
            grants = _containing(grants, actor, str(path))
        if not grants:
            return report
        limits = sync_limits()
        from core.files.storage import get_storage
        from core.sandbox.providers.opensandbox.runtime import sandbox_lock
        storage = get_storage()
        async with sandbox_lock(sandbox, _LOCK_ATTR):
            roots = [{"root": g.mount_path, "state": state_path(g)} for g in grants]
            scanned = await sandbox_io.scan(sandbox, roots, md5_limit=limits.max_file_bytes)
            # Grants own disjoint roots and prefixes.
            await asyncio.gather(*(
                _GrantSync(sandbox, grant, RemoteSpace(storage, grant.prefix), scanned[grant.mount_path],
                           limits, report).run(pull=pull)
                for grant in grants))
            setattr(sandbox, _FLUSHED_ATTR, time.monotonic())
        return report


class _GrantSync:
    """One synchronization of one grant: plan, transfer, then record the baseline."""

    def __init__(self, sandbox, grant, remote: RemoteSpace, scanned: dict, limits, report: SyncReport):
        self.sandbox, self.grant, self.remote, self.limits = sandbox, grant, remote, limits
        self.local = {rel: Local(v[0], v[1], v[2]) for rel, v in scanned["files"].items()}
        self.local_dirs = set(scanned["dirs"])
        self.has_baseline = scanned["baseline"] is not None
        self.baseline = Baseline.load(scanned["baseline"])
        self.slots = asyncio.Semaphore(limits.concurrency)
        self.stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.report = report

    async def run(self, *, pull: bool) -> None:
        if not pull and self.has_baseline and not locally_changed(self.local, self.local_dirs, self.baseline):
            return
        remote, remote_dirs = await asyncio.to_thread(self.remote.listing, limit=self.limits.max_entries)
        result = plan(remote, remote_dirs, self.local, self.local_dirs, self.baseline,
                      writable=self.grant.writable, max_file_bytes=self.limits.max_file_bytes,
                      max_total_bytes=self.limits.max_total_bytes)
        if result.is_empty() and self.has_baseline:
            return
        files = dict(self.baseline.files)
        downloads, local_deletes = list(result.downloads), list(result.local_deletes)

        async def resolve_conflict(rel):
            await self._keep_sandbox_copy(rel)
            (downloads if rel in remote and rel not in result.skipped else local_deletes).append(rel)

        async def upload(rel, expected):
            item = self.local[rel]
            try:
                etag = await self._push(rel, rel, expected)
            except PreconditionFailed:
                await self._keep_sandbox_copy(rel)
                downloads.append(rel)
                return
            # The scanned state, not a fresh stat: a later edit must still look changed.
            files[rel] = Base(etag, item.size, item.mtime_ns)

        async def delete(rel, expected):
            try:
                await asyncio.to_thread(self.remote.delete, rel, expected)
                files.pop(rel, None)
            except PreconditionFailed:
                downloads.append(rel)

        await asyncio.gather(
            *(resolve_conflict(rel) for rel in result.conflicts),
            *(upload(rel, expected) for rel, expected in result.uploads.items()),
            *(delete(rel, expected) for rel, expected in result.remote_deletes.items()),
            *(asyncio.to_thread(self.remote.make_directory, rel) for rel in result.remote_mkdirs))
        for rel, etag in result.adopt.items():
            item = self.local[rel]
            files[rel] = Base(etag, item.size, item.mtime_ns)
        for rel in result.forget:
            files.pop(rel, None)
        # Deepest first: a parent is only empty once its children are gone.
        removed_remote = {rel for rel in result.remote_rmdirs
                          if await asyncio.to_thread(self.remote.remove_directory, rel)}
        dirs = ((remote_dirs | set(result.remote_mkdirs)) - removed_remote) & \
            ((self.local_dirs | set(result.local_mkdirs)) - set(result.local_rmdirs))

        self.report.rejected += [self._display(rel) for rel in result.rejected]
        self.report.skipped += [self._display(rel) for rel in result.newly_skipped]

        writes = await self._stage_downloads(sorted(set(downloads)))
        outcome = await sandbox_io.apply(self.sandbox, {
            "root": self.grant.mount_path, "state": state_path(self.grant),
            "writable": self.grant.writable, "baseline": Baseline(files, dirs, result.skipped).dump(),
            "writes": writes,
            "deletes": [{"rel": rel, "expect": self._expect(rel)} for rel in sorted(set(local_deletes))
                        if rel in self.local],
            "mkdirs": sorted(set(result.local_mkdirs)), "rmdirs": result.local_rmdirs,
        })
        if outcome.get("raced"):
            logger.info("[space-sync] %d path(s) changed during sync; retried next time",
                        len(outcome["raced"]))

    def _display(self, rel: str) -> str:
        return self.grant.mount_path + "/" + rel

    def _expect(self, rel):
        item = self.local.get(rel)
        return None if item is None else [item.size, item.mtime_ns]

    async def _push(self, rel: str, key: str, expected: str | None) -> str:
        """Upload the sandbox file ``rel`` to ``key`` if the object still has ``expected``."""
        async with self.slots:
            stream = _spooled()
            try:
                await sandbox_io.read_into(self.sandbox, self._display(rel), stream)
                stream.seek(0)
                return await asyncio.to_thread(self.remote.upload, key, stream, expected)
            finally:
                stream.close()

    async def _keep_sandbox_copy(self, rel: str) -> None:
        """Store the sandbox version under a conflict name that does not exist yet."""
        for attempt in range(1, 100):
            copy = conflict_name(rel, self.stamp if attempt == 1 else f"{self.stamp}-{attempt}")
            try:
                await self._push(rel, copy, None)
            except PreconditionFailed:
                continue
            self.report.conflicts.append((self._display(rel), self._display(copy)))
            return
        raise RuntimeError("No free conflict name for " + rel)

    async def _stage_downloads(self, rels: list[str]) -> list[dict]:
        writes = []
        token = uuid.uuid4().hex
        for start in range(0, len(rels), _DOWNLOAD_BATCH):
            chunk = rels[start:start + _DOWNLOAD_BATCH]
            streams = [_spooled() for _ in chunk]
            try:
                async def fetch(rel, stream):
                    async with self.slots:
                        return await asyncio.to_thread(self.remote.download, rel, stream)
                etags = await asyncio.gather(*(fetch(r, s) for r, s in zip(chunk, streams)))
                staged = []
                for index, (rel, stream, etag) in enumerate(zip(chunk, streams, etags)):
                    if etag is None:
                        continue
                    path = f"/tmp/.jx-space-stage-{token}-{start + index}"
                    staged.append((path, stream))
                    writes.append({"rel": rel, "staged": path, "etag": etag,
                                   "expect": self._expect(rel)})
                await sandbox_io.stage(self.sandbox, staged)
            finally:
                for stream in streams:
                    stream.close()
        return writes


__all__ = ["SyncTransport"]
