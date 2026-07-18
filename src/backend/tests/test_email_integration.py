"""Email (email / himalaya) integration unit tests: pure functions (provider
detection / config rendering), app-password encryption round trip, model/repository,
connect/disconnect sync flow (stubbed _verify, no real mail server connection),
credentials-volume degradation, plugin installability, route e2e. Real sandbox
connections are left for verification on a real machine."""

import base64

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
import core.db.models  # noqa: F401  ensures all models are registered (FK depends on users_shadow)
from core.db.models import UserShadow, EmailConnection
from core.db.repository import EmailConnectionRepository
from core.services import email_service as es
from core.services.email_service import (
    EmailService,
    detect_provider,
    render_config_toml,
    get_email_config_bundle,
)


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(UserShadow(user_id="u1", username="alice"))
    s.commit()
    yield s
    s.close()


def _redirect_storage(monkeypatch, tmp_path):
    """Redirect the email_* path helpers to a temp directory (settings.storage is frozen and cannot be monkeypatched).
    email_service internally resolves these functions at call time via
    ``from core.sandbox._common import ...``, so patching the module attributes
    is enough to take effect."""
    import core.sandbox._common as common

    def cache(uid):
        return tmp_path / "email_cache" / uid

    monkeypatch.setattr(common, "email_cache_dir", cache)
    monkeypatch.setattr(common, "email_home_dir", lambda uid: cache(uid) / "home")
    monkeypatch.setattr(
        common, "email_himalaya_config",
        lambda uid: cache(uid) / "home" / ".config" / "himalaya" / "config.toml",
    )


# ── Provider auto-detection ──────────────────────────────────────────────
def test_detect_provider_known():
    g = detect_provider("Alice@Gmail.com")
    assert g["provider"] == "gmail"
    assert g["imap_host"] == "imap.gmail.com" and g["imap_port"] == 993
    assert g["smtp_host"] == "smtp.gmail.com"
    o = detect_provider("bob@outlook.com")
    assert o["provider"] == "outlook" and o["smtp_security"] == "starttls"
    q = detect_provider("x@exmail.qq.com")
    assert q["provider"] == "exmail"


def test_detect_provider_unknown():
    assert detect_provider("user@self-hosted.example") is None
    assert detect_provider("notanemail") is None
    assert detect_provider("") is None


# ── config.toml rendering (v1.2.0 schema) ────────────────────────────────
def test_render_config_keys_and_starttls_mapping():
    toml = render_config_toml(
        email_address="a@b.com", display_name="A", app_password="pw",
        imap_host="imap.b.com", imap_port=993, imap_security="tls",
        smtp_host="smtp.b.com", smtp_port=587, smtp_security="starttls",
    )
    assert "[accounts.default]" in toml
    assert 'email = "a@b.com"' in toml
    assert 'display-name = "A"' in toml
    assert 'backend.type = "imap"' in toml
    assert 'backend.encryption.type = "tls"' in toml
    assert "backend.port = 993" in toml
    assert 'backend.auth.raw = "pw"' in toml
    assert 'message.send.backend.type = "smtp"' in toml
    assert "message.send.backend.port = 587" in toml
    # starttls must map to himalaya's start-tls
    assert 'message.send.backend.encryption.type = "start-tls"' in toml


def test_render_config_escapes_special_chars():
    toml = render_config_toml(
        email_address="a@b.com", display_name=None, app_password='p"w\\x',
        imap_host="h", imap_port=993, imap_security="none",
        smtp_host="h", smtp_port=465, smtp_security="tls",
    )
    # Backslash and double-quote escaping so the app password doesn't break the TOML
    assert 'backend.auth.raw = "p\\"w\\\\x"' in toml
    # display-name defaults back to the email
    assert 'display-name = "a@b.com"' in toml
    assert 'backend.encryption.type = "none"' in toml


# ── App-password encryption round trip ───────────────────────────────────
def test_crypto_round_trip():
    from core.infra.crypto import encrypt_secret, decrypt_secret

    tok = encrypt_secret("s3cr3t-app-pw")
    assert tok and tok != "s3cr3t-app-pw"          # not plaintext
    assert decrypt_secret(tok) == "s3cr3t-app-pw"  # decrypts back
    assert decrypt_secret(None) is None
    assert decrypt_secret("not-a-valid-token") is None  # corrupted data degrades to None


# ── Model + repository ───────────────────────────────────────────────────
def test_repo_ensure_and_update(db):
    repo = EmailConnectionRepository(db)
    rec = repo.ensure("u1")
    assert rec.status == "disconnected"
    assert repo.ensure("u1").user_id == "u1"  # idempotent
    repo.update("u1", {"status": "connected", "email_address": "a@b.com", "imap_port": 993})
    rec2 = repo.get("u1")
    assert rec2.status == "connected" and rec2.email_address == "a@b.com" and rec2.imap_port == 993


def test_status_dict_shape(db):
    data = EmailService(db).get_status("u1")
    assert data["status"] == "disconnected"
    for k in ("email_address", "provider", "imap_host", "smtp_host", "last_error"):
        assert k in data
    # Email has no device flow — QR-code / verification-URL fields must never appear
    assert "qr_data_uri" not in data
    assert "verification_url" not in data


# ── connect sync flow (stubbed _verify, no real server connection) ───────
@pytest.mark.asyncio
async def test_connect_success(monkeypatch, db, tmp_path):
    async def fake_verify(user_id):
        return True, ""
    monkeypatch.setattr(es, "_verify", fake_verify)
    _redirect_storage(monkeypatch, tmp_path)

    svc = EmailService(db)
    data = await svc.connect("u1", email_address="alice@gmail.com", secret="app-pw", display_name="Alice")
    assert data["status"] == "connected"
    assert data["provider"] == "gmail"
    assert data["imap_host"] == "imap.gmail.com"
    assert data["last_error"] is None

    rec = EmailConnectionRepository(db).get("u1")
    assert rec.secret_enc and rec.secret_enc != "app-pw"   # app password is encrypted
    assert rec.config_bundle                                # portable config bundle is stored
    decoded = base64.b64decode(rec.config_bundle).decode("utf-8")
    assert 'backend.auth.raw = "app-pw"' in decoded
    # config.toml is actually written under that user's HOME
    from core.sandbox._common import email_himalaya_config
    assert email_himalaya_config("u1").exists()


@pytest.mark.asyncio
async def test_connect_custom_needs_server(monkeypatch, db, tmp_path):
    _redirect_storage(monkeypatch, tmp_path)

    svc = EmailService(db)
    # Unknown domain with no server given → error guiding manual entry; must not attempt verification
    data = await svc.connect("u1", email_address="user@self-hosted.example", secret="pw")
    assert data["status"] == "error"
    assert "服务器" in (data["last_error"] or "")


@pytest.mark.asyncio
async def test_connect_verify_failure_purges(monkeypatch, db, tmp_path):
    async def fake_verify(user_id):
        return False, "AUTHENTICATIONFAILED"
    monkeypatch.setattr(es, "_verify", fake_verify)
    _redirect_storage(monkeypatch, tmp_path)

    svc = EmailService(db)
    data = await svc.connect("u1", email_address="alice@gmail.com", secret="wrong")
    assert data["status"] == "error"
    assert "AUTHENTICATIONFAILED" in (data["last_error"] or "")
    rec = EmailConnectionRepository(db).get("u1")
    assert rec.secret_enc is None and rec.config_bundle is None  # no credentials kept on failure
    from core.sandbox._common import email_himalaya_config
    assert not email_himalaya_config("u1").exists()             # bad config was purged


@pytest.mark.asyncio
async def test_connect_validates_input(db):
    svc = EmailService(db)
    assert (await svc.connect("u1", email_address="bad", secret="x"))["status"] == "error"
    assert (await svc.connect("u1", email_address="a@b.com", secret=""))["status"] == "error"


@pytest.mark.asyncio
async def test_disconnect_clears(monkeypatch, db, tmp_path):
    async def fake_verify(user_id):
        return True, ""
    monkeypatch.setattr(es, "_verify", fake_verify)
    _redirect_storage(monkeypatch, tmp_path)

    svc = EmailService(db)
    await svc.connect("u1", email_address="alice@gmail.com", secret="app-pw")
    data = await svc.disconnect("u1")
    assert data["status"] == "disconnected"
    assert data["email_address"] is None
    rec = EmailConnectionRepository(db).get("u1")
    assert rec.secret_enc is None and rec.config_bundle is None
    from core.sandbox._common import email_himalaya_config
    assert not email_himalaya_config("u1").exists()


# ── Config-bundle export (for cube injection) ────────────────────────────
@pytest.mark.asyncio
async def test_config_bundle_export(monkeypatch, db, tmp_path):
    async def fake_verify(user_id):
        return True, ""
    monkeypatch.setattr(es, "_verify", fake_verify)
    _redirect_storage(monkeypatch, tmp_path)

    svc = EmailService(db)
    await svc.connect("u1", email_address="alice@gmail.com", secret="app-pw")
    # get_email_config_bundle uses its own SessionLocal — here we read the repo's bundle directly to verify its shape
    rec = EmailConnectionRepository(db).get("u1")
    decoded = base64.b64decode(rec.config_bundle).decode("utf-8")
    assert "[accounts.default]" in decoded


# ── Credentials-volume degradation + paths ───────────────────────────────
def test_creds_volume_degrades_without_host_storage(monkeypatch):
    from types import SimpleNamespace
    from core.sandbox import _opensandbox_internals as internals

    # Explicitly simulate "HOST_STORAGE_PATH not configured" — don't depend on the
    # runtime environment's .env (a dev machine may actually set HOST_STORAGE_PATH,
    # which would make this assertion misjudge). settings is frozen, attributes
    # can't be changed, so swap the module-level settings reference for a stub
    # carrying only the required fields.
    monkeypatch.setattr(
        internals, "settings",
        SimpleNamespace(sandbox=SimpleNamespace(
            email_creds_bind_mount_enabled=True,
            opensandbox_host_storage_path="",
        )),
    )
    assert internals._make_email_creds_volumes("u1") == []  # quiet degradation without HOST_STORAGE_PATH


def test_creds_volume_rejects_bad_user_id():
    from core.sandbox._opensandbox_internals import _make_email_creds_volumes
    assert _make_email_creds_volumes("") == []
    assert _make_email_creds_volumes("../etc/passwd") == []


def test_email_cache_dir_path():
    from core.sandbox._common import email_cache_dir, email_himalaya_config
    p = email_cache_dir("u_abc")
    assert p.name == "u_abc" and p.parent.name == "email_cache"
    cfg = email_himalaya_config("u_abc")
    assert cfg.name == "config.toml" and "himalaya" in str(cfg)


def test_settings_email_arch_flag():
    from core.config.settings import settings
    assert settings.sandbox.email_creds_bind_mount_enabled is True


# ── Plugin installability ────────────────────────────────────────────────
def test_email_plugin_installable(db):
    """Email plugin: discovered by list → declares connection=email and contains the
    email skill → install lands an AdminSkill (source_plugin=email) → detail returns
    connection so the frontend renders the account-connection panel on the plugin
    detail page."""
    from core.db.models import AdminSkill
    from core.services import plugin_service as ps

    items = ps.list_plugins(db, owner_user_id="u1")
    em = next((it for it in items if it["slug"] == "email"), None)
    assert em is not None, "email 未出现在插件列表"
    assert em["installed"] is False

    detail = ps.get_plugin_detail("email")
    assert detail.get("connection") == "email"

    res = ps.install_plugin(db, "email", owner_user_id="u1")
    assert res["action"] == "installed"
    sk = db.query(AdminSkill).filter(AdminSkill.source_plugin == "email").all()
    assert len(sk) == 1

    inst = ps.get_installed_detail(db, res["install_id"], owner_user_id="u1")
    assert inst.get("connection") == "email"


# ── Route e2e ────────────────────────────────────────────────────────────
def test_email_routes_e2e(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool

    from api.app import app
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db

    async def fake_verify(user_id):
        return True, ""
    monkeypatch.setattr(es, "_verify", fake_verify)
    _redirect_storage(monkeypatch, tmp_path)

    # StaticPool + check_same_thread=False: a single shared connection, so when
    # TestClient runs requests on other threads it still sees the same in-memory DB
    # with the tables created (plain :memory: gives each thread its own empty
    # connection → no such table).
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    seed = Session()
    seed.add(UserShadow(user_id="u1", username="alice"))
    seed.commit()
    seed.close()

    def _override_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="u1", user_center_id="c1", username="alice", email="a@e.com",
    )
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        r = client.get("/v1/integrations/email/status")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "disconnected"

        r = client.post("/v1/integrations/email/connect", json={
            "email_address": "alice@gmail.com", "secret": "app-pw", "display_name": "Alice",
        })
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == "connected"

        r = client.post("/v1/integrations/email/disconnect")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "disconnected"
    finally:
        app.dependency_overrides.clear()
