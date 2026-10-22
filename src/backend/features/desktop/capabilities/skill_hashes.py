"""skill hashes responsibilities."""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Callable, Tuple
from features.desktop.capabilities import skills_state

def dir_signature(path: Path) -> Tuple:
    from core.capabilities.archive import iter_file_stats

    # A maximum mtime misses edits to any file older than the newest file.
    return tuple(
        (rel, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_ino)
        for rel, _, metadata in iter_file_stats(path)
        if rel != skills_state.INVENTORY_NAME
    )


def dir_digest(path: Path, compute: Callable[[], str], *, fresh: bool = False) -> str:
    """A package directory's content digest, cached on its file signature.

    Identity is *established* with ``fresh=True``: every byte is read, so the
    digest a binding freezes is always a true content hash. Later passes only
    *verify* that digest, and a run re-verifies its whole closure on every
    message — re-reading a hundred unchanged packages each time is pure cost.
    So verification recomputes exactly when the signature moved (a file was
    added, removed, or written), and reuses the frozen digest otherwise.
    """
    key = str(path.resolve())
    sig = dir_signature(path)
    with skills_state.hash_lock:
        hit = skills_state.hash_cache.get(key)
        if not fresh and hit and hit[0] == sig:
            return hit[1]
    digest = compute()
    with skills_state.hash_lock:
        skills_state.hash_cache[key] = (sig, digest)
    return digest


def skill_dir_hash(path: Path, *, fresh: bool = False) -> str:
    """Hash every package file; reject links and excess sizes rather than omit bytes."""
    from core.capabilities.archive import iter_file_stats

    key = str(path.resolve())
    entries = [item for item in iter_file_stats(path) if item[0] != skills_state.INVENTORY_NAME]
    sig = tuple(
        (rel, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_ino)
        for rel, _, metadata in entries
    )
    with skills_state.hash_lock:
        hit = skills_state.hash_cache.get(key)
        if not fresh and hit and hit[0] == sig:
            return hit[1]
    # Reuse this traversal's metadata; fresh verification still reads every byte.
    digest = hash_skill_package(path, entries=entries)
    with skills_state.hash_lock:
        skills_state.hash_cache[key] = (sig, digest)
    return digest


def hash_skill_package(path: Path, *, entries=None) -> str:
    from core.capabilities.skills.binary_files import encode_upload
    from core.capabilities.manifest_protocol import skill_content_hash

    from core.capabilities import archive as archive
    from core.harness.capability_errors import IntegrityFailed
    from core.infra.directory_links import native_path as _native

    raw_files = {}
    total = 0
    for rel, file, metadata in archive.iter_file_stats(path) if entries is None else entries:
        if rel == skills_state.INVENTORY_NAME:
            continue
        size = metadata.st_size
        total += size
        if (
            size > archive.MAX_MEMBER_BYTES
            or total > archive.MAX_TOTAL_BYTES
            or len(raw_files) >= archive.MAX_MEMBERS
        ):
            raise IntegrityFailed("skill package exceeds content verification limits")
        # Scanning already supports extended Windows paths; byte reads must too.
        raw = _native(file).read_bytes()
        if len(raw) != size:
            raise IntegrityFailed("skill package changed during verification")
        raw_files[rel] = raw
    content_key = tuple((rel, hashlib.sha256(raw).digest()) for rel, raw in raw_files.items())
    with skills_state.hash_lock:
        cached = skills_state.content_hash_cache.get(content_key)
        if cached is not None:
            skills_state.content_hash_cache.move_to_end(content_key)
            return cached
    files = {rel: encode_upload(rel, raw) for rel, raw in raw_files.items()}
    digest = skill_content_hash(files.pop("SKILL.md", ""), files)
    with skills_state.hash_lock:
        skills_state.content_hash_cache[content_key] = digest
        skills_state.content_hash_cache.move_to_end(content_key)
        while len(skills_state.content_hash_cache) > 256:
            skills_state.content_hash_cache.popitem(last=False)
    return digest
