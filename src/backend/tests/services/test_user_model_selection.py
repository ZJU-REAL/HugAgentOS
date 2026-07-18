import pytest
from sqlalchemy.orm import sessionmaker

from core.db.models import ModelProvider, ModelRoleAssignment, UserShadow
from core.services.model_config import ModelConfigService
from core.services.user_model_selection import (
    UserModelSelectionError,
    list_user_selectable_models,
    resolve_effective_chat_model_name,
    resolve_user_model_provider_id,
    user_can_switch_model,
)


def _provider(
    provider_id: str,
    *,
    display_name: str,
    provider_type: str = "chat",
    is_active: bool = True,
) -> ModelProvider:
    return ModelProvider(
        provider_id=provider_id,
        display_name=display_name,
        provider_type=provider_type,
        provider="openai_compatible",
        base_url="http://model.local/v1",
        api_key="test-key",
        model_name=f"{provider_id}-model",
        extra_config={},
        is_active=is_active,
    )


def test_list_user_selectable_models_filters_active_chat_and_marks_default(db_session):
    db_session.add_all(
        [
            _provider("p_main", display_name="Main"),
            _provider("p_alt", display_name="Alt"),
            _provider("p_inactive", display_name="Inactive", is_active=False),
            _provider("p_embed", display_name="Embedding", provider_type="embedding"),
            ModelRoleAssignment(role_key="main_agent", provider_id="p_main"),
        ]
    )
    db_session.commit()

    rows = list_user_selectable_models(db_session)

    assert {row["provider_id"] for row in rows} == {"p_main", "p_alt"}
    assert next(row for row in rows if row["provider_id"] == "p_main")["is_default"] is True
    assert next(row for row in rows if row["provider_id"] == "p_alt")["is_default"] is False
    assert all("api_key" not in row and "base_url" not in row for row in rows)


def test_resolve_user_model_provider_id_respects_switch_and_allowlist(db_session):
    db_session.add(_provider("p_chat", display_name="Chat"))
    db_session.add(_provider("p_embed", display_name="Embedding", provider_type="embedding"))
    db_session.commit()

    assert resolve_user_model_provider_id(db_session, "p_chat", can_switch_model=False) is None
    assert resolve_user_model_provider_id(db_session, "p_chat", can_switch_model=True) == "p_chat"

    with pytest.raises(UserModelSelectionError):
        resolve_user_model_provider_id(db_session, "p_embed", can_switch_model=True)

    with pytest.raises(UserModelSelectionError):
        resolve_user_model_provider_id(db_session, "missing", can_switch_model=True)


def test_resolve_effective_chat_model_name_uses_runtime_model_config(db_session, monkeypatch):
    import core.services.model_config as model_config_module

    db_session.add_all(
        [
            _provider("p_main", display_name="Main"),
            _provider("p_alt", display_name="Alt"),
            ModelRoleAssignment(role_key="main_agent", provider_id="p_main"),
        ]
    )
    db_session.commit()

    TestSessionLocal = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(model_config_module, "SessionLocal", TestSessionLocal)
    ModelConfigService.get_instance().invalidate_cache()

    assert resolve_effective_chat_model_name("p_alt", fallback_model_name="qwen") == "p_alt-model"
    assert resolve_effective_chat_model_name(fallback_model_name="qwen") == "p_main-model"


def test_user_can_switch_model_uses_user_capability(db_session):
    db_session.add(
        UserShadow(
            user_id="u1",
            user_center_id="uc1",
            username="alice",
            extra_data={},
        )
    )
    db_session.commit()

    assert user_can_switch_model(db_session, "u1") is False

    shadow = db_session.query(UserShadow).filter(UserShadow.user_id == "u1").one()
    shadow.extra_data = {"can_switch_model": True}
    db_session.commit()

    assert user_can_switch_model(db_session, "u1") is True
