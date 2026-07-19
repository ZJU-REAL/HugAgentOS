"""Regression tests for CE-only runtime seams."""

from types import SimpleNamespace

from api.routes.v1.auth import router as auth_router
from api.routes.v1.users import router as users_router
from cli import DEFAULT_LOCAL_CONTEXT_LENGTH, build_parser, configure_model
from core.db import model_repository
from core.licensing import license_manager


def test_ce_license_never_blocks_business_routes():
    assert license_manager.mode() == "ce"
    assert license_manager.is_active() is True


def test_ce_auth_router_keeps_local_session_contract():
    paths = {route.path for route in auth_router.routes}
    assert {
        "/v1/auth/ticket/exchange",
        "/v1/auth/session/check",
        "/v1/auth/logout",
    } <= paths


def test_ce_user_router_exposes_self_service_password_change():
    paths = {route.path for route in users_router.routes}
    assert "/v1/me/password" in paths


def test_onboard_cli_has_safe_context_window_default():
    args = build_parser().parse_args(["onboard"])
    assert args.model_context_length == DEFAULT_LOCAL_CONTEXT_LENGTH
    assert args.host == "127.0.0.1"


def test_serve_cli_accepts_explicit_public_bind():
    args = build_parser().parse_args(["serve", "--host", "0.0.0.0"])
    assert args.host == "0.0.0.0"


def test_configure_chat_model_persists_context_window(monkeypatch):
    from core.db import engine
    from core.services.model_config import ModelConfigService

    class FakeDb:
        closed = False

        def close(self):
            self.closed = True

    db = FakeDb()
    captured = {}
    invalidated = []

    def fake_create_provider(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(provider_id="provider-test")

    monkeypatch.setattr(engine, "SessionLocal", lambda: db)
    monkeypatch.setattr(model_repository, "create_provider", fake_create_provider)
    monkeypatch.setattr(model_repository, "assign_role", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ModelConfigService,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(invalidate_cache=lambda: invalidated.append(True))),
    )

    configure_model(
        "http://model.example/v1",
        "test-key",
        "test-model",
        test=False,
        context_length=65536,
    )

    assert captured["extra_config"] == {"context_length": 65536}
    assert db.closed is True
    assert invalidated == [True]
