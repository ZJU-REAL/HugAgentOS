"""Acceptance tests for the no-Docker local/quick-install profile.

Covers the pieces that are new or SQLite-risky (see
``internal design docs`` §6):

- fakeredis substitution for ``REDIS_URL=memory://`` — incl. blocking XREAD
- SQLite ``BigInteger`` autoincrement PK portability (the ``BigIntPK`` variant)
- built-in MCP catalog seed: idempotent, empty-table-only, URL templating
- super_admin bootstrap writes ``users_shadow.extra_data.role``
- local single-origin hosting: ``/api`` prefix strip + SPA fallback
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

# ── fakeredis (REDIS_URL=memory://) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_redis_uses_fakeredis_and_blocks_on_xread(monkeypatch):
    import core.infra.redis as rmod

    # Point the module's settings at memory:// and reset the singleton.
    fake_settings = SimpleNamespace(redis=SimpleNamespace(url="memory://", socket_timeout=30))
    monkeypatch.setattr(rmod, "settings", fake_settings)
    monkeypatch.setattr(rmod, "_redis_pool", None)

    r = rmod.get_redis()
    assert type(r).__module__.startswith("fakeredis")

    # The load-bearing risk: blocking XREAD must actually block then wake on a
    # write (not busy-spin returning empty).
    i1 = await r.xadd("k", {"e": "1"}, maxlen=5000, approximate=True)

    async def writer():
        await asyncio.sleep(0.2)
        await r.xadd("k", {"e": "2"}, maxlen=5000, approximate=True)

    task = asyncio.create_task(writer())
    res = await r.xread({"k": i1}, count=100, block=3000)
    await task
    assert res and res[0][1][0][1]["e"] == "2"

    # Other command families chat_run_executor / consumers rely on.
    assert await r.getdel("missing") is None
    await rmod.close_redis()


# ── SQLite BigInteger autoincrement PK portability ───────────────────────────


def test_bigint_pk_autoincrements_on_sqlite(db_session):
    """A CE-safe BigIntPK must autoincrement under SQLite, not stay NULL."""
    from core.db.models import MemorySanitizerRule

    row = MemorySanitizerRule(rule_type="classified", pattern="unit-test")
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    assert row.id is not None and row.id >= 1


# ── Built-in MCP catalog seed ────────────────────────────────────────────────


def test_seed_builtin_mcp_is_idempotent_and_templates_url(db_session):
    from core.db.models import AdminMcpServer
    from core.services.mcp_service import BUILTIN_MCP_SERVERS, seed_builtin_mcp_servers_if_empty
    from mcp_servers._ports import PORTS

    seeded = seed_builtin_mcp_servers_if_empty(db_session)
    expected_ids = {spec["server_id"] for spec in BUILTIN_MCP_SERVERS if spec["server_id"] in PORTS}
    assert set(seeded) == expected_ids

    # Second call is a no-op (never resurrects rows / double-inserts).
    assert seed_builtin_mcp_servers_if_empty(db_session) == []
    assert db_session.query(AdminMcpServer).count() == len(expected_ids)

    sample_id = next(iter(expected_ids))
    sample = db_session.query(AdminMcpServer).filter_by(server_id=sample_id).one()
    assert sample.transport == "streamable_http"
    assert sample.url.endswith(f":{PORTS[sample_id]}/mcp/")


def test_seed_builtin_mcp_skips_when_catalog_present(db_session):
    from core.db.models import AdminMcpServer
    from core.services.mcp_service import seed_builtin_mcp_servers_if_empty

    # A pre-existing global built-in row means the catalog already exists.
    db_session.add(
        AdminMcpServer(
            server_id="preexisting",
            display_name="x",
            transport="streamable_http",
            url="http://x/mcp/",
        )
    )
    db_session.commit()
    assert seed_builtin_mcp_servers_if_empty(db_session) == []
    assert db_session.query(AdminMcpServer).count() == 1


# ── super_admin bootstrap ────────────────────────────────────────────────────


def test_super_admin_bootstrap_writes_role(db_session):
    from core.db.models import UserShadow
    from core.services.local_user_service import LocalUserService
    from core.services.user_service import UserService

    res = LocalUserService(db_session).create_by_admin(username="admin", password="pw-123456")
    assert res.ok and res.user_id
    # Fresh account is a regular user…
    shadow = db_session.query(UserShadow).filter_by(user_id=res.user_id).first()
    assert (shadow.extra_data or {}).get("role") != "super_admin"

    # …bootstrap elevates it (merge-safe: auth_source preserved).
    UserService(db_session).update_user_metadata(res.user_id, {"role": "super_admin"})
    db_session.refresh(shadow)
    assert shadow.extra_data.get("role") == "super_admin"
    assert shadow.extra_data.get("auth_source") == "local"


def test_ce_default_admin_is_idempotent_and_requires_password_change(db_session, monkeypatch):
    import core.services.local_user_service as local_users
    from core.auth.password import verify_password
    from core.db.models import LocalUser, UserShadow

    monkeypatch.setattr(
        local_users,
        "settings",
        SimpleNamespace(
            edition=SimpleNamespace(edition="ce"),
            auth=SimpleNamespace(password_min_length=8),
        ),
    )

    user_id, created = local_users.ensure_ce_default_admin(db_session)
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
    assert shadow.extra_data["must_change_password"] is True
    assert shadow.extra_data["onboarding_required"] is True

    assert local_users.ensure_ce_default_admin(db_session) == (user_id, False)
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
    import core.services.local_user_service as local_users
    from core.auth.backend import UserContext
    from core.db import model_repository
    from core.db.models import UserShadow
    from core.infra.exceptions import ValidationError

    fake_settings = SimpleNamespace(
        edition=SimpleNamespace(edition="ce"),
        auth=SimpleNamespace(password_min_length=8),
    )
    monkeypatch.setattr(local_users, "settings", fake_settings)
    monkeypatch.setattr(user_routes, "settings", fake_settings)

    user_id, _ = local_users.ensure_ce_default_admin(db_session)
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

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ValidationError, match="主对话模型"):
            loop.run_until_complete(user_routes.complete_my_onboarding(user=user, db=db_session))

        provider = model_repository.create_provider(
            db_session,
            display_name="Test chat",
            provider_type="chat",
            base_url="http://model.test/v1",
            api_key="test-key",
            model_name="test-model",
        )
        model_repository.assign_role(db_session, "main_agent", provider.provider_id)

        response = loop.run_until_complete(
            user_routes.complete_my_onboarding(user=user, db=db_session)
        )
    finally:
        loop.close()

    assert response["data"]["onboarding_required"] is False
    shadow = db_session.query(UserShadow).filter_by(user_id=user_id).one()
    assert "onboarding_required" not in shadow.extra_data
    assert shadow.extra_data["onboarding_completed_version"] == 1


def test_ce_has_no_optional_organization_permission_layers():
    import importlib.util

    from core.auth.edition_capabilities import default_capability_layers_for_user

    assert importlib.util.find_spec("core.auth.role_permissions") is None
    assert importlib.util.find_spec("core.auth.team_permissions") is None
    assert default_capability_layers_for_user(None, "user_ce_admin") == ()


def test_ce_branding_repairs_persistent_page_config(db_session, monkeypatch):
    import core.content.content_blocks as blocks
    from core.db.models import ContentBlock

    monkeypatch.setattr(
        blocks,
        "settings",
        SimpleNamespace(edition=SimpleNamespace(edition="ce")),
    )
    db_session.add(
        ContentBlock(
            id="page_config",
            payload={
                "auth": {"allow_register": True},
                "branding": {
                    "product_name": "旧名称",
                    "product_subtitle": "旧副标题",
                    "page_title": "旧标签",
                    "hero_title": "你好，我是旧名称",
                    "logo_url": "/custom.svg",
                },
                "navigation": {
                    "admin_header": {"title": "旧名称 — 后台管理", "subtitle": "后台管理"},
                    "admin_platform": {"product_name": "旧名称", "config_label": "系统配置"},
                },
            },
        )
    )
    db_session.commit()

    assert blocks.enforce_ce_branding(db_session) is True
    row = db_session.query(ContentBlock).filter_by(id="page_config").one()
    assert row.payload["branding"] == {
        "product_name": "HugAgentOS",
        "product_subtitle": "AI 智能助手",
        "page_title": "HugAgentOS",
        "hero_title": "你好，我是 HugAgentOS",
        "logo_url": "/custom.svg",
    }
    assert row.payload["navigation"]["admin_header"]["title"] == "HugAgentOS — 后台管理"
    assert row.payload["navigation"]["admin_platform"]["product_name"] == "HugAgentOS"
    assert row.payload["auth"]["allow_register"] is False
    assert blocks.enforce_ce_branding(db_session) is False


def test_ce_registration_is_disabled_unconditionally(monkeypatch):
    import core.content.content_blocks as blocks

    monkeypatch.setattr(
        blocks,
        "settings",
        SimpleNamespace(edition=SimpleNamespace(edition="ce")),
    )

    assert blocks.is_register_allowed() is False


@pytest.mark.asyncio
async def test_ce_local_login_starts_in_english_with_brand_title(monkeypatch):
    import api.routes.v1.mock_sso as mock_sso
    import core.config.settings as settings_module
    from starlette.requests import Request

    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(
            auth=SimpleNamespace(local_enabled=True),
            edition=SimpleNamespace(edition="ce"),
            sso=SimpleNamespace(effective_login_mode="local"),
        ),
    )
    monkeypatch.setattr(mock_sso, "is_register_allowed", lambda: False)
    monkeypatch.setattr(
        mock_sso,
        "get_branding_info",
        lambda: {"product_name": "HugAgentOS", "logo_url": "/icon.png"},
    )
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/login",
            "raw_path": b"/login",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        }
    )

    response = await mock_sso.mock_login_page(
        request,
        redirect="/",
        auto="0",
        error=None,
        tab=None,
        reg_error=None,
    )
    html = response.body.decode()
    assert response.status_code == 200
    assert '<html lang="en">' in html
    assert "<title>HugAgentOS</title>" in html
    assert 'data-tab="register"' not in html
    assert 'name="confirm_password"' not in html
    assert '<img class="brand-logo" src="/home/hugagentos-logo.png" alt="HugAgentOS" />' in html
    assert "<span>HugAgentOS</span>" not in html
    assert "document.title = 'HugAgentOS'" in html


def test_mock_login_shortcuts_require_non_ce_explicit_mock_mode(monkeypatch):
    import api.routes.v1.mock_sso as mock_sso
    import core.config.settings as settings_module

    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(
            edition=SimpleNamespace(edition="ce"),
            sso=SimpleNamespace(effective_login_mode="mock"),
        ),
    )
    assert mock_sso._mock_account_shortcuts_enabled() is False

    monkeypatch.setattr(
        settings_module,
        "settings",
        SimpleNamespace(
            edition=SimpleNamespace(edition="ee"),
            sso=SimpleNamespace(effective_login_mode="mock"),
        ),
    )
    assert mock_sso._mock_account_shortcuts_enabled() is True


# ── Local single-origin hosting: /api strip + SPA fallback ───────────────────


def _local_app(tmp_path: Path):
    from api.local_hosting import mount_frontend_static, setup_local_api_prefix
    from fastapi import FastAPI

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")

    app = FastAPI()

    @app.get("/v1/ping")
    async def ping():
        return {"ok": True}

    setup_local_api_prefix(app)  # /api/* -> /*
    import os

    os.environ["FRONTEND_DIST_DIR"] = str(dist)
    mount_frontend_static(app)
    return app


def test_local_api_prefix_and_spa_fallback(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    client = TestClient(_local_app(tmp_path))

    # /api/v1/ping is bridged to /v1/ping (nginx-style strip).
    assert client.get("/api/v1/ping").json() == {"ok": True}
    assert client.get("/v1/ping").json() == {"ok": True}

    # SPA routes return index.html on hard refresh.
    for route in ("/admin", "/config", "/api-docs"):
        r = client.get(route)
        assert r.status_code == 200 and "<title>app</title>" in r.text

    # Real static asset served as a file.
    assert "console.log" in client.get("/assets/app.js").text

    # Unknown API path is a real 404, never masked as the SPA.
    assert client.get("/v1/nope").status_code == 404


# ── Self-built KB over embedded Milvus Lite (dense-only) ─────────────────────


def test_kb_milvus_lite_dense_only(tmp_path, monkeypatch):
    """The vector KB must run server-less on Milvus Lite: a file MILVUS_URL is
    detected as Lite, the collection is created dense-only (no sparse field), an
    upsert with a sparse_embedding key succeeds (stripped), and a dense search
    returns the row."""
    pytest = __import__("pytest")
    try:
        import milvus_lite  # noqa: F401
    except Exception:
        pytest.skip("milvus-lite not installed")

    monkeypatch.setenv("MILVUS_URL", str(tmp_path / "kb.db"))
    import importlib

    import core.kb.kb_vector as kv

    importlib.reload(kv)  # re-read MILVUS_URL

    DIM = 8
    monkeypatch.setattr(kv, "detect_embed_dim", lambda: DIM)
    monkeypatch.setattr(kv, "embed_text", lambda t: [0.1] * DIM)

    assert kv._is_lite() is True
    kv.get_or_create_collection()
    kv.upsert_rows(
        [
            {
                "chunk_id": "c1",
                "parent_chunk_id": "c1",
                "row_type": "chunk",
                "user_id": "u1",
                "kb_id": "kb1",
                "document_id": "d1",
                "title": "t",
                "content": "机器学习",
                "tags_text": "",
                "chunk_index": 0,
                "dense_embedding": [0.1] * DIM,
                "sparse_embedding": kv.text_to_sparse("机器学习"),  # must be stripped on Lite
            }
        ]
    )
    hits = kv.hybrid_search("u1", ["kb1"], "机器学习", [0.1] * DIM, top_k=5)
    assert len(hits) == 1 and hits[0]["content"] == "机器学习"


# ── /workspace alias (site-building + skills work when WORKSPACE != /workspace) ─


def test_workspace_path_alias_in_local_mode(monkeypatch):
    """When the real workspace root differs (no-Docker local), the file-tool path
    layer must alias a leading /workspace → the real root, accept it in validation,
    and leave /myspace and lookalikes (/workspaces) untouched. No-op in Docker."""
    import core.llm.tools._paths as p

    monkeypatch.setattr(p, "WORKSPACE_ROOT", "/home/u/.hugagent/workspace")
    assert p.canonicalize_ws_path("/workspace") == "/home/u/.hugagent/workspace"
    assert (
        p.canonicalize_ws_path("/workspace/site-src/foo")
        == "/home/u/.hugagent/workspace/site-src/foo"
    )
    assert p.canonicalize_ws_path("/workspaces/other") == "/workspaces/other"
    assert p.canonicalize_ws_path("/myspace/a") == "/myspace/a"
    # The model passes container-canonical /workspace paths → validation accepts them.
    assert p.validate_workspace_path("/workspace/.site-dist/x") is None
    # to_physical_path returns the aliased (real) root for non-myspace paths.
    assert (
        p.to_physical_path("/workspace/site-src/foo", "u1")
        == "/home/u/.hugagent/workspace/site-src/foo"
    )

    # Docker parity: root == /workspace → every alias is a byte-for-byte no-op.
    monkeypatch.setattr(p, "WORKSPACE_ROOT", "/workspace")
    assert p.canonicalize_ws_path("/workspace/site-src/foo") == "/workspace/site-src/foo"


def test_runner_canon_ws_and_bash_rewrite(monkeypatch):
    """The script_runner service must alias /workspace at its path chokepoint and in
    the bash/python it executes, so model-written /workspace paths resolve locally."""
    import services.script_runner_service.server as srv

    monkeypatch.setattr(srv, "WORKSPACE_ROOT", "/home/u/.hugagent/workspace")
    assert srv._canon_ws("/workspace") == "/home/u/.hugagent/workspace"
    assert srv._canon_ws("/workspace/a.txt") == "/home/u/.hugagent/workspace/a.txt"
    assert srv._canon_ws("/workspaces/x") == "/workspaces/x"

    # The bash-command regex rewrites path-boundary /workspace but not /workspaces.
    ws_re = __import__("re").compile(r'/workspace(?=/|$|["\'\s:;)&|])')
    out = ws_re.sub(
        "/home/u/.hugagent/workspace", "cd /workspace && npm run build; echo /workspaces"
    )
    assert out == "cd /home/u/.hugagent/workspace && npm run build; echo /workspaces"
