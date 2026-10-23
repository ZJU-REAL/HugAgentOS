"""CE local account bootstrap and web onboarding acceptance."""
from types import SimpleNamespace
import pytest
from core.auth import local_user_bootstrap, local_user_policy, local_users


def configure_local_auth(monkeypatch):
    settings = SimpleNamespace(edition=SimpleNamespace(edition="ce"), auth=SimpleNamespace(password_min_length=8))
    for module in (local_user_bootstrap, local_user_policy, local_users):
        monkeypatch.setattr(module, "settings", settings)
    return settings


def test_ce_default_admin_is_idempotent_and_requires_password_change(db_session, monkeypatch):
    from core.auth.password import verify_password
    from core.db.models import LocalUser, UserShadow

    configure_local_auth(monkeypatch)

    user_id, created = local_user_bootstrap.ensure_ce_default_admin(db_session)
    assert (user_id, created) == ("user_ce_admin", True)

    shadow = db_session.query(UserShadow).filter_by(user_id=user_id).one()
    local = db_session.query(LocalUser).filter_by(user_id=user_id).one()
    assert shadow.username == "admin"
    assert verify_password("admin", local.password_hash)
    assert shadow.extra_data["role"] == "super_admin"
    assert shadow.extra_data["can_add_skill"] is True
    assert shadow.extra_data["can_add_mcp"] is True
    assert shadow.extra_data["can_import_plugin"] is True
    assert shadow.extra_data["can_create_private_kb"] is True
    assert shadow.extra_data["can_create_public_kb"] is False
    assert shadow.extra_data["can_create_channel_bot"] is True
    assert shadow.extra_data["can_switch_model"] is True
    assert shadow.extra_data["can_use_ontology_validation"] is True
    assert shadow.extra_data["must_change_password"] is True
    assert shadow.extra_data["onboarding_required"] is True

    assert local_user_bootstrap.ensure_ce_default_admin(db_session) == (user_id, False)
    result = local_users.LocalUserService(db_session).change_password(
        user_id,
        old_password="admin",
        new_password="new-password-123",
    )
    assert result.ok is True
    db_session.refresh(shadow)
    assert "must_change_password" not in shadow.extra_data
    assert shadow.extra_data["onboarding_required"] is True
    login = local_users.LocalUserService(db_session).authenticate("admin", "new-password-123")
    assert login.ok
    assert login.user_info["onboarding_required"] is True



def test_ce_web_onboarding_requires_main_model_and_clears_checkpoint(db_session, monkeypatch):
    import api.routes.v1.users as user_routes
    from core.auth.backend import UserContext
    from core.models import repository as model_repository
    from core.db.models import UserShadow
    from core.infra.exceptions import ValidationError

    fake_settings = configure_local_auth(monkeypatch)
    monkeypatch.setattr(user_routes, "settings", fake_settings)

    user_id, _ = local_user_bootstrap.ensure_ce_default_admin(db_session)
    local_users.LocalUserService(db_session).change_password(
        user_id,
        old_password="admin",
        new_password="new-password-123",
    )
    user = UserContext(
        user_id=user_id,
        user_center_id=user_id,
        username="admin",
    )

    with pytest.raises(ValidationError, match="主对话模型"):
        user_routes.complete_my_onboarding(user=user, db=db_session)

    provider = model_repository.create_provider(
        db_session,
        display_name="Test chat",
        provider_type="chat",
        base_url="http://model.test/v1",
        api_key="test-key",
        model_name="test-model",
    )
    model_repository.assign_role(db_session, "main_agent", provider.provider_id)

    response = user_routes.complete_my_onboarding(user=user, db=db_session)

    assert response["data"]["onboarding_required"] is False
    shadow = db_session.query(UserShadow).filter_by(user_id=user_id).one()
    assert "onboarding_required" not in shadow.extra_data
    assert shadow.extra_data["onboarding_completed_version"] == 1
