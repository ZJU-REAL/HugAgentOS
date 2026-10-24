"""Acceptance tests for the no-Docker local/quick-install profile.

Covers the pieces that are new or SQLite-risky (see
``internal design docs`` §6):

- the local profile reaches Redis-free backends and never opens a client
- SQLite ``BigInteger`` autoincrement PK portability (the ``BigIntPK`` variant)
- built-in MCP catalog seed: idempotent, empty-table-only, URL templating
- super_admin bootstrap writes ``users_shadow.extra_data.role``
- local single-origin hosting: ``/api`` prefix strip + SPA fallback
"""
from core.chat.projection.codec import chat_projection_codec
from core.files.content import branding

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# ── Redis-free local profile ─────────────────────────────────────────────────

def _local_redis_settings(monkeypatch):
    """Point core.infra.redis at an unconfigured (local-profile) deployment."""
    import core.infra.redis as rmod

    monkeypatch.setattr(
        rmod, "settings", SimpleNamespace(redis=SimpleNamespace(url="", socket_timeout=30))
    )
    monkeypatch.setattr(rmod, "_redis_pools", type(rmod._redis_pools)())
    monkeypatch.setattr(rmod, "_stream_pools", type(rmod._stream_pools)())
    return rmod


def test_local_profile_never_opens_a_redis_client(monkeypatch):
    """No URL means no client and no imitation — the seams must not reach here.

    The desktop used to run an in-process Redis imitation, which only had to
    disagree with redis-py about one reply shape to stall a whole chat turn.
    An unconfigured deployment now fails loudly instead of substituting one.
    """
    rmod = _local_redis_settings(monkeypatch)

    assert rmod.redis_configured() is False
    with pytest.raises(RuntimeError, match="REDIS_URL is not configured"):
        asyncio.run(_open_client(rmod))


async def _open_client(rmod):
    return rmod.get_redis()


@pytest.mark.asyncio
async def test_local_profile_picks_in_process_backends(monkeypatch):
    """Both store-agnostic seams follow the same predicate."""
    import core.infra.ephemeral as ephemeral
    import core.runs.events.store as res

    _local_redis_settings(monkeypatch)
    monkeypatch.setattr(ephemeral, "redis_configured", lambda: False)
    monkeypatch.setattr(res, "redis_configured", lambda: False)

    assert isinstance(ephemeral.get_ephemeral_state(), ephemeral.LocalEphemeralState)
    assert isinstance(res.get_run_event_stream(), res.LocalRunEventStream)


@pytest.mark.asyncio
async def test_local_event_log_blocks_then_wakes_on_append(monkeypatch):
    """The load-bearing risk: a follower must park, then wake on the next event.

    Busy-returning empty would spin the CPU; never waking would leave the UI on
    "starting task" for the whole run — the failure the imitation used to cause.
    """
    import core.runs.events.store as res

    stream = res.LocalRunEventStream(chat_projection_codec())
    await stream.append("run-1", {"type": "content", "_offset": 1})
    cursor = (await stream.read("run-1"))[-1][0]

    async def writer():
        await asyncio.sleep(0.2)
        await stream.append("run-1", {"type": "content", "_offset": 2})

    task = asyncio.create_task(writer())
    started = time.monotonic()
    batch = await stream.wait("run-1", after=cursor, limit=100, timeout_ms=3000)
    waited = time.monotonic() - started
    await task

    assert [event["_offset"] for _cursor, event in batch] == [2]
    assert 0.15 < waited < 1.0, f"should wake on the append, waited {waited:.2f}s"


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
    from core.capabilities.mcp.service import BUILTIN_MCP_SERVERS, seed_builtin_mcp_servers_if_empty
    from extensions.mcp_servers._ports import PORTS

    seeded = seed_builtin_mcp_servers_if_empty(db_session)
    expected_ids = {spec["server_id"] for spec in BUILTIN_MCP_SERVERS if spec["server_id"] in PORTS}
    assert set(seeded) == expected_ids

    # Second call is a no-op (never resurrects rows / double-inserts).
    assert seed_builtin_mcp_servers_if_empty(db_session) == []
    assert db_session.query(AdminMcpServer).count() == len(expected_ids)

    sample_id = next(iter(expected_ids))
    sample = db_session.query(AdminMcpServer).filter_by(server_id=sample_id).one()
    assert sample.transport == "streamable_http"
    assert sample.url.endswith(f":{PORTS[sample_id]}/mcp")


def test_seed_builtin_mcp_skips_when_catalog_present(db_session):
    from core.db.models import AdminMcpServer
    from core.capabilities.mcp.service import seed_builtin_mcp_servers_if_empty

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


def test_retired_report_export_mcp_is_not_seeded_and_legacy_row_is_pruned(db_session):
    from core.db.models import AdminMcpServer
    from core.capabilities.mcp.service import BUILTIN_MCP_SERVERS, prune_removed_builtin_mcp_servers
    from extensions.mcp_servers._ports import PORTS

    assert "report_export_mcp" not in PORTS
    assert "report_export_mcp" not in {str(spec["server_id"]) for spec in BUILTIN_MCP_SERVERS}
    db_session.add(
        AdminMcpServer(
            server_id="report_export_mcp",
            display_name="报告导出",
            transport="streamable_http",
            url="http://mcp:9105/mcp/",
            owner_user_id=None,
            source_plugin=None,
        )
    )
    db_session.commit()

    assert prune_removed_builtin_mcp_servers(db_session) == ["report_export_mcp"]
    assert db_session.query(AdminMcpServer).count() == 0


# ── super_admin bootstrap ────────────────────────────────────────────────────


def test_super_admin_bootstrap_writes_role(db_session):
    from core.db.models import UserShadow
    from core.auth.local_users import LocalUserService
    from core.auth.users import UserService

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






def test_ce_has_no_optional_organization_permission_layers():
    import importlib.util

    from core.auth.edition import default_capability_layers_for_user

    assert importlib.util.find_spec("core.auth.edition") is None
    assert importlib.util.find_spec("core.auth.team_permissions") is None
    assert default_capability_layers_for_user(None, "user_ce_admin") == ()


def test_ce_branding_repairs_persistent_page_config(db_session, monkeypatch):
    import core.files.content.content_blocks as blocks
    from core.db.models import ContentBlock

    monkeypatch.setattr(
        branding,
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

    assert branding.enforce_ce_branding(db_session) is True
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
    assert branding.enforce_ce_branding(db_session) is False


def test_ce_registration_is_disabled_unconditionally(monkeypatch):
    import core.files.content.content_blocks as blocks

    monkeypatch.setattr(
        branding,
        "settings",
        SimpleNamespace(edition=SimpleNamespace(edition="ce")),
    )

    assert branding.is_register_allowed() is False


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
    from features.knowledge import kb_vector, vector_client, vector_indexing
    from core.models import embedding as vector_embedding, embedding_state
    import features.knowledge.kb_vector_state as kv
    importlib.reload(kv)
    importlib.reload(embedding_state)  # Reset relocated caches and environment configuration.
    DIM = 8
    monkeypatch.setattr(vector_embedding, 'detect_embed_dim', lambda: DIM)
    monkeypatch.setattr(vector_embedding, 'embed_text', lambda t: [0.1] * DIM)

    assert vector_client.is_lite() is True
    vector_client.get_or_create_collection()
    vector_indexing.upsert_rows(
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
                "sparse_embedding": kb_vector.text_to_sparse("机器学习"),  # must be stripped on Lite
            }
        ]
    )
    hits = kb_vector.hybrid_search("u1", ["kb1"], "机器学习", [0.1] * DIM, top_k=5)
    assert len(hits) == 1 and hits[0]["content"] == "机器学习"


# ── Loopback proxy bypass (desktop/local internal calls must not hit env proxies) ─


def test_local_env_merges_loopback_hosts_into_no_proxy(monkeypatch):
    """A system proxy (http_proxy/all_proxy) must never intercept the local
    profile's 127.0.0.1 service mesh — httpx honours those vars and a desktop
    proxy (e.g. Clash) answers loopback targets with 502, breaking sidecar
    readiness and site publishing. Existing NO_PROXY entries must survive."""
    import cli

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "internal.corp")
    monkeypatch.delenv("no_proxy", raising=False)

    cli.ensure_loopback_proxy_bypass()

    import os

    for var in ("NO_PROXY", "no_proxy"):
        entries = os.environ[var].split(",")
        assert "internal.corp" in entries
        assert "127.0.0.1" in entries
        assert "localhost" in entries
        assert "::1" in entries
    # The proxy itself stays configured — external model/API calls may need it.
    assert os.environ["http_proxy"] == "http://127.0.0.1:7897"

    # Idempotent: a second call must not duplicate entries.
    cli.ensure_loopback_proxy_bypass()
    assert os.environ["NO_PROXY"].split(",").count("127.0.0.1") == 1


@pytest.mark.parametrize("explicit", [None, "", "http://configured:8080"])
def test_windows_loopback_bypass_preserves_registry_proxy(monkeypatch, explicit):
    import os
    from urllib import request

    import cli

    for name in list(os.environ):
        if name.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(request, "getproxies_registry", lambda: {"http": "http://system:7897", "https": "http://system:7897"}, raising=False)
    if explicit is not None:
        monkeypatch.setenv("HTTPS_PROXY", explicit)
    cli.ensure_loopback_proxy_bypass()
    assert os.environ["HTTPS_PROXY"] == (explicit if explicit is not None else "http://system:7897")
    assert "127.0.0.1" in os.environ["NO_PROXY"]
    if explicit is not None:
        assert "HTTP_PROXY" not in os.environ


def test_apply_local_env_seeds_the_profile_before_settings_is_frozen(tmp_path):
    """``DEPLOY_PROFILE`` must be in the environment before ``settings`` reads it.

    ``core.config.settings`` snapshots the environment the first time it is
    imported, and ``core.services`` imports it transitively. Resolving anything
    that reaches ``core.services`` while building the local defaults therefore
    freezes a settings object that predates ``DEPLOY_PROFILE=local``: the local
    profile then silently runs as a compose deployment — no ``/api`` prefix
    bridge, no static frontend, mock auth — which in the desktop shell shows up
    as "本机执行面尚未就绪" on every hybrid request.

    A fresh interpreter is required: ``settings`` is a module-level singleton.
    """
    import json
    import subprocess
    import sys
    from pathlib import Path as _Path

    backend = str(_Path(__file__).resolve().parents[1])
    script = (
        "import os, sys, json\n"
        "os.environ.pop('DEPLOY_PROFILE', None)\n"
        f"os.environ['HUGAGENT_HOME'] = {str(tmp_path / 'home')!r}\n"
        "import cli\n"
        "cli.apply_local_env(port=18999)\n"
        "from core.config.settings import settings\n"
        "print(json.dumps({'is_local': settings.deploy.is_local,\n"
        "                  'site_home': os.environ.get('SITE_TEMPLATE_HOME', '')}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": backend},
        cwd=backend,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["is_local"] is True
    assert payload["site_home"]
