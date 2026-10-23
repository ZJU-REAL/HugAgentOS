"""Startup reads never include another user's packages or a replaced account."""
from core.capabilities.skills import loader_factory as loader_factory_module
from features.desktop.bridge import identity as bridge_identity
from features.desktop.capabilities import skill_account
from features.desktop.capabilities import skills

from types import SimpleNamespace

from features.desktop.capabilities import warmup, skills, dependency, registry, store
from features.desktop.bridge import client as bridge
from core.capabilities.skills import loader


def test_warmup_filters_identity_and_owner(monkeypatch):
    state = ["a"]
    reads = []
    materialized = []
    monkeypatch.setattr(warmup, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr('features.desktop.capabilities.paths.capabilities_enabled', lambda: True)
    monkeypatch.setattr(bridge_identity, 'get_state', lambda: state[0])
    monkeypatch.setattr(bridge_identity, 'state_fingerprint', lambda value: value)
    monkeypatch.setattr(skill_account, 'current_local_user_id', lambda: "u")
    monkeypatch.setattr(skill_account, 'current_account_profile', lambda: "account-a")
    monkeypatch.setattr(skill_account, 'account_authorized_for', lambda uid: uid == "u")
    monkeypatch.setattr(
        skills, 'resolve_for_user', lambda uid: SimpleNamespace(chosen={"own": None, "other": None})
    )
    monkeypatch.setattr(
        loader_factory_module,
        'get_skill_loader',
        lambda: SimpleNamespace(
            get_skill_owner=lambda name: "u" if name == "own" else "v",
            get_skill_dir=materialized.append,
        ),
    )

    def row(key, profile="account-a", owner="u", enabled=True):
        return SimpleNamespace(
            kind="skill",
            ready=True,
            enabled=enabled,
            profile_id=profile,
            key=key,
            resolved_revision="rev",
            payload={"owner_user_id": owner},
        )

    monkeypatch.setattr(
        registry,
        "list_installations",
        lambda: [
            row("own"),
            row("other", owner="v"),
            row("old", profile="account-b"),
            row("disabled", enabled=False),
        ],
    )
    monkeypatch.setattr(store, "get", lambda kind, profile, key, rev: SimpleNamespace(path=key))
    monkeypatch.setattr(
        dependency, "component_hash", lambda component, fresh: reads.append((component.path, fresh))
    )
    monkeypatch.setattr(dependency, "skill_definition", lambda path: {})
    warmup.warmup_current_account("a")
    assert materialized == []
    assert reads == [("own", True)]
    state[0] = "b"
    warmup.warmup_current_account("a")
    assert materialized == [] and reads == [("own", True)]
