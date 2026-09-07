"""Identity authorization cannot be relaxed for legacy tokens or another request user."""

from pathlib import Path
import pytest
from core.capabilities import connectors, registry, skills
from core.capabilities.resolver import Candidate
from core.services import desktop_cloud_bridge as bridge


@pytest.mark.parametrize("token", ["dcap1.legacy.sig", "opaque", ""])
def test_legacy_token_never_authorizes_even_with_matching_identity(monkeypatch, token):
    monkeypatch.setattr(bridge, "get_state", lambda: {"token": token})
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "a")
    assert not skills.account_authorized_for("a")


def test_cloud_authorization_requires_live_state_and_matching_request_user(monkeypatch):
    monkeypatch.setattr(bridge, "get_state", lambda: {"token": "dcap2.test.sig"})
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "a")
    assert skills.account_authorized_for("a")
    assert not skills.account_authorized_for("b")
    assert not skills.account_authorized_for(None)
    monkeypatch.setattr(bridge, "get_state", lambda: None)
    assert not skills.account_authorized_for("a")


def test_connector_explicit_request_user_controls_only_that_users_preference(index_db, monkeypatch):
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "b")
    candidates = [
        Candidate(
            install_id="mcp:local:" + key,
            runtime_name="same",
            kind="mcp",
            profile="local",
            source="local",
            path=Path("<db>"),
        )
        for key in ("a", "b")
    ]
    registry.set_preference("mcp", "same", "mcp:local:a", chosen_by="a")
    registry.set_preference("mcp", "same", "mcp:local:b", chosen_by="b")
    assert (
        connectors.resolve_bindings(candidates, keep_local=set(), user_id="a")
        .chosen["same"]
        .install_id
        == "mcp:local:a"
    )
    assert (
        connectors.resolve_bindings(candidates, keep_local=set()).chosen["same"].install_id
        == "mcp:local:b"
    )
