"""OpenSandbox provider internals — module-level helpers, manifest parsing,
session/pool helper classes. Extracted from opensandbox_provider.py to slim it;
the OpenSandboxProvider class imports these back. No dependency on the provider
class itself (no cycle).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional


from ._common import myspace_cache_dir, safe_user_id


logger = logging.getLogger(__name__)
from . import _opensandbox_internals as runtime

# Built-in skills enter the sandbox at /workspace/skills/ via a host bind mount, read-only.
# This replaces the old materialization path of "per chat, tar-upload every used skill +
# unpack with Python" (the main source of the 50-80s slow first bash). Editing skill content
# on the host makes it immediately visible to all new sandboxes, no image rebuild needed;
# only pip / apt dependency changes require rebuilding Dockerfile.opensandbox.
#
# Note: an absolute host path is required — the OpenSandbox server creates sandbox
# containers through the host docker daemon, and docker.sock sees the host's view.
# HOST_REPO_PATH is injected by docker-compose (the .env variable of the same name);
# when missing we fall back to the in-container path derived from Path(__file__) —
# in dev the backend already bind-mounts `${HOST_REPO_PATH}:${HOST_REPO_PATH}`, so
# paths inside and outside the container are identical and the derived result is
# also a valid host path.
_BUILTIN_SKILLS_HOST_PATH: Optional[str] = None


def _resolve_builtin_skills_host_path() -> Optional[str]:
    """Compute the absolute host path of the built-in skills dir, for sandbox volumes."""
    global _BUILTIN_SKILLS_HOST_PATH
    if _BUILTIN_SKILLS_HOST_PATH is not None:
        return _BUILTIN_SKILLS_HOST_PATH or None  # "" → None
    host_repo = os.environ.get("HOST_REPO_PATH", "").strip()
    if host_repo:
        candidate = f"{host_repo.rstrip('/')}/src/backend/skill_bundles"
    else:
        # In-container derived path: this file lives in src/backend/core/sandbox/,
        # walk back up to src/backend then down into skill_bundles. In dev the backend
        # bind-mounts the whole repo via HOST_REPO_PATH, so the absolute path inside
        # the container equals the host path.
        candidate = str(
            (Path(__file__).resolve().parent.parent.parent / "skill_bundles")
        )
    if not Path(candidate).is_dir():
        logger.warning(
            "[opensandbox] built-in skills dir not found at %s; skill bind-mount disabled",
            candidate,
        )
        _BUILTIN_SKILLS_HOST_PATH = ""
        return None
    _BUILTIN_SKILLS_HOST_PATH = candidate
    return candidate


# Metadata tags used for adoption / cross-process pool reuse. Every sandbox created by
# this app carries them; after a backend restart _adopt_existing_sandboxes uses this tag
# to precisely identify "our own orphans" in the OpenSandbox server's full listing —
# adopting them back into the pool saves the ~15s cost of recreating a sandbox +
# starting the Jupyter Server.
#
# - hugagent-owner=backend  : distinguishes this service from other clients' sandboxes on OpenSandbox
# - hugagent-pool-kind=...  : "jupyter" / "light", refills into the correct bucket
# Design choice: no instance-id tag (process pid or hostname) — adoption is meant to
# share across instances; partitioning by instance would defeat the purpose. Chat-bound
# sandboxes carry no distinguishing tag either, and cannot: a sandbox handed out from
# the user idle pool is bound to a chat long after creation, while metadata is fixed at
# creation. 「这台容器有没有主」改问 session_registry 的反向索引 ``bound_session``。
_METADATA_OWNER_KEY = "hugagent-owner"
_METADATA_OWNER_VALUE = "backend"
_METADATA_POOL_KIND_KEY = "hugagent-pool-kind"
# Plan F: jupyter sandboxes are bucketed by user_id. Every jupyter sandbox created by this
# service carries this tag; after a backend restart _adopt_existing_sandboxes uses it to
# reclaim orphan sandboxes into the correct user's pool — cross-user reuse would let one
# user see another's myspace bind contents, a security hole that must be prevented.
_METADATA_USER_ID_KEY = "hugagent-user-id"
# Which skill layout a sandbox was created with. Mounts are fixed at creation, so a
# sandbox from before the per-user skill view still has the whole shared dir bound at
# /workspace/skills and would miss its user's private skills once those move into the
# per-user dir. Adoption after a backend upgrade uses this tag to drop such sandboxes
# instead of handing back one whose skill mount no longer matches.
_METADATA_SKILLS_LAYOUT_KEY = "hugagent-skills-layout"
_METADATA_SKILLS_LAYOUT_VALUE = "user-view"


def _pool_metadata(kind: str, user_id: Optional[str] = None) -> dict[str, str]:
    md = {
        _METADATA_OWNER_KEY: _METADATA_OWNER_VALUE,
        _METADATA_POOL_KIND_KEY: kind,
    }
    # Only jupyter sandboxes are bucketed per user; light sandboxes are ephemeral and not
    # user-bound. Must normalize with safe_user_id — if the raw user_id is invalid,
    # _make_myspace_volume refuses to mount the volume; in that case we must not tag here
    # either, otherwise adopt would use this tag to bucket into _jupyter_user_pool and a
    # sandbox with no mount would be reused as user-bound, bypassing isolation.
    if user_id and kind == "jupyter":
        safe = safe_user_id(user_id)
        if safe:
            md[_METADATA_USER_ID_KEY] = safe
            md[_METADATA_SKILLS_LAYOUT_KEY] = _METADATA_SKILLS_LAYOUT_VALUE
    return md


def _make_skills_volumes(user_id: Optional[str] = None) -> list:
    """Build the read-only skill bind-mounts, isolated per user.

    A sandbox belonging to ``user_id`` mounts **that user's skill view** at
    ``/workspace/skills`` (their private skills plus one relative symlink per
    shared skill) and the shared dir at ``/workspace/skills_shared``, where those
    symlinks resolve. Every skill keeps the one path ``/workspace/skills/<id>``,
    while another user's private skills — a market skill's ``secrets.json``
    included — are simply not mounted. Nothing is copied; the view is symlinks.

    Without a usable user id only the shared dir is mounted, at
    ``/workspace/skills``: the pre-warmed ephemeral sandboxes are handed to
    whoever asks next and must never carry someone's private files.

    Read-only, and the bind is live, so newly imported skills appear at once.
    Without ``HOST_STORAGE_PATH`` fall back to the built-in source tree alone and
    warn; on failure return an empty list — the sandbox is created as usual, just
    without /workspace/skills/, and skill references hit a plain not-found.
    """
    try:
        from opensandbox.models.sandboxes import Host, Volume
    except ImportError:  # older SDKs lack these two — can only continue without mounting
        return []

    host_storage = (runtime.settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
    if not host_storage:
        # No host storage → no unified dir to bind; mount the built-in source tree alone.
        legacy = _resolve_builtin_skills_host_path()
        logger.warning(
            "[opensandbox] HOST_STORAGE_PATH 未配置；/workspace/skills 仅挂内置源码树，"
            "DB 导入技能在沙盒内不可见"
        )
        if not legacy:
            return []
        return [
            Volume(
                name="skills",
                host=Host(path=legacy),
                mount_path="/workspace/skills",
                read_only=True,
            )
        ]

    # get_sandbox_skills_dir() / get_user_skills_root() return the container-side
    # dirs (the same storage volume as the host bind source) and guarantee they
    # exist — OpenSandbox fails to bind a nonexistent source. Only their basenames
    # are used to compose host paths, so the names are not written twice.
    from core.agent_skills.config import (
        SHARED_LINK_NAME,
        get_sandbox_skills_dir,
        get_user_skills_root,
    )

    shared_host = f"{host_storage}/{get_sandbox_skills_dir().name}"
    from core.agent_skills.publication import prepare_skill_view

    view = prepare_skill_view(safe_user_id(user_id) or None)
    if view is None:
        return [
            Volume(
                name="skills",
                host=Host(path=shared_host),
                mount_path="/workspace/skills",
                read_only=True,
            )
        ]
    view_host = f"{host_storage}/{get_user_skills_root().name}/{view.name}"
    return [
        Volume(
            name="skills",
            host=Host(path=view_host),
            mount_path="/workspace/skills",
            read_only=True,
        ),
        # The view's entries for shared skills are relative symlinks
        # (``../skills_shared/<id>``), so this mount path is what makes them
        # resolve inside the sandbox — it must stay in step with SHARED_LINK_NAME.
        Volume(
            name="skills-shared",
            host=Host(path=shared_host),
            mount_path=f"/workspace/{SHARED_LINK_NAME}",
            read_only=True,
        ),
    ]


def _make_myspace_volume(user_id: Optional[str]) -> Optional[Any]:
    """Plan F: bind the myspace_cache/{uid}/ subdirectory under the backend host storage
    into the sandbox at ``/workspace/myspace/{uid}/``, visible at startup, eliminating
    the full HTTP PUT of _sync_inputs_to_sandbox.

    Prerequisites:
    - runtime.settings.sandbox.opensandbox_myspace_bind_mount_enabled = true (default)
    - runtime.settings.sandbox.opensandbox_host_storage_path non-empty (points to the real
      location of the backend storage directory on the host, i.e. the
      ${HOST_STORAGE_PATH} used by the backend volumes in docker-compose)
    - the OpenSandbox server's ``allowed_host_paths`` includes that path prefix

    Two cases return None:
    1. flag off / invalid user_id / host_storage_path not configured — the caller
       continues without mounting the myspace volume and falls back to the old HTTP PUT
       path (depending on which flag branch takes effect)
    2. SDK model import failure — same as above
    """
    if not runtime.settings.sandbox.opensandbox_myspace_bind_mount_enabled:
        return None
    uid = safe_user_id(user_id or "")
    if not uid:
        return None
    host_storage = (runtime.settings.sandbox.opensandbox_host_storage_path or "").strip().rstrip("/")
    if not host_storage:
        logger.warning(
            "[opensandbox] myspace bind mount enabled but HOST_STORAGE_PATH "
            "not configured; falling back to HTTP PUT path"
        )
        return None
    # Proactively mkdir so the host subdirectory is owned by the in-container backend
    # process (appuser, UID 1000), instead of relying on the docker daemon's root-owned
    # auto-mkdir at bind time — that would leave files written by root inside the sandbox
    # that the backend cannot chown/delete/overwrite. If mkdir fails we must abandon the
    # bind mount (return None and fall back to the old path); anything else steps right
    # into the root-owned trap we are trying to avoid.
    cache_dir = myspace_cache_dir(uid)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[opensandbox] failed to pre-create myspace_cache dir %s: %s; "
            "skipping bind mount (would otherwise create root-owned host dir)",
            cache_dir, exc,
        )
        return None
    try:
        from opensandbox.models.sandboxes import Host, Volume
    except ImportError:
        return None
    host_path = f"{host_storage}/myspace_cache/{uid}"
    # Volume name must match ``^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`` (OpenSandbox enforces
    # k8s-style naming); _ / . / uppercase letters in the uid are all invalid — normalize
    # to lowercase and substitute `-`.
    slug = re.sub(r"[^a-z0-9-]", "-", uid.lower()).strip("-")
    vol_name = f"myspace-{slug}"[:63].rstrip("-") or "myspace"
    return Volume(
        name=vol_name,
        host=Host(path=host_path),
        mount_path=f"/workspace/myspace/{uid}",
        read_only=False,
    )
