"""Cloud definition grants are checked again after an online checkpoint."""

import pytest
from core.capabilities import agents, registry, runtime, skills, store
from core.capabilities.errors import PermissionDenied
from core.capabilities.ref import cloud_ref, profile_id
from core.capabilities.paths import revision_for_hash
from core.services import desktop_cloud_bridge as bridge
from tests.capabilities.test_runtime_recovery import durable_index, state


@pytest.mark.parametrize("revoke", ["remove", "disable"])
def test_cloud_definition_revoked_during_creation_cannot_be_pinned(
    durable_index, caps_root, monkeypatch, revoke
):
    st = state("a")
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "owner")
    definition = agents.AgentDefinition(
        agent_id="private", name="Private", system_prompt="private content"
    )
    digest = definition.content_hash()
    profile = profile_id(st["cloud_base"], "a")
    inst = registry.upsert(
        profile_id=profile,
        ref=cloud_ref(st["cloud_base"], "agent", "private", scope="private"),
        content_hash=digest,
    )
    comp = store.write_from_files(
        "agent", profile, "private", revision_for_hash(digest), definition.to_files()
    )
    registry.set_state(inst.install_id, "ready", resolved_revision=comp.revision)
    loaded = agents.load_definition(comp, origin="cloud")
    monkeypatch.setattr(
        bridge,
        "ensure_current_authorization",
        lambda: (
            registry.mark_removed(inst.install_id)
            if revoke == "remove"
            else registry.set_enabled(inst.install_id, False)
        ),
    )
    with pytest.raises(PermissionDenied):
        runtime.pin_agent_definition("revoked-before-run", "owner", loaded)
