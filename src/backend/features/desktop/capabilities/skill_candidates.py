"""skill candidates responsibilities."""
from __future__ import annotations
from features.desktop.capabilities import registry_types
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Dict, List, Optional, Tuple
from features.desktop.capabilities import registry, store
from core.capabilities.identity_rules import BUILTIN_PROFILE, KIND_SKILL, LOCAL_PROFILE
from core.capabilities.reference import builtin_ref
from features.desktop.capabilities.resolver import SOURCE_BUILTIN, Candidate
from features.desktop.capabilities import skill_account
from features.desktop.capabilities import skill_hashes
from features.desktop.capabilities import skill_paths

def builtin_candidates() -> List[Candidate]:
    """安装包自带的技能。

    混合模式下它们不进能力中心、也不会被启用（目录层在 ``get_runtime_catalog`` 里就清空
    了），但**候选必须留着**：云端技能会声明对自带技能的依赖，自带的那份还是云端副本在
    本平台跑不了时的兜底，候选一空这两条链路就断了。
    """
    root = skill_paths.builtin_dir()
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


def installation_candidate(inst: registry_types.Installation, *, account_level: bool) -> Candidate:
    path = None
    if inst.ready:
        from core.harness.capability_errors import CapabilityError

        try:
            comp = store.get(KIND_SKILL, inst.profile_id, inst.key, inst.resolved_revision or "")
        except (CapabilityError, OSError):
            comp = None
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


def installation_candidates(rows):
    rows = list(rows)
    if os.name == "nt" and len(rows) >= 8:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="cap-candidates") as pool:
            futures = [
                pool.submit(copy_context().run, installation_candidate, row, account_level=True)
                for row in rows
            ]
            return [future.result() for future in futures]
    return [installation_candidate(row, account_level=True) for row in rows]


def local_candidates(user_id: Optional[str], *, shared_only: bool = False) -> List[Candidate]:
    rows = []
    for inst in registry.list_installations(kind=KIND_SKILL, profile_id=LOCAL_PROFILE):
        owner = inst.payload.get("owner_user_id")
        if owner and (shared_only or owner != user_id):
            continue
        rows.append(inst)
    return installation_candidates(rows)


def account_candidates(profile: Optional[str] = None) -> List[Candidate]:
    profile = profile or skill_account.current_account_profile()
    if not profile:
        return []
    return installation_candidates(
        registry.list_installations(kind=KIND_SKILL, profile_id=profile)
    )


def candidates(user_id: Optional[str]) -> List[Candidate]:
    return (
        builtin_candidates()
        + local_candidates(user_id)
        + (account_candidates() if skill_account.account_authorized_for(user_id) else [])
    )


def fill_hashes(cands: List[Candidate]) -> List[Candidate]:
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
                c = Candidate(**{**c.__dict__, "content_hash": skill_hashes.skill_dir_hash(c.path)})
            out.append(c)
    return out


def builtin_signature() -> Tuple:
    """Shipped built-ins live on disk, outside every write signal — list them."""
    root = skill_paths.builtin_dir()
    try:
        return (str(root), tuple(sorted(p.name for p in root.iterdir() if p.is_dir())))
    except OSError:
        return (str(root), ())
