"""Three-way comparison of object storage, the sandbox copy and the last sync.

Pure functions only: every decision about which side wins lives here, so the
table in internal design docs is tested
without a sandbox or a bucket.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Remote:
    etag: str
    size: int
    modified: float = 0.0

    @property
    def md5(self) -> str | None:
        """S3 ETag of a single-part upload is the content MD5; multipart ETags are not."""
        value = self.etag.strip('"')
        return None if "-" in value else value.lower()


@dataclass(frozen=True)
class Local:
    size: int
    mtime_ns: int
    md5: str | None = None


@dataclass(frozen=True)
class Base:
    etag: str
    size: int
    mtime_ns: int


@dataclass
class Baseline:
    files: dict[str, Base] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    # Remote files deliberately kept out of the copy, reported once each.
    skipped: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, data) -> "Baseline":
        if not isinstance(data, dict) or data.get("version") != 1:
            return cls()
        files = {}
        for rel, item in (data.get("files") or {}).items():
            try:
                files[rel] = Base(str(item["etag"]), int(item["size"]), int(item["mtime_ns"]))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(files, set(data.get("dirs") or ()), dict(data.get("skipped") or {}))

    def dump(self) -> dict:
        return {"version": 1,
                "files": {rel: {"etag": b.etag, "size": b.size, "mtime_ns": b.mtime_ns}
                          for rel, b in sorted(self.files.items())},
                "dirs": sorted(self.dirs), "skipped": dict(sorted(self.skipped.items()))}


@dataclass
class Plan:
    # Remote content replaces the sandbox file (expected local state is recorded).
    downloads: list[str] = field(default_factory=list)
    # Sandbox content replaces the object; value is the ETag it must still have
    # (None: the object must not exist yet).
    uploads: dict[str, str | None] = field(default_factory=dict)
    remote_deletes: dict[str, str] = field(default_factory=dict)
    local_deletes: list[str] = field(default_factory=list)
    # Both sides changed: keep the sandbox version under a new name, then download.
    conflicts: list[str] = field(default_factory=list)
    # Both sides hold identical new content; only the baseline is recorded.
    adopt: dict[str, str] = field(default_factory=dict)
    forget: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    newly_skipped: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    remote_mkdirs: list[str] = field(default_factory=list)
    remote_rmdirs: list[str] = field(default_factory=list)
    local_mkdirs: list[str] = field(default_factory=list)
    local_rmdirs: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any((self.downloads, self.uploads, self.remote_deletes, self.local_deletes,
                        self.conflicts, self.adopt, self.forget, self.remote_mkdirs,
                        self.remote_rmdirs, self.local_mkdirs, self.local_rmdirs,
                        self.newly_skipped))


def local_changed(local: Local | None, base: Base | None) -> bool:
    if local is None or base is None:
        return (local is None) != (base is None)
    return local.size != base.size or local.mtime_ns != base.mtime_ns


def remote_changed(remote: Remote | None, base: Base | None) -> bool:
    if remote is None or base is None:
        return (remote is None) != (base is None)
    return remote.etag != base.etag


def locally_changed(local: dict[str, Local], local_dirs: set[str], baseline: Baseline) -> bool:
    """Whether the sandbox copy differs from the last sync at all."""
    if local_dirs != baseline.dirs or set(local) - set(baseline.files) - set(baseline.skipped):
        return True
    return any(local_changed(local.get(rel), base) for rel, base in baseline.files.items())


def eligible(remote: dict[str, Remote], *, max_file_bytes: int, max_total_bytes: int) -> set[str]:
    """Newest files first until the copy reaches its total size limit."""
    chosen, total = set(), 0
    for rel, item in sorted(remote.items(), key=lambda kv: (-kv[1].modified, kv[0])):
        if item.size > max_file_bytes or total + item.size > max_total_bytes:
            continue
        chosen.add(rel)
        total += item.size
    return chosen


def plan(remote: dict[str, Remote], remote_dirs: set[str], local: dict[str, Local],
         local_dirs: set[str], baseline: Baseline, *, writable: bool,
         max_file_bytes: int, max_total_bytes: int) -> Plan:
    result = Plan()
    allowed = eligible(remote, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes)
    for rel in sorted(set(remote) | set(local) | set(baseline.files)):
        r, l, b = remote.get(rel), local.get(rel), baseline.files.get(rel)
        lc, rc = local_changed(l, b), remote_changed(r, b)
        too_big_local = l is not None and l.size > max_file_bytes
        if r is not None and rel not in allowed:
            result.skipped[rel] = r.etag
            if baseline.skipped.get(rel) != r.etag:
                result.newly_skipped[rel] = r.etag
            if l is not None and lc and writable and not too_big_local:
                # A local file at a path we never mirrored: keep both versions.
                result.conflicts.append(rel)
            elif b is not None and l is not None and not lc:
                result.local_deletes.append(rel)
            elif b is not None:
                result.forget.append(rel)
            continue
        if not lc and not rc:
            continue
        if not writable:
            if lc:
                result.rejected.append(rel)
            if r is not None:
                result.downloads.append(rel)
            elif l is not None:
                result.local_deletes.append(rel)
            else:
                result.forget.append(rel)
            continue
        if too_big_local and lc:
            result.skipped[rel] = "local"
            if baseline.skipped.get(rel) != "local":
                result.newly_skipped[rel] = "local"
            continue
        if lc and not rc:
            if l is None:
                result.remote_deletes[rel] = b.etag
            else:
                result.uploads[rel] = b.etag if b is not None else None
        elif rc and not lc:
            if r is None:
                result.local_deletes.append(rel)
            else:
                result.downloads.append(rel)
        elif r is None and l is None:
            result.forget.append(rel)
        elif r is None:
            # Deleted remotely, edited here: the edit survives.
            result.uploads[rel] = None
        elif l is None:
            # Deleted here, edited remotely: the remote edit survives.
            result.downloads.append(rel)
        elif b is None and l.md5 is not None and l.md5 == r.md5:
            result.adopt[rel] = r.etag
        else:
            result.conflicts.append(rel)

    created_remote = remote_dirs - baseline.dirs
    created_local = local_dirs - baseline.dirs
    for rel in sorted(created_remote - local_dirs):
        result.local_mkdirs.append(rel)
    for rel in sorted(baseline.dirs):
        in_r, in_l = rel in remote_dirs, rel in local_dirs
        if in_r and not in_l:
            if writable:
                result.remote_rmdirs.append(rel)
            else:
                result.local_mkdirs.append(rel)
        elif in_l and not in_r:
            result.local_rmdirs.append(rel)
    if writable:
        for rel in sorted(created_local - remote_dirs):
            result.remote_mkdirs.append(rel)
    else:
        for rel in sorted(created_local - remote_dirs):
            result.local_rmdirs.append(rel)
    result.local_rmdirs.sort(key=lambda p: (-p.count("/"), p))
    result.remote_rmdirs.sort(key=lambda p: (-p.count("/"), p))
    return result


def conflict_name(rel: str, stamp: str) -> str:
    """``dir/report.xlsx`` -> ``dir/report (沙盒冲突 20261029-101500).xlsx``."""
    head, _, name = rel.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not stem or not dot:
        stem, ext, dot = name, "", ""
    renamed = f"{stem} (沙盒冲突 {stamp}){dot}{ext}"
    return f"{head}/{renamed}" if head else renamed
