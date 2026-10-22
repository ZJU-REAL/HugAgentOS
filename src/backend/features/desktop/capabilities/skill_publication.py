"""skill publication responsibilities."""
from __future__ import annotations
from features.desktop.capabilities import skills
from typing import Dict, Optional
from features.desktop.capabilities import registry, store
from core.capabilities.identity_rules import KIND_SKILL, LOCAL_PROFILE
from features.desktop.capabilities.paths import revision_for_hash
from core.capabilities.reference import local_ref
from features.desktop.capabilities.resolver import SOURCE_LOCAL, SOURCE_PLUGIN
from features.desktop.capabilities import skills

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
    before_generation = registry.generation()
    published = False
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
            published = True
            registry.advance_transaction(tx, "published", inventory=store.inventory(comp))
        except Exception as exc:
            registry.advance_transaction(tx, "failed", error=str(exc))
            registry.set_state(inst.install_id, "failed", last_error=str(exc))
            raise
        registry.set_state(inst.install_id, "ready", resolved_revision=revision)
        registry.advance_transaction(tx, "committed")
    elif inst.resolved_revision != revision or inst.state != "ready":
        registry.set_state(inst.install_id, "ready", resolved_revision=revision)
    # A DB-backed skill is materialized again after restart/cache refresh. An
    # identical publication must not invalidate every user's resolved closure.
    if published or registry.generation() != before_generation:
        skills.bump_view_generation()
    return comp


def remove_local_skill(skill_id: str) -> bool:
    from features.desktop.capabilities.runtime import references

    if references("skill", LOCAL_PROFILE, skill_id):
        # Source deletion revokes use; history still needs its exact bytes.
        return registry.mark_removed(registry.install_id("skill", LOCAL_PROFILE, skill_id))
    iid = registry.install_id(KIND_SKILL, LOCAL_PROFILE, skill_id)
    removed_files = store.remove_key(KIND_SKILL, LOCAL_PROFILE, skill_id) > 0
    removed_row = registry.delete(iid)
    registry.clear_preference(KIND_SKILL, skill_id)
    skills.bump_view_generation()
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
