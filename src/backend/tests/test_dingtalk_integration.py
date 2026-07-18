"""DingTalk (dingtalk / dws) integration unit tests: pure-function parsing, PAT detection,
models/repositories, settings, credential-volume degradation, marketplace skill
installability. No dependence on real DingTalk/sandbox — the sandbox orchestration part
is left for P0 real-machine verification."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
import core.db.models  # noqa: F401  ensure all models are registered (FK depends on users_shadow)
from core.db.models import UserShadow, DingTalkConnection, AdminSkill
from core.db.repository import DingTalkConnectionRepository
from core.services.dingtalk_service import (
    parse_device_login_output,
    parse_auth_status,
    parse_get_self,
)

# sandbox_tool pulls in agentscope via core.llm.__init__; skip related cases when
# agentscope is absent locally — they run normally inside the container (the real test environment).
try:
    from core.llm.tools.sandbox_tool import _detect_dws_pat_authorization
    _HAS_SANDBOX_TOOL = True
except ModuleNotFoundError:
    _HAS_SANDBOX_TOOL = False


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(UserShadow(user_id="u1", username="alice"))
    s.commit()
    yield s
    s.close()


# ── Device-flow output parsing ────────────────────────────────────────────
def test_parse_device_login_box():
    # Real shape of the dws device-code box: link + auth code + "or open directly" full link with the code embedded
    t = (
        "请在浏览器中打开以下链接，并输入授权码：\n"
        "  链接: https://login.dingtalk.com/oauth/device\n"
        "  授权码: WXYZ-1234\n"
        "或者直接打开以下链接：\n"
        "  https://login.dingtalk.com/oauth/device?user_code=WXYZ-1234\n"
        "授权码将在 600 秒后过期。"
    )
    url, code, complete = parse_device_login_output(t)
    assert url == "https://login.dingtalk.com/oauth/device"
    assert code == "WXYZ-1234"
    assert complete == "https://login.dingtalk.com/oauth/device?user_code=WXYZ-1234"


def test_parse_device_login_json():
    t = '{"verificationUri":"https://oapi.dingtalk.com/c","userCode":"7Q2K9F","verificationUriComplete":"https://oapi.dingtalk.com/c?user_code=7Q2K9F"}'
    url, code, complete = parse_device_login_output(t)
    assert url == "https://oapi.dingtalk.com/c"
    assert code == "7Q2K9F"
    assert complete == "https://oapi.dingtalk.com/c?user_code=7Q2K9F"


def test_parse_device_login_empty():
    assert parse_device_login_output("") == (None, None, None)
    assert parse_device_login_output("无链接无码") == (None, None, None)


# ── auth status / get-self ────────────────────────────────────────────────
@pytest.mark.parametrize("blob,expect", [
    ('{"success":true,"authenticated":true}', True),
    ('{"authenticated":false,"message":"未登录"}', False),
    ('not json at all', False),
    ('', False),
    ('[1,2,3]', False),
])
def test_parse_auth_status(blob, expect):
    assert parse_auth_status(blob) is expect


def test_parse_get_self_nested():
    blob = json.dumps({"result": [{"orgEmployeeModel": {
        "orgUserName": "张三", "userId": "u12345", "corpId": "ding9988"}}]})
    out = parse_get_self(blob)
    assert out == {"dingtalk_user_id": "u12345", "dingtalk_name": "张三", "corp_id": "ding9988"}


def test_parse_get_self_garbage():
    assert parse_get_self("oops") == {
        "dingtalk_user_id": None, "dingtalk_name": None, "corp_id": None}


# ── verify_and_refresh: verdict via real API probing (does not trust local auth status) ──
@pytest.mark.asyncio
@pytest.mark.parametrize("out,rc,expect", [
    # Identity fetched successfully → valid (this call also makes the backend HOME refresh + rotate in place)
    (json.dumps({"result": [{"orgEmployeeModel": {"orgUserName": "张三", "userId": "u1"}}]}), 0, "valid"),
    # Explicit auth failure (category=auth / reason=not_authenticated) → invalid
    (json.dumps({"error": {"category": "auth", "reason": "not_authenticated",
                           "message": "未登录，请先执行 dws auth login"}}), 1, "invalid"),
    # reason matches the failure set but category is missing → still invalid
    (json.dumps({"error": {"reason": "token_expired"}}), 1, "invalid"),
    # Non-auth errors (rate limiting / server-side 5xx) → unknown, no conclusion drawn
    (json.dumps({"error": {"category": "rate_limit", "reason": "too_many_requests"}}), 1, "unknown"),
    # Unparsable / empty output / dws missing → unknown
    ("not json", 0, "unknown"),
    ("", 124, "unknown"),
])
async def test_verify_and_refresh_verdict(monkeypatch, out, rc, expect):
    import core.services.dingtalk_service as ds

    async def fake_run_dws(user_id, args, timeout=40):
        return out, "", rc

    monkeypatch.setattr(ds, "_run_dws", fake_run_dws)
    assert await ds.verify_and_refresh("u1") == expect


def test_mark_login_expired_sets_disconnected(monkeypatch):
    # mark_login_expired writes the DB with an independent SessionLocal; stub the underlying _update_connection to verify the persisted fields.
    import core.services.dingtalk_service as ds

    captured = {}
    monkeypatch.setattr(ds, "_update_connection",
                        lambda user_id, data: captured.update(user_id=user_id, data=data))
    ds.mark_login_expired("u1")
    assert captured["user_id"] == "u1"
    assert captured["data"]["status"] == "disconnected"
    assert captured["data"]["last_error"] == ds._EXPIRED_ERROR


# ── PAT authorization-intercept detection ─────────────────────────────────
@pytest.mark.skipif(not _HAS_SANDBOX_TOOL, reason="agentscope 未安装（仅容器内测）")
def test_detect_pat_from_stderr():
    pat = _detect_dws_pat_authorization(
        4, "", "权限不足\nPAT_AUTHORIZATION_URL=https://open-dev.dingtalk.com/auth?x=1&userCode=ABC\n")
    assert pat is not None
    assert pat["authorization_url"] == "https://open-dev.dingtalk.com/auth?x=1&userCode=ABC"
    assert pat["reason"] == "dingtalk_pat_consent_required"


@pytest.mark.skipif(not _HAS_SANDBOX_TOOL, reason="agentscope 未安装（仅容器内测）")
def test_detect_pat_none_when_absent():
    assert _detect_dws_pat_authorization(0, "ok", "") is None
    assert _detect_dws_pat_authorization(1, "err", "some other failure") is None


# ── Settings ──────────────────────────────────────────────────────────────
def test_settings_dws_arch_flag():
    from core.config.settings import settings
    # bind-mount is an architecture switch, kept in settings; client_id/secret moved to the Config platform DB configuration
    assert settings.sandbox.dws_creds_bind_mount_enabled is True


def test_dingtalk_seeded_in_system_configs():
    # Custom App credentials go through system-config dingtalk.* keys (configured visually in the Config admin platform, not in .env)
    from core.services.system_config import SEED_CONFIGS
    keys = {row[0] for row in SEED_CONFIGS}
    assert {"dingtalk.client_id", "dingtalk.client_secret", "dingtalk.trusted_domains"} <= keys
    # client_id/secret must be marked secret (masked in API responses)
    by_key = {row[0]: row for row in SEED_CONFIGS}
    assert by_key["dingtalk.client_id"][5] is True
    assert by_key["dingtalk.client_secret"][5] is True


# ── Credential-volume degradation ─────────────────────────────────────────
def test_creds_volume_degrades_without_host_storage():
    from core.sandbox._opensandbox_internals import _make_dws_creds_volumes
    # No local HOST_STORAGE_PATH → quietly return an empty list (sandbox still created, just without the credential volume)
    assert _make_dws_creds_volumes("u1") == []


def test_creds_volume_rejects_bad_user_id():
    from core.sandbox._opensandbox_internals import _make_dws_creds_volumes
    assert _make_dws_creds_volumes("") == []
    assert _make_dws_creds_volumes("../etc/passwd") == []


def test_dws_cache_dir_path():
    from core.sandbox._common import dws_cache_dir
    p = dws_cache_dir("u_abc")
    assert p.name == "u_abc"
    assert p.parent.name == "dws_cache"


# ── Models + repositories ─────────────────────────────────────────────────
def test_connection_repo_ensure_and_update(db):
    repo = DingTalkConnectionRepository(db)
    rec = repo.ensure("u1")
    assert rec.status == "disconnected"
    # Idempotent
    assert repo.ensure("u1").user_id == "u1"
    repo.update("u1", {"status": "pending", "login_user_code": "Z9", "granted_scopes": ["a", "b"]})
    rec2 = repo.get("u1")
    assert rec2.status == "pending"
    assert rec2.login_user_code == "Z9"
    assert rec2.granted_scopes == ["a", "b"]


def test_connection_status_dict(db):
    from core.services.dingtalk_service import DingTalkService
    svc = DingTalkService(db)
    data = svc.get_status("u1")
    assert data["status"] == "disconnected"
    assert data["granted_scopes"] == []
    assert "verification_url" in data
    assert "verification_url_complete" in data
    assert data["qr_data_uri"] is None  # QR code is not rendered when not pending


def test_status_dict_renders_qr_when_pending(db):
    from core.services.dingtalk_service import DingTalkService, make_qr_data_uri
    if make_qr_data_uri("https://x") is None:
        pytest.skip("segno 未安装（仅容器内测）")
    repo = DingTalkConnectionRepository(db)
    repo.ensure("u1")
    repo.update("u1", {
        "status": "pending",
        "login_verification_url_complete": "https://login.dingtalk.com/d?user_code=AB-12",
        "login_user_code": "AB-12",
    })
    data = DingTalkService(db).get_status("u1")
    assert data["status"] == "pending"
    assert (data["qr_data_uri"] or "").startswith("data:image/svg+xml")


# ── Private/marketplace skills enter user capability resolution (core fix) ──
def test_owned_private_skills_resolve_into_enabled(db):
    """Private skills/MCPs installed by the user themselves (owner_user_id == user) should
    enter their enabled set, getting injected into the agent toolkit + system prompt —
    not just global skills."""
    from core.db.models import AdminSkill, AdminMcpServer
    from core.config.catalog_resolver import _owned_enabled_ids
    # This user's private skills: one enabled, one disabled
    db.add(AdminSkill(skill_id="dingtalk-abc", skill_content="---\nname: x\ndescription: d\n---",
                      display_name="钉钉", description="d", owner_user_id="u1", is_enabled=True))
    db.add(AdminSkill(skill_id="disabled-skill", skill_content="---\nname: y\ndescription: d\n---",
                      display_name="禁用", description="d", owner_user_id="u1", is_enabled=False))
    # Another user's private skill (must never leak over)
    db.add(AdminSkill(skill_id="other-user-skill", skill_content="---\nname: z\ndescription: d\n---",
                      display_name="他人", description="d", owner_user_id="u2", is_enabled=True))
    db.add(AdminMcpServer(server_id="my-mcp", display_name="MyMCP", owner_user_id="u1", is_enabled=True))
    db.commit()
    skills, mcps = _owned_enabled_ids(db, "u1", {})
    assert "dingtalk-abc" in skills          # enabled private skill enters the set
    assert "disabled-skill" not in skills    # disabled ones do not
    assert "other-user-skill" not in skills  # multi-tenant isolation: other users' do not
    assert "my-mcp" in mcps


def test_owned_override_disables(db):
    """User turns a private skill off in the capability center (writes CatalogOverride enabled=false) → not in the set."""
    from core.db.models import AdminSkill
    from core.config.catalog_resolver import _owned_enabled_ids
    db.add(AdminSkill(skill_id="dingtalk-abc", skill_content="---\nname: x\ndescription: d\n---",
                      display_name="钉钉", description="d", owner_user_id="u1", is_enabled=True))
    db.commit()
    # Turn off via override
    skills, _ = _owned_enabled_ids(db, "u1", {"skills": [{"id": "dingtalk-abc", "enabled": False}]})
    assert "dingtalk-abc" not in skills


# ── Skill-list template regression guard ──────────────────────────────────
def test_skill_instruction_template_has_loop():
    """Regression guard: agent_factory's _SKILL_INSTRUCTION_TEMPLATE must contain the
    Jinja2 skill loop, otherwise get_skill_instructions renders only the header and the
    skill list is entirely empty → no skill ever appears in the system prompt or triggers
    automatically (losing this loop once made all skills behave as if unloaded)."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "core" / "llm" / "agent_factory.py"
    text = src.read_text(encoding="utf-8")
    assert "_SKILL_INSTRUCTION_TEMPLATE" in text
    assert "{% for skill in skills %}" in text, "技能清单 Jinja 循环缺失，技能区会渲染为空"
    assert "{{ skill.name }}" in text


# ── Marketplace skill installability ─────────────────────────────────────
def test_dingtalk_plugin_installable(db):
    """The DingTalk workbench has migrated from the "skill marketplace" to a "plugin":
    discovered via list → declares connection=dingtalk, contains a single skill, full
    references/scripts ported with the package → install lands as
    AdminSkill(source_plugin=dingtalk) → detail returns connection for the frontend to
    render the account-connection panel on the plugin detail page."""
    from core.services import plugin_service as ps

    items = ps.list_plugins(db, owner_user_id="u1")
    dt = next((it for it in items if it["slug"] == "dingtalk"), None)
    assert dt is not None, "dingtalk 未出现在插件列表"
    assert dt["installed"] is False
    assert dt["skills_count"] == 1

    # Marketplace detail: declares the account-connection type; the frontend renders the connection panel from it
    detail = ps.get_plugin_detail("dingtalk")
    assert detail.get("connection") == "dingtalk"

    res = ps.install_plugin(db, "dingtalk", owner_user_id="u1")
    assert res["action"] == "installed"

    sk = db.query(AdminSkill).filter(AdminSkill.source_plugin == "dingtalk").all()
    assert len(sk) == 1
    skill = sk[0]
    assert "钉钉" in (skill.description or "")
    # references + scripts fully ported (secrets.json not counted)
    extra = {k: v for k, v in (skill.extra_files or {}).items() if k != "secrets.json"}
    assert len(extra) > 50

    # Installed detail also returns connection (the frontend plugin detail page mounts the account-connection panel from it)
    inst = ps.get_installed_detail(db, res["install_id"], owner_user_id="u1")
    assert inst.get("connection") == "dingtalk"
