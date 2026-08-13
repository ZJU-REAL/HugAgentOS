"""Shared Yida plugin, connection service, path, and SKILL.md regression tests.

No dependency on real Yida or a real sandbox. EE persistent-sandbox state tests
live in ``tests/sandbox/test_yida_ee_persistence.py``.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
import core.db.models  # noqa: F401  ensure all models are registered (FK depends on users_shadow)
from core.db.models import AdminSkill, UserShadow


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(UserShadow(user_id="u1", username="alice"))
    s.commit()
    yield s
    s.close()


# ── Settings ────────────────────────────────────────────────────────────
def test_settings_yida_arch_flag():
    from core.config.settings import settings
    # bind-mount is an architecture switch, kept in settings; openyida has no deployment-level
    # credentials (login is in-conversation QR scan), no system config key, no DB connection table.
    assert settings.sandbox.yida_creds_bind_mount_enabled is True


def test_yida_cache_dir_path():
    from core.sandbox._common import yida_cache_dir, yida_workspace_dir
    p = yida_cache_dir("u_abc")
    assert p.name == "u_abc"
    assert p.parent.name == "yida_cache"
    # The sandbox bind source is the workspace subdirectory (the whole Yida working directory persists with the volume)
    assert yida_workspace_dir("u_abc") == p / "workspace"


def test_yida_shared_workspace_dir_path():
    """script-runner shared sandbox working directory: one per deployment (__shared__), rooted alongside the per-user directories."""
    from core.sandbox._common import yida_shared_workspace_dir
    p = yida_shared_workspace_dir()
    assert p.name == "workspace"
    assert p.parent.name == "__shared__"
    assert p.parent.parent.name == "yida_cache"


# ── Connection panel service (cookie cache + real read-only liveness reconciliation) ──
def test_yida_extract_result_json():
    from core.services.yida_service import extract_result_json

    out = (
        "some sandbox noise\n"
        '{"status":"need_qr_scan","qr_url":"https://login.dingtalk.com/x",'
        '"session_file":".cache/qr-session.json","poll_command":"openyida login --agent-poll ..."}\n'
    )
    data = extract_result_json(out)
    assert data and data["status"] == "need_qr_scan"
    assert extract_result_json("not json at all") is None
    assert extract_result_json("") is None
    # Take the last JSON line (there may be intermediate CLI output before it)
    two = '{"status":"old"}\n{"status":"ok","corp_id":"c1"}'
    assert extract_result_json(two)["status"] == "ok"


def test_yida_service_status_lifecycle(tmp_path, monkeypatch):
    import core.services.yida_service as ys

    monkeypatch.setattr(ys, "_host_workspace_dir", lambda uid: tmp_path / uid / "workspace")
    ys._PENDING.clear()
    svc = ys.YidaService()

    # No cookie → disconnected
    assert svc.get_status("u1")["status"] == "disconnected"
    # Pending session exists → pending (and the QR code is rendered)
    ys._PENDING["u1"] = {
        "session_file": ".cache/qr.json", "qr_url": "https://login.dingtalk.com/x",
        "started_at": __import__("time").monotonic(),
    }
    st = svc.get_status("u1")
    assert st["status"] == "pending"
    ys._PENDING.clear()
    # Cookie file written → connected
    ck = tmp_path / "u1" / "workspace" / ".cache"
    ck.mkdir(parents=True)
    (ck / "cookies-public.json").write_text('{"cookies":[],"base_url":"https://x.aliwork.com"}')
    assert svc.get_status("u1")["status"] == "connected"
    # Invalid user_id → disconnected immediately (never touching the filesystem)
    assert svc.get_status("../etc")["status"] == "disconnected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected_status"),
    [("valid", "connected"), ("invalid", "disconnected"), ("unknown", "connected")],
)
async def test_yida_probe_status_reconciles_real_login(
    tmp_path, monkeypatch, verdict, expected_status
):
    """Only a definite server rejection disconnects; transient probe failures keep local state."""
    import json as _json
    import os

    import core.services.yida_service as ys

    monkeypatch.setattr(ys, "_host_workspace_dir", lambda uid: tmp_path / uid / "workspace")
    ys._PENDING.clear()
    svc = ys.YidaService()
    cache = tmp_path / "u1" / "workspace" / ".cache"
    cache.mkdir(parents=True)
    cookie_file = cache / "cookies-public.json"
    cookie_file.write_text('{"cookies":[]}', encoding="utf-8")
    first_mtime_ns = cookie_file.stat().st_mtime_ns

    async def fake_run(user_id, command, timeout):  # noqa: ARG001
        assert command == ys._PROBE_COMMAND
        return _json.dumps({"verdict": verdict}), 0

    monkeypatch.setattr(svc, "_run_in_sandbox", fake_run)
    result = await svc.probe_status("u1")
    assert result["status"] == expected_status
    assert cookie_file.exists()  # an invalid probe marks this version stale; it does not destroy data

    if verdict == "valid":
        meta = _json.loads((tmp_path / "u1" / "connection.json").read_text(encoding="utf-8"))
        assert meta["status"] == "connected"
        assert meta["last_verified_at"]
    elif verdict == "invalid":
        meta = _json.loads((tmp_path / "u1" / "connection.json").read_text(encoding="utf-8"))
        assert meta["status"] == "disconnected"
        assert meta["invalidated_cookie_mtime_ns"] == first_mtime_ns
        assert svc.get_status("u1")["status"] == "disconnected"

        # A subsequent in-chat or panel QR login overwrites the cookie.  The
        # newer version becomes connected without requiring the old marker to
        # be manually cleared.
        cookie_file.write_text('{"cookies":[{"name":"new"}]}', encoding="utf-8")
        os.utime(cookie_file, ns=(first_mtime_ns + 1_000_000, first_mtime_ns + 1_000_000))
        assert svc.get_status("u1")["status"] == "connected"
    else:
        assert not (tmp_path / "u1" / "connection.json").exists()


@pytest.mark.asyncio
async def test_yida_probe_without_cookie_does_not_start_sandbox(tmp_path, monkeypatch):
    import core.services.yida_service as ys

    monkeypatch.setattr(ys, "_host_workspace_dir", lambda uid: tmp_path / uid / "workspace")
    svc = ys.YidaService()

    async def should_not_run(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("a disconnected user must not start a probe sandbox")

    monkeypatch.setattr(svc, "_run_in_sandbox", should_not_run)
    assert (await svc.probe_status("u1"))["status"] == "disconnected"


@pytest.mark.asyncio
async def test_yida_service_poll_transitions(tmp_path, monkeypatch):
    """Three poll states: ok → connected (metadata persisted); need_corp_selection → corp_selection;
    non-JSON (not scanned / timeout) → stays pending."""
    import json as _json

    import core.services.yida_service as ys

    monkeypatch.setattr(ys, "_host_workspace_dir", lambda uid: tmp_path / uid / "workspace")
    ys._PENDING.clear()
    svc = ys.YidaService()
    outputs = {}

    async def fake_run(user_id, command, timeout):  # noqa: ARG001
        return outputs["next"], 0

    monkeypatch.setattr(svc, "_run_in_sandbox", fake_run)

    # No pending → return the current status (disconnected)
    assert (await svc.poll_login("u1"))["status"] == "disconnected"

    ys._PENDING["u1"] = {
        "session_file": ".cache/qr.json", "qr_url": "https://q",
        "started_at": __import__("time").monotonic(),
    }
    # Not scanned: CLI errors with non-JSON → pending, and the QR code must be carried back (so a full-payload frontend overwrite doesn't wipe the display)
    outputs["next"] = "Error: timeout waiting for scan"
    r = await svc.poll_login("u1")
    assert r["status"] == "pending"
    assert r["qr_url"] == "https://q"
    assert "u1" in ys._PENDING
    # Success-summary shape: printLoginResult prints only {"ok":true,...} **without a status
    # field** (the initial release tripped exactly here in real testing: treated as pending while
    # the session file was already deleted → spins forever)
    outputs["next"] = _json.dumps({"ok": True, "corp_id": "c9", "user_id": "yu9",
                                   "base_url": "https://y.aliwork.com", "cookies_count": 12})
    r = await svc.poll_login("u1")
    assert r["status"] == "connected"
    assert r["corp_id"] == "c9"
    assert "u1" not in ys._PENDING
    # Fallback: poll returns non-JSON (e.g. the session file was cleaned up by the CLI after
    # success → ENOENT), but the cookie landed on disk during the session → login deemed complete
    ys._PENDING["u1"] = {"session_file": ".cache/qr.json", "qr_url": "q",
                         "started_at": __import__("time").monotonic()}
    ck2 = tmp_path / "u1" / "workspace" / ".cache"
    ck2.mkdir(parents=True, exist_ok=True)
    (ck2 / "cookies-public.json").write_text('{"cookies":[]}')
    outputs["next"] = "ENOENT: no such file or directory"
    r = await svc.poll_login("u1")
    assert r["status"] == "connected"
    assert "u1" not in ys._PENDING
    (ck2 / "cookies-public.json").unlink()
    ys._PENDING["u1"] = {"session_file": ".cache/qr.json", "qr_url": "https://q",
                         "started_at": __import__("time").monotonic()}
    # Multiple organizations
    outputs["next"] = _json.dumps({
        "status": "need_corp_selection",
        "organizations": [{"corp_id": "c1", "corp_name": "组织一", "main_org": True}],
    })
    r = await svc.poll_login("u1")
    assert r["status"] == "corp_selection"
    assert r["organizations"][0]["corp_id"] == "c1"
    # Completes after selecting an organization
    outputs["next"] = _json.dumps({"status": "ok", "corp_id": "c1", "user_id": "yu1",
                                   "base_url": "https://x.aliwork.com"})
    r = await svc.poll_login("u1", corp_id="c1")
    assert r["status"] == "connected"
    assert r["corp_id"] == "c1"
    assert "u1" not in ys._PENDING
    meta = _json.loads((tmp_path / "u1" / "connection.json").read_text())
    assert meta["corp_id"] == "c1"
    # corp_id injection surface: illegal characters are rejected outright
    ys._PENDING["u1"] = {"session_file": ".cache/qr.json", "qr_url": "q",
                         "started_at": __import__("time").monotonic()}
    r = await svc.poll_login("u1", corp_id="c1; rm -rf /")
    assert r["status"] == "error"
    ys._PENDING.clear()


@pytest.mark.asyncio
async def test_yida_run_in_sandbox_heals_stale_sandbox(monkeypatch):
    """Existing sandbox lacks the yida volume mount (cd failure echoes the marker) → close_session
    truly destroys it, then retry with a fresh sandbox. This was the pitfall the connection panel hit
    on first release: both the no-session_id light-pool path and old-sandbox adopt reuse trigger it."""
    import core.services.yida_service as ys

    calls = {"exec": 0, "closed": []}

    class FakeResult:
        def __init__(self, stdout, exit_code, stderr=""):
            self.stdout, self.exit_code, self.stderr = stdout, exit_code, stderr

    class FakeProvider:
        async def execute(self, req):
            calls["exec"] += 1
            assert req.session_id == "yida-connect-u1"  # must carry the synthetic session → persistent sandbox
            if calls["exec"] == 1:
                return FakeResult(f"{ys._NO_WS_MARKER}\n", 97)  # stale: no mount
            return FakeResult('{"status":"need_qr_scan","qr_url":"https://q",'
                              '"session_file":".cache/qr.json"}', 0)

        async def close_session(self, session_id):
            calls["closed"].append(session_id)

    import core.sandbox.factory as factory
    monkeypatch.setattr(factory, "get_sandbox_provider", lambda: FakeProvider())

    ys._PENDING.clear()
    r = await ys.YidaService().start_login("u1")
    assert r["status"] == "pending"
    assert calls["exec"] == 2
    assert calls["closed"] == ["yida-connect-u1"]
    ys._PENDING.clear()


def test_yida_plugin_declares_connection():
    """plugin.json declares connection=yida (extensions["org.hugagent"], Agent Plugins standard)
    → the frontend plugin detail page renders the YidaConnect panel."""
    import pathlib

    from core.services.plugin_importer import normalize_plugin_dir

    p = (
        pathlib.Path(__file__).resolve().parents[1]
        / "plugin_bundles" / "marketplace" / "yida"
    )
    assert normalize_plugin_dir(p).connection == "yida"


# ── Marketplace plugin installability ────────────────────────────────────
def test_yida_plugin_installable(db):
    """Yida ships as a plugin: discovered via list → declares connection=yida (QR-scan connect
    panel on the plugin detail page, in-conversation scan as fallback), contains a single skill,
    the full references/subskills set travels with the package → install lands
    AdminSkill(source_plugin=yida)."""
    from core.services import plugin_service as ps

    items = ps.list_plugins(db, owner_user_id="u1")
    yd = next((it for it in items if it["slug"] == "yida"), None)
    assert yd is not None, "yida 未出现在插件列表"
    assert yd["installed"] is False
    assert yd["skills_count"] == 1

    # Marketplace detail: declares the account connection type; the frontend renders the YidaConnect QR panel based on it
    detail = ps.get_plugin_detail("yida")
    assert detail.get("connection") == "yida"

    res = ps.install_plugin(db, "yida", owner_user_id="u1")
    assert res["action"] == "installed"

    sk = db.query(AdminSkill).filter(AdminSkill.source_plugin == "yida").all()
    assert len(sk) == 1
    skill = sk[0]
    assert "宜搭" in (skill.description or "")
    # The full references set (including the 47 subskills READMEs) is carried over
    extra = skill.extra_files or {}
    assert len(extra) > 100
    assert any("subskills/yida-app/" in k for k in extra)


# ── SKILL.md host-adaptation regression pins ─────────────────────────────
def test_yida_skill_md_host_adaptation():
    """Regression pin: SKILL.md is synced from openyida-skills.zip, and an upgrade can easily
    overwrite it back to the upstream text written for a use_skill host. This host has no use_skill,
    and the login / working-directory conventions are specific to this repository — losing any of
    these three adaptations makes the skill unusable inside the sandbox."""
    import pathlib

    p = (
        pathlib.Path(__file__).resolve().parents[1]
        / "plugin_bundles" / "marketplace" / "yida" / "skills" / "yida" / "SKILL.md"
    )
    text = p.read_text(encoding="utf-8")
    # Fixed working-directory convention (consistent with _YIDA_WORKSPACE_MOUNT)
    assert "/home/ubuntu/yida-workspace" in text
    # In-conversation QR-scan login (the sandbox has no desktop browser, --browser is not an option)
    assert "--agent-qr" in text
    # Channel adaptation: must not instruct the host to call use_skill (this host lacks that tool)
    assert 'use_skill("' not in text
    # Key frontmatter fields
    assert text.startswith("---\nname: yida\n")
    assert "allowed_tools:" in text.split("---")[1]
