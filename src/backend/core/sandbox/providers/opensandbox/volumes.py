"""Skill volumes and ownership metadata for OpenSandbox instances."""

from __future__ import annotations

from typing import Optional

from core.config.settings import settings
from core.sandbox.sessions.metadata import USER_ID_METADATA_KEY
from core.sandbox.workspace.identity import safe_user_id

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
# Persistent sandboxes are bucketed by user_id. Every jupyter sandbox created by this
# service carries this tag; after a backend restart _adopt_existing_sandboxes uses it to
# reclaim orphan sandboxes into the correct user's pool — cross-user reuse would let one
# user see another's myspace bind contents, a security hole that must be prevented.
_METADATA_USER_ID_KEY = USER_ID_METADATA_KEY
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
    HOST_STORAGE_PATH must identify the same storage root on the Docker host.
    """
    from opensandbox.models.sandboxes import Host, Volume
    from core.sandbox.errors import SandboxError

    host_storage = settings.sandbox.opensandbox_host_storage_path.strip().rstrip("/")
    if not host_storage:
        raise SandboxError("OpenSandbox skill mounts require HOST_STORAGE_PATH")

    # get_sandbox_skills_dir() / get_user_skills_root() return the container-side
    # dirs (the same storage volume as the host bind source) and guarantee they
    # exist — OpenSandbox fails to bind a nonexistent source. Only their basenames
    # are used to compose host paths, so the names are not written twice.
    from core.capabilities.skills.config import SHARED_LINK_NAME, get_sandbox_skills_dir, get_user_skills_root

    shared_host = f"{host_storage}/{get_sandbox_skills_dir().name}"
    from core.capabilities.skills.publication import prepare_skill_view

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
