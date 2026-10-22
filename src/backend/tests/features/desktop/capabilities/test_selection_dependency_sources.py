"""Implicit defaults and dependency closure share the final source decision."""
from features.desktop.bridge import identity as bridge_identity
from features.desktop.capabilities import skill_account
from features.desktop.capabilities import skill_candidates
from features.desktop.capabilities import skill_paths
from features.desktop.capabilities import skill_publication
from features.desktop.capabilities import skills

from time import monotonic
from types import SimpleNamespace

import pytest

from features.desktop.capabilities import registry, skills, readiness
from features.desktop.bridge import client as bridge
from core.capabilities.manifest_protocol import skill_content_hash
from tests.features.desktop.capabilities.test_desktop_capabilities_api import cloud as cloud
from tests.features.desktop.capabilities.test_desktop_capabilities_api import client, USER, PROFILE, _Cloud


def _publish(key, metadata="", owner=USER):
    text = f"---\nname: {key}\ndescription: local fixture\n{metadata}\n---\nLocal fixture\n"
    skill_publication.publish_local_skill(
        key,
        files={"SKILL.md": text},
        content_hash=skill_content_hash(text, {}),
        owner_user_id=owner,
    )
    return f"skill:local:{key}"


@pytest.mark.parametrize("cached", [False, True])
def test_offline_non_none_catalog_defaults_filter_with_actual_user(client, monkeypatch, cached):
    from core.capabilities.catalog import resolver as catalog_resolver
    from core.capabilities.catalog import overrides as services

    _publish("good-default")
    _publish(
        "bad-default",
        "dependencies:\n  - kind: pip\n    id: codex-never-installed-selection-783465",
    )
    monkeypatch.setattr(bridge_identity, 'bridge_active', lambda: False)
    monkeypatch.setattr(bridge_identity, 'get_state', lambda: None)
    monkeypatch.setattr(skill_account, 'current_local_user_id', lambda: "a-different-user")
    catalog_resolver.invalidate_capability_cache()
    if cached:
        catalog_resolver.process_state().capability_cache[USER] = (
            monotonic() + 60,
            (["good-default", "bad-default"], [], []),
        )
    else:
        monkeypatch.setattr(
            catalog_resolver,
            "get_runtime_catalog",
            lambda *args, **kw: {
                "skills": [
                    {"id": name, "enabled": True} for name in ["good-default", "bad-default"]
                ]
            },
        )
        monkeypatch.setattr(
            services,
            "CatalogService",
            lambda db: SimpleNamespace(get_user_overrides=lambda user: {}),
        )
        monkeypatch.setattr(catalog_resolver, "_owned_enabled_ids", lambda *args: ([], []))
    try:
        enabled, agents, mcps = catalog_resolver.resolve_all_runtime_enabled(object(), USER)
        assert enabled == ["good-default"]
        assert agents == [] and mcps == []
    finally:
        catalog_resolver.invalidate_capability_cache()


def _cloud_dependencies(client, monkeypatch, b_metadata):
    definitions = {
        "dependent-a": "dependencies:\n  - kind: skill\n    id: dependency-b",
        "dependency-b": b_metadata,
    }
    files = {
        key: {
            "SKILL.md": f"---\nname: {key}\ndescription: fixture\n{metadata}\n---\nCloud fixture\n"
        }
        for key, metadata in definitions.items()
    }
    cloud = _Cloud(files)
    monkeypatch.setattr("httpx.get", cloud.get)
    assert client.post("/v1/desktop/capabilities/sync").status_code == 200
    return [f"skill:{PROFILE}:{key}" for key in definitions]


def test_cloud_dependency_uses_explicit_compatible_local_source(client, monkeypatch):
    aid, bid = _cloud_dependencies(client, monkeypatch, "platforms: [not-a-real-platform]")
    local = _publish("dependency-b")
    registry.set_preference("skill", "dependency-b", local, chosen_by=USER)
    result = skills.resolve_for_user(USER)
    assert result.chosen["dependency-b"].install_id == local
    assert result.chosen["dependent-a"].install_id == aid
    # Every root is evaluated independently: the rejected cloud copy must not
    # borrow the ready state of its same-named local replacement.
    evaluated = readiness.eligible_skill_candidates(skill_candidates.candidates(USER), USER)
    assert not next(candidate for candidate in evaluated if candidate.install_id == bid).usable
    report = readiness.file_readiness(registry.get(aid), readiness.context_for_user(USER))
    assert report["ready"]
    assert any(node["install_id"] == local for node in report["nodes"])


def test_dependency_selection_rechecks_parent_after_broken_winner_is_removed(client, monkeypatch):
    aid, bid = _cloud_dependencies(
        client, monkeypatch, "dependencies:\n  - kind: skill\n    id: missing-component"
    )
    local = _publish("dependency-b")
    result = skills.resolve_for_user(USER)
    assert result.chosen["dependency-b"].install_id == local
    assert result.chosen["dependent-a"].install_id == aid


def test_bad_explicit_dependency_source_keeps_parent_blocked(client, monkeypatch):
    aid, bid = _cloud_dependencies(client, monkeypatch, "platforms: [not-a-real-platform]")
    _publish("dependency-b")
    registry.set_preference("skill", "dependency-b", bid, chosen_by=USER)
    result = skills.resolve_for_user(USER)
    assert "dependency-b" not in result.chosen
    assert "dependent-a" not in result.chosen


def test_dependency_uses_runtime_alias_for_opaque_local_installation(client, monkeypatch):
    aid, _ = _cloud_dependencies(client, monkeypatch, "platforms: [not-a-real-platform]")
    local = _publish("opaque-install-key")
    inst = registry.get(local)
    registry.upsert(
        profile_id="local",
        ref=inst.ref,
        content_hash=inst.content_hash,
        source="local",
        payload={**inst.payload, "runtime_name": "dependency-b"},
    )
    registry.set_preference("skill", "dependency-b", local, chosen_by=USER)
    result = skills.resolve_for_user(USER)
    assert result.chosen["dependency-b"].install_id == local
    assert result.chosen["dependent-a"].install_id == aid
    report = readiness.file_readiness(registry.get(aid), readiness.context_for_user(USER))
    assert report["ready"]
    assert any(node["install_id"] == local for node in report["nodes"])


def test_builtin_dependency_is_checked_without_publishing_files(client, monkeypatch, tmp_path):
    from features.desktop.capabilities import store

    root = tmp_path / "unpublished-builtins"
    for key, metadata in [
        ("builtin-a", "dependencies:\n  - kind: skill\n    id: builtin-b"),
        ("builtin-b", ""),
    ]:
        path = root / key
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {key}\ndescription: fixture\n{metadata}\n---\nBuiltin fixture\n"
        )
    monkeypatch.setattr(skill_paths, 'builtin_dir', lambda: root)
    result = skills.resolve_for_user(USER)
    assert set(result.chosen) == {"builtin-a", "builtin-b"}
    assert list(store.iter_components("skill", "builtin")) == []
