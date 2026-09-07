"""Skill kind on the desktop store: candidates, resolution, runtime views, local publication.

Two views are maintained, both link-only:

- the **device view** (``get_sandbox_skills_dir()``, i.e. ``{workspace}/skills``):
  shipped built-ins plus device-local skills that are not private to one user.
  Process-level consumers (CLI shims on PATH) resolve through it.
- the **user view** (``get_user_skills_dir(uid)``): everything the current user
  may use — device view entries plus the current cloud account's ready
  installations — after name resolution. Session workspaces link to it.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import registry, store
from . import view as view_mod
from .paths import (
    BUILTIN_PROFILE,
    KIND_SKILL,
    LOCAL_PROFILE,
    capabilities_enabled,
    capability_root,
    revision_for_hash,
)
from .ref import ResourceRef, builtin_ref, local_ref, profile_id
from .resolver import (
    SOURCE_BUILTIN,
    SOURCE_CLOUD,
    SOURCE_LOCAL,
    SOURCE_PLUGIN,
    Candidate,
    Resolution,
    resolve,
)

logger = logging.getLogger(__name__)

_SKIP_PARTS = {"__pycache__", ".git", ".svn", ".hg", "__MACOSX"}
_INVENTORY_NAME = ".inventory.json"

_hash_lock = threading.Lock()
_hash_cache: Dict[str, Tuple[Tuple[int, int], str]] = {}

_resolution_lock = threading.Lock()
_last_resolution: Dict[str, Resolution] = {}
_view_generation = 0


def view_generation() -> int:
    """Bumped whenever store contents change; view consumers refresh on a new value."""
    return _view_generation


def bump_view_generation() -> int:
    global _view_generation
    with _resolution_lock:
        _view_generation += 1
        return _view_generation


def builtin_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "skill_bundles" / "default"


def allowed_link_roots() -> List[Path]:
    roots = [builtin_dir()]
    root = capability_root()
    if root is not None:
        roots.insert(0, root)
    return roots


def device_view_dir() -> Path:
    from core.agent_skills.config import get_sandbox_skills_dir

    return get_sandbox_skills_dir()


def user_view_dir(user_id: Optional[str]) -> Optional[Path]:
    from core.agent_skills.config import get_user_skills_dir

    return get_user_skills_dir(user_id)


# ── content hashing (same scheme the cloud uses for its manifest) ───────


def _dir_signature(path: Path) -> Tuple[int, int]:
    from .archive import iter_files

    entries = [p.stat() for rel, p in iter_files(path) if rel != _INVENTORY_NAME]
    return len(entries), max((s.st_mtime_ns for s in entries), default=0)


def skill_dir_hash(path: Path, *, fresh: bool = False) -> str:
    """Hash every package file; reject links and excess sizes rather than omit bytes."""
    from core.agent_skills.binary_files import encode_upload
    from core.services.desktop_capability_protocol import skill_content_hash
    from . import archive
    from .errors import IntegrityFailed

    key = str(path.resolve())
    sig = _dir_signature(path)
    with _hash_lock:
        hit = _hash_cache.get(key)
        if not fresh and hit and hit[0] == sig:
            return hit[1]
    files = {}
    total = 0
    for rel, file in archive.iter_files(path):
        if rel == _INVENTORY_NAME:
            continue
        size = file.stat().st_size
        total += size
        if (
            size > archive.MAX_MEMBER_BYTES
            or total > archive.MAX_TOTAL_BYTES
            or len(files) >= archive.MAX_MEMBERS
        ):
            raise IntegrityFailed("skill package exceeds content verification limits")
        raw = file.read_bytes()
        if len(raw) != size:
            raise IntegrityFailed("skill package changed during verification")
        files[rel] = encode_upload(rel, raw)
    digest = skill_content_hash(files.pop("SKILL.md", ""), files)
    with _hash_lock:
        _hash_cache[key] = (sig, digest)
    return digest


# ── account profile ───────────────────────────────────────────────────


def current_account_profile() -> Optional[str]:
    """Profile id of the cloud account this device is currently bridged to."""
    from core.services.desktop_capability_protocol import token_subject
    from core.services.desktop_cloud_bridge import get_state

    st = get_state()
    if not st:
        return None
    subject = token_subject(str(st.get("token") or ""))
    if not subject:
        return None
    return profile_id(str(st["cloud_base"]), subject)


def current_local_user_id() -> Optional[str]:
    """Map the signed stable cloud subject to this device's shadow-user ID."""
    from core.services.desktop_cloud_bridge import get_identity_state

    identity = get_identity_state()
    # 本机影子用户按壳送来的命名空间标识建档（cloud:<host>:<port>:<ucid>），
    # 查找必须用同一标识，而不是凭据里的原始云端 id。
    center = str((identity or {}).get("shell_user_center_id") or "")
    if not center:
        return None
    from core.db.models import UserShadow

    with registry._session() as db:
        row = db.query(UserShadow).filter(UserShadow.user_center_id == center).first()
        return str(row.user_id) if row else None


def account_authorized_for(user_id: Optional[str]) -> bool:
    from core.services.desktop_cloud_bridge import get_state

    st = get_state()
    if not st or not str(st.get("token") or "").startswith("dcap2."):
        return False
    current = current_local_user_id()
    return bool(user_id and current and str(user_id) == current)


# ── candidates ────────────────────────────────────────────────────────


def builtin_candidates() -> List[Candidate]:
    root = builtin_dir()
    out: List[Candidate] = []
    if not root.is_dir():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (d / "SKILL.md").is_file():
            continue
        out.append(
            Candidate(
                install_id=registry.install_id(KIND_SKILL, BUILTIN_PROFILE, d.name),
                runtime_name=d.name,
                kind=KIND_SKILL,
                profile=BUILTIN_PROFILE,
                source=SOURCE_BUILTIN,
                path=d,
                content_hash=None,  # computed lazily only on a name collision
                ref=builtin_ref(KIND_SKILL, d.name),
                usable=True,
                account_level=False,
                display_name=d.name,
            )
        )
    return out


def _installation_candidate(inst: registry.Installation, *, account_level: bool) -> Candidate:
    path = None
    if inst.ready:
        comp = store.get(KIND_SKILL, inst.profile_id, inst.key, inst.resolved_revision or "")
        path = comp.path if comp else None
    usable = inst.enabled and path is not None
    return Candidate(
        install_id=inst.install_id,
        runtime_name=str(inst.payload.get("runtime_name") or inst.key),
        kind=KIND_SKILL,
        profile=inst.profile_id,
        source=inst.source,
        path=path,
        content_hash=inst.payload.get("resolved_content_hash") or inst.content_hash,
        revision=inst.resolved_revision,
        ref=inst.ref,
        usable=usable,
        account_level=account_level and inst.enabled,
        state=inst.state if path is not None or not inst.ready else "files_missing",
        display_name=inst.display_name,
        description=inst.description,
        version=inst.version,
    )


def local_candidates(user_id: Optional[str], *, shared_only: bool = False) -> List[Candidate]:
    out: List[Candidate] = []
    for inst in registry.list_installations(kind=KIND_SKILL, profile_id=LOCAL_PROFILE):
        owner = inst.payload.get("owner_user_id")
        if owner and (shared_only or owner != user_id):
            continue
        out.append(_installation_candidate(inst, account_level=True))
    return out


def account_candidates(profile: Optional[str] = None) -> List[Candidate]:
    profile = profile or current_account_profile()
    if not profile:
        return []
    return [
        _installation_candidate(inst, account_level=True)
        for inst in registry.list_installations(kind=KIND_SKILL, profile_id=profile)
    ]


def candidates(user_id: Optional[str]) -> List[Candidate]:
    return (
        builtin_candidates()
        + local_candidates(user_id)
        + (account_candidates() if account_authorized_for(user_id) else [])
    )


def _fill_hashes(cands: List[Candidate]) -> List[Candidate]:
    """Hash only the members of multi-candidate names (identical-content detection)."""
    by_name: Dict[str, List[Candidate]] = {}
    for c in cands:
        by_name.setdefault(c.runtime_name, []).append(c)
    out: List[Candidate] = []
    for group in by_name.values():
        if len(group) < 2:
            out.extend(group)
            continue
        for c in group:
            if c.content_hash is None and c.path is not None:
                c = Candidate(**{**c.__dict__, "content_hash": skill_dir_hash(c.path)})
            out.append(c)
    return out


def resolve_for_user(user_id: Optional[str], *, requested: Optional[Set[str]] = None) -> Resolution:
    from .readiness import eligible_skill_candidates

    preferences = registry.preferences(KIND_SKILL, user_id=user_id)
    eligible = eligible_skill_candidates(
        _fill_hashes(candidates(user_id)), user_id, preferences=preferences, requested=requested
    )
    res = resolve(KIND_SKILL, eligible, preferences=preferences, requested=requested)
    with _resolution_lock:
        _last_resolution[user_id or ""] = res
    return res



def filter_available_names(names, *, user_id=None):
    """Implicit defaults omit unusable names; explicit bindings still fail in preflight."""
    chosen = resolve_for_user(user_id).chosen
    return [name for name in dict.fromkeys(names) if name in chosen]


def last_resolution(user_id: Optional[str]) -> Optional[Resolution]:
    with _resolution_lock:
        return _last_resolution.get(user_id or "")


# ── views ─────────────────────────────────────────────────────────────


def _targets(res: Resolution) -> Dict[str, Path]:
    return {name: c.path for name, c in res.chosen.items() if c.path is not None}


def rebuild_device_view() -> view_mod.ViewReport:
    res = resolve(
        KIND_SKILL,
        _fill_hashes(builtin_candidates() + local_candidates(None, shared_only=True)),
        preferences=registry.preferences(KIND_SKILL),
    )
    report = view_mod.build_view(
        device_view_dir(), _targets(res), allowed_roots=allowed_link_roots()
    )
    _log_report("device", report)
    return report


def rebuild_user_view(user_id: str) -> Optional[view_mod.ViewReport]:
    vdir = user_view_dir(user_id)
    if vdir is None:
        return None
    res = resolve_for_user(user_id)
    report = view_mod.build_view(vdir, _targets(res), allowed_roots=allowed_link_roots())
    _log_report(f"user:{user_id}", report)
    return report


def rebuild_views(user_id: Optional[str]) -> Dict[str, view_mod.ViewReport]:
    reports = {"device": rebuild_device_view()}
    if user_id:
        user_report = rebuild_user_view(user_id)
        if user_report is not None:
            reports["user"] = user_report
    return reports


def _log_report(label: str, report: view_mod.ViewReport) -> None:
    if report.changed:
        logger.info(
            "[caps-view] %s linked=%d relinked=%d removed=%d",
            label,
            len(report.linked),
            len(report.relinked),
            len(report.removed),
        )
    for name, why in report.blocked.items():
        logger.warning("[caps-view] %s name '%s' unavailable: %s", label, name, why)


# ── local publication (device-created / DB-materialized skills) ───────


def publish_local_skill(
    skill_id: str,
    *,
    files: Dict[str, bytes | str],
    content_hash: str,
    owner_user_id: Optional[str] = None,
    source: str = SOURCE_LOCAL,
    source_plugin: Optional[str] = None,
    display_name: str = "",
    description: str = "",
    version: str = "",
    enabled: bool = True,
    from_db: bool = False,
) -> store.StoredComponent:
    """Write one revision of a device-local skill and make it the ready revision."""
    revision = revision_for_hash(content_hash)
    ref = local_ref(KIND_SKILL, skill_id)
    inst = registry.upsert(
        profile_id=LOCAL_PROFILE,
        ref=ref,
        display_name=display_name or skill_id,
        description=description,
        version=version,
        content_hash=content_hash,
        source=source,
        source_plugin=source_plugin,
        payload={"owner_user_id": owner_user_id, "from_db": from_db},
        enabled=enabled,
    )
    comp = store.get(KIND_SKILL, LOCAL_PROFILE, ref.key, revision)
    if comp is None:
        tx = registry.begin_transaction(inst.install_id, inst.generation + 1)
        try:
            comp = store.write_from_files(KIND_SKILL, LOCAL_PROFILE, ref.key, revision, files)
            registry.advance_transaction(tx, "published", inventory=store.inventory(comp))
        except Exception as exc:
            registry.advance_transaction(tx, "failed", error=str(exc))
            registry.set_state(inst.install_id, "failed", last_error=str(exc))
            raise
        registry.set_state(inst.install_id, "ready", resolved_revision=revision)
        registry.advance_transaction(tx, "committed")
    elif inst.resolved_revision != revision or inst.state != "ready":
        registry.set_state(inst.install_id, "ready", resolved_revision=revision)
    # Revisions are retained for durable run snapshots; collection needs reference checks.
    bump_view_generation()
    return comp


def remove_local_skill(skill_id: str) -> bool:
    from .runtime import references

    if references("skill", LOCAL_PROFILE, skill_id):
        # Source deletion revokes use; history still needs its exact bytes.
        return registry.mark_removed(registry.install_id("skill", LOCAL_PROFILE, skill_id))
    iid = registry.install_id(KIND_SKILL, LOCAL_PROFILE, skill_id)
    removed_files = store.remove_key(KIND_SKILL, LOCAL_PROFILE, skill_id) > 0
    removed_row = registry.delete(iid)
    registry.clear_preference(KIND_SKILL, skill_id)
    bump_view_generation()
    return removed_files or removed_row


def prune_local(live_skill_owners: Dict[str, Optional[str]]) -> int:
    """Drop local-profile entries whose database row no longer exists."""
    removed = 0
    for inst in registry.list_installations(kind=KIND_SKILL, profile_id=LOCAL_PROFILE):
        if inst.source in (SOURCE_LOCAL, SOURCE_PLUGIN) and inst.payload.get("from_db"):
            if inst.key not in live_skill_owners:
                registry.mark_removed(inst.install_id)
                removed += 1
    # Unindexed and superseded bytes may still be retained by history snapshots.
    return removed


# ── loader integration: resolver-driven merge of skill sources ─────────


def _info_candidate(info: Any) -> Candidate:
    """Map a backend ``SkillFileInfo`` onto a resolver candidate."""
    src = info.source_name
    origin = dict(getattr(info, "origin", None) or {})
    if src == "built-in":
        return Candidate(
            install_id=registry.install_id(KIND_SKILL, BUILTIN_PROFILE, info.skill_id),
            runtime_name=info.skill_id,
            kind=KIND_SKILL,
            profile=BUILTIN_PROFILE,
            source=SOURCE_BUILTIN,
            path=Path(info.file_path).parent,
            account_level=False,
        )
    if src in ("cloud", "device-store"):
        return Candidate(
            install_id=str(origin.get("install_id") or ""),
            runtime_name=info.skill_id,
            kind=KIND_SKILL,
            profile=str(origin.get("profile") or ""),
            source=SOURCE_CLOUD if src == "cloud" else SOURCE_LOCAL,
            path=Path(info.file_path).parent,
            content_hash=origin.get("content_hash"),
            revision=origin.get("revision"),
            account_level=True,
        )
    if src == "admin":
        # Device catalog row (created here, installed from a plugin or the market).
        return Candidate(
            install_id=registry.install_id(KIND_SKILL, LOCAL_PROFILE, info.skill_id),
            runtime_name=info.skill_id,
            kind=KIND_SKILL,
            profile=LOCAL_PROFILE,
            source=SOURCE_LOCAL,
            path=Path("<db>"),
            account_level=True,
        )
    return Candidate(
        install_id=registry.install_id(KIND_SKILL, src, info.skill_id),
        runtime_name=info.skill_id,
        kind=KIND_SKILL,
        profile=src,
        source=SOURCE_LOCAL,
        path=Path(info.file_path).parent,
        account_level=False,
    )


def merge_skill_infos(groups: Dict[str, List[Any]], *, backend_hash) -> Dict[str, Any]:
    """Composite-backend merge hook: one winner per id, conflicts load nothing.

    ``backend_hash(info) -> str`` computes a content hash for a backend entry; it
    is only called for ids with more than one candidate.
    """
    cands: List[Candidate] = []
    lookup: Dict[str, Any] = {}
    for skill_id, infos in groups.items():
        for info in infos:
            cand = _info_candidate(info)
            if len(infos) > 1 and cand.content_hash is None:
                cand = Candidate(**{**cand.__dict__, "content_hash": backend_hash(info)})
            cands.append(cand)
            lookup[cand.install_id] = info
    res = resolve(
        KIND_SKILL,
        cands,
        preferences=registry.preferences(KIND_SKILL, user_id=current_local_user_id()),
    )
    with _resolution_lock:
        _last_resolution["__loader__"] = res
    for name in res.conflicts:
        logger.warning("[caps] skill name conflict, nothing loaded for '%s'", name)
    return {name: lookup[c.install_id] for name, c in res.chosen.items()}


def loader_resolution() -> Optional[Resolution]:
    with _resolution_lock:
        return _last_resolution.get("__loader__")


def conflicted_names() -> Set[str]:
    res = loader_resolution()
    return set(res.conflicts) if res else set()
