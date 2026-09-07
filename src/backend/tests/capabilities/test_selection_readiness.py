"""Unavailable installed skills cannot win implicit runtime selection."""

from core.capabilities import registry, skills
from tests.capabilities.test_desktop_capabilities_api import client, USER, PROFILE
from tests.capabilities.test_management_readiness import _cloud_skill, _item


def test_incompatible_cloud_candidate_does_not_override_compatible_builtin(
    client, monkeypatch, tmp_path
):
    iid, _ = _cloud_skill(
        client, monkeypatch, "platforms: [not-a-real-platform]", key="platform-choice"
    )
    assert not _item(client, "skill", iid)["usable"]
    root = tmp_path / "builtins"
    path = root / "platform-choice"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "---\nname: platform-choice\ndescription: compatible\n---\nBuiltin\n"
    )
    monkeypatch.setattr(skills, "builtin_dir", lambda: root)
    resolution = skills.resolve_for_user(USER)
    assert resolution.chosen["platform-choice"].profile == "builtin"
    registry.set_preference("skill", "platform-choice", iid, chosen_by=USER)
    selected = skills.resolve_for_user(USER)
    assert "platform-choice" not in selected.chosen
    assert selected.reasons["platform-choice"] == "preferred_not_ready"


def test_explicit_unavailable_request_never_falls_back_to_builtin(client, monkeypatch, tmp_path):
    iid, _ = _cloud_skill(client, monkeypatch, "platforms: [not-a-real-platform]", key="requested")
    path = tmp_path / "builtin" / "requested"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: requested\ndescription: compatible\n---\nBuiltin\n")
    monkeypatch.setattr(skills, "builtin_dir", lambda: path.parent)
    result = skills.resolve_for_user(USER, requested={iid})
    assert "requested" not in result.chosen
    assert result.reasons["requested"] == "requested_not_ready"


def test_missing_local_runtime_is_filtered_from_implicit_defaults(client, monkeypatch):
    iid, _ = _cloud_skill(
        client,
        monkeypatch,
        "dependencies:\n  - kind: pip\n    id: codex-never-installed-selection-37845",
        key="missing-runtime",
    )
    assert "missing-runtime" not in skills.resolve_for_user(USER).chosen
    assert skills.filter_available_names(["missing-runtime"], user_id=USER) == []


def test_selection_does_not_guess_per_run_mcp_authorization(client, monkeypatch):
    iid, _ = _cloud_skill(
        client, monkeypatch, "mcp_servers: [selected-at-runtime]", key="mcp-dependent"
    )
    # Full preparation/preflight still rejects absent grants; candidate eligibility
    # only decides platform/local runtime suitability before the run binds MCPs.
    assert skills.resolve_for_user(USER).chosen["mcp-dependent"].install_id == iid
