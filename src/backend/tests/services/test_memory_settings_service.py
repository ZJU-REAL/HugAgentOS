"""Tests for memory availability and default-on user settings."""

import pytest
from core.db import model_repository
from core.db.models import UserShadow
from core.infra.exceptions import BadRequestError
from core.services.memory_settings_service import MemorySettingsService
from core.services.user_service import UserService


def _create_user(db_session, *, extra_data=None) -> UserShadow:
    user = UserShadow(
        user_id="memory-user",
        username="Memory User",
        extra_data=extra_data or {},
    )
    db_session.add(user)
    db_session.commit()
    return user


def _assign_embedding(db_session, *, active: bool = True) -> None:
    provider = model_repository.create_provider(
        db_session,
        display_name="Test embedding",
        provider_type="embedding",
        base_url="http://embedding.test/v1",
        api_key="test-key",
        model_name="test-embedding",
        is_active=active,
    )
    model_repository.assign_role(db_session, "embedding", provider.provider_id)


def test_memory_defaults_off_and_enable_is_rejected_without_embedding(
    db_session,
    monkeypatch,
):
    _create_user(db_session)
    monkeypatch.setattr(
        "core.services.memory_settings_service._memory_runtime_available",
        lambda: True,
    )

    settings = UserService(db_session).get_user_settings("memory-user")

    assert settings["memory_enabled"] is False
    assert settings["memory_write_enabled"] is False
    with pytest.raises(BadRequestError, match="embedding"):
        MemorySettingsService(db_session).validate_patch({"memory_enabled": True})


def test_memory_defaults_on_when_runtime_and_embedding_are_available(
    db_session,
    monkeypatch,
):
    _create_user(db_session)
    _assign_embedding(db_session)
    monkeypatch.setattr(
        "core.services.memory_settings_service._memory_runtime_available",
        lambda: True,
    )

    settings = UserService(db_session).get_user_settings("memory-user")

    assert settings["memory_enabled"] is True
    assert settings["memory_write_enabled"] is True
    MemorySettingsService(db_session).validate_patch(
        {"memory_enabled": True, "memory_write_enabled": True}
    )


def test_explicit_memory_off_is_preserved(db_session, monkeypatch):
    _create_user(
        db_session,
        extra_data={"memory_enabled": False, "memory_write_enabled": False},
    )
    _assign_embedding(db_session)
    monkeypatch.setattr(
        "core.services.memory_settings_service._memory_runtime_available",
        lambda: True,
    )

    settings = UserService(db_session).get_user_settings("memory-user")

    assert settings["memory_enabled"] is False
    assert settings["memory_write_enabled"] is False


def test_inactive_embedding_provider_does_not_unlock_memory(db_session, monkeypatch):
    _create_user(db_session)
    _assign_embedding(db_session, active=False)
    monkeypatch.setattr(
        "core.services.memory_settings_service._memory_runtime_available",
        lambda: True,
    )

    availability = MemorySettingsService(db_session).availability()

    assert availability == {
        "mem0_available": True,
        "embedding_available": False,
        "memory_available": False,
    }


def test_missing_memory_runtime_rejects_enable(db_session, monkeypatch):
    _create_user(db_session)
    _assign_embedding(db_session)
    monkeypatch.setattr(
        "core.services.memory_settings_service._memory_runtime_available",
        lambda: False,
    )

    with pytest.raises(BadRequestError, match="记忆服务"):
        MemorySettingsService(db_session).validate_patch({"memory_write_enabled": True})
