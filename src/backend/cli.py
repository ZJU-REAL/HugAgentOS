"""HugAgentOS no-Docker local server CLI (``hugagent-server`` console entry).

hermes-agent-style quick install: one process, SQLite, in-process event log
and ephemeral state, subprocess MCP + sandbox — zero Docker / Postgres / Redis.

    hugagent-server            # not initialized → onboard; else serve + open browser
    hugagent-server onboard    # first-run wizard (admin account → model → serve); re-runnable
    hugagent-server serve      # start the server
    hugagent-server doctor     # environment self-check
    hugagent-server engine     # headless loopback engine for the `hugagent` terminal client

The ``hugagent`` command itself belongs to the terminal client (npm package
``packages/cli``), which forwards ``serve`` / ``onboard`` / ``doctor`` here.

Data lives under ``~/.hugagent/`` (override with ``HUGAGENT_HOME``):
``data.db`` (SQLite), ``storage/``, ``workspace/`` (sandbox), ``logs/``, ``config.env``.

IMPORTANT: this module sets the local-profile environment **before** importing
anything that reads ``core.config.settings`` (settings snapshot env at import
time), so every heavy import is done lazily inside the command functions.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

def _configure_standard_streams() -> None:
    """Keep redirected desktop logs UTF-8 and tolerant of status symbols."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (LookupError, OSError, ValueError):
            try:
                reconfigure(errors="replace")
            except (LookupError, OSError, ValueError):
                pass

def _status(message: str, *, file=None, **kwargs) -> None:
    """Print CLI status without letting a legacy code page break startup."""
    stream = file or sys.stdout
    try:
        print(message, file=stream, **kwargs)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        try:
            safe_message = message.encode(encoding, errors="replace").decode(encoding)
        except LookupError:
            safe_message = message.encode("ascii", errors="replace").decode("ascii")
        print(safe_message, file=stream, **kwargs)

# ── Data dir & environment ────────────────────────────────────────────────────

def data_dir() -> Path:
    from core.config.runtime_env import local_data_dir

    return local_data_dir()

def ensure_loopback_proxy_bypass() -> None:
    """Keep loopback traffic off any HTTP(S)/SOCKS proxy from the environment.

    The local profile is a mesh of 127.0.0.1 services (backend ↔ MCP sidecars ↔
    script_runner ↔ internal publish/batch callbacks) all speaking httpx, which
    honours ``http_proxy``/``https_proxy``/``all_proxy``. A system proxy (e.g.
    Clash on macOS) usually rejects loopback targets with 502, which kills
    startup readiness and site publishing. Merge loopback hosts into NO_PROXY
    instead of deleting the proxy vars — model/API calls may legitimately need
    the proxy to reach external endpoints.
    """
    # urllib/httpx on Windows chooses environment proxies OR registry proxies.
    # Adding NO_PROXY alone makes the environment dict nonempty and silently
    # hides the system proxy. Preserve it in this process before adding bypasses.
    # Any explicitly configured transport proxy (including an empty value) wins.
    if sys.platform == "win32" and not any(
        key.lower() in ("http_proxy", "https_proxy", "all_proxy") for key in os.environ
    ):
        from urllib import request as urllib_request

        registry_proxies = urllib_request.getproxies_registry()
        for scheme in ("http", "https", "all"):
            if registry_proxies.get(scheme):
                os.environ[scheme.upper() + "_PROXY"] = registry_proxies[scheme]
    loopback_hosts = ("127.0.0.1", "localhost", "::1")
    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    entries = [entry.strip() for entry in current.split(",") if entry.strip()]
    entries.extend(host for host in loopback_hosts if host not in entries)
    merged = ",".join(entries)
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged

def _resolve_frontend_dist() -> Optional[str]:
    """Locate the built frontend for StaticFiles hosting."""
    env = os.getenv("FRONTEND_DIST_DIR", "").strip()
    if env and (Path(env) / "index.html").exists():
        return env
    # src/backend/cli.py → parents[1] = src/backend, .parent = src
    src = Path(__file__).resolve().parents[1]
    cand = src / "frontend" / "dist"
    return str(cand) if (cand / "index.html").exists() else None

def _site_template_home() -> Path:
    from features.desktop.local.plugins.assets import site_template_home

    return site_template_home()

def apply_local_env(port: int) -> dict:
    """Populate the local-profile env (idempotent; real env wins) + data dirs."""
    ensure_loopback_proxy_bypass()
    dd = data_dir()
    for sub in ("", "storage", "workspace", "logs", "node", "node/browsers", "fonts"):
        (dd / sub).mkdir(parents=True, exist_ok=True)

    dist = _resolve_frontend_dist()
    defaults = {
        "DEPLOY_PROFILE": "local",
        "JX_EDITION": "ce",
        "BRAND_PRODUCT_NAME": "HugAgentOS",
        "DATABASE_URL": f"sqlite:///{dd / 'data.db'}",
        # No Redis on a single-machine install. Every capability that used one
        # reaches an in-process backend through core.infra.ephemeral /
        # orchestration.run_event_stream, selected by this being empty.
        "REDIS_URL": "",
        "SESSION_STORE": "memory",
        "AUTH_MODE": "session",
        "SSO_LOGIN_MODE": "local",
        "LOCAL_AUTH_ENABLED": "true",
        "SANDBOX_PROVIDER": "script_runner",
        "SANDBOX_RUNNER_URL": "http://127.0.0.1:8900",
        "SANDBOX_TOOLS_ENABLED": "true",
        "SCRIPT_RUNNER_WORKSPACE": str(dd / "workspace"),
        # Skills dir lives UNDER the workspace so the host script_runner (which
        # shares the host filesystem, no bind mount) sees built-in + installed
        # skill files at {workspace}/skills/<id> — the path the model is told.
        # With a capability store it is a link view into that store.
        "SANDBOX_SKILLS_DIR": str(dd / "workspace" / "skills"),
        # Capability file store root (skills/ plugins/ agents/ mcp.json). The
        # desktop shell overrides this with its application data directory;
        # the standalone local install keeps it with the rest of its data.
        "HUGAGENT_CAPS_ROOT": str(dd),
        # Office Agent Skills use locally installed Node packages without
        # requiring a writable global npm prefix.
        "NODE_PATH": str(dd / "node" / "node_modules"),
        "PLAYWRIGHT_BROWSERS_PATH": str(dd / "node" / "browsers"),
        "JX_FONT_DIR": str(dd / "fonts"),
        "MCP_HOST": "127.0.0.1",
        # Internal MCP callbacks (site publish / batch runner) execute in child
        # processes in the local profile.  Point them back at this exact
        # uvicorn instance instead of letting them fall back to the compose
        # default port (3001).  The desktop-managed server listens on 32101.
        "BACKEND_INTERNAL_URL": f"http://127.0.0.1:{port}",
        "BACKEND_PORT": str(port),
        # A local/desktop install is expected to be useful immediately after a
        # zero-state boot.  Keep the three credential-free first-party plugins
        # as the local-profile default even when the caller is plain
        # ``hugagent-server serve`` rather than one of the installer wrappers.
        "HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS": "1",
        "STORAGE_TYPE": "local",
        "STORAGE_PATH": str(dd / "storage"),
        "LOG_FILE_PATH": str(dd / "logs" / "backend.log"),
        # Self-built vector KB uses embedded Milvus Lite (a local file uri triggers
        # the server-less engine; kb_vector degrades to dense-only there). Requires
        # an embedding model — configure one in `onboard`.
        "MILVUS_URL": str(dd / "milvus.db"),
        # L2 mem0 memory is available by default over the same Lite backend. The
        # user switch still requires an assigned embedding model. L3 stays off
        # unless a separate Neo4j service is configured.
        "MEM0_ENABLED": "true",
        # Conversational React site-building: the site-builder skill / init script /
        # vite config all reference a container-canonical /opt/site-template + /workspace.
        # In local mode we provision the template under the data dir and point the
        # scripts at the real workspace via these envs (Docker keeps its baked defaults).
        "SITE_NODE_BASE": str(dd / "workspace" / ".site-node"),
        "PORT": str(port),
    }
    if dist:
        defaults["FRONTEND_DIST_DIR"] = dist
    for k, v in defaults.items():
        os.environ.setdefault(k, v)

    # Deliberately after the loop above: ``core.services`` pulls in
    # ``core.config.settings``, and ``settings`` snapshots the environment the
    # first time it is imported. Resolving the site-template path while building
    # ``defaults`` would freeze a settings object that predates
    # ``DEPLOY_PROFILE=local`` — the whole process would then run as a non-local
    # deployment (no ``/api`` prefix bridge, no static frontend, mock auth).
    from orchestration.configuration import register_configuration
    register_configuration()
    from features import register_run_capabilities, register_model_credentials
    register_run_capabilities()
    register_model_credentials()
    from integrations import register_credentials
    register_credentials()
    site_home = _site_template_home()
    defaults["SITE_TEMPLATE_HOME"] = str(site_home)
    defaults["SITE_TEMPLATE_DIR"] = str(site_home / "react-vite")
    os.environ.setdefault("SITE_TEMPLATE_HOME", defaults["SITE_TEMPLATE_HOME"])
    os.environ.setdefault("SITE_TEMPLATE_DIR", defaults["SITE_TEMPLATE_DIR"])
    return defaults

def _is_initialized() -> bool:
    """True if the SQLite DB exists and has at least one local account."""
    dd = data_dir()
    db_path = dd / "data.db"
    if not db_path.exists():
        return False
    try:
        import sqlite3

        con = sqlite3.connect(str(db_path))
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='local_users'"
            )
            if cur.fetchone() is None:
                return False
            cur = con.execute("SELECT COUNT(*) FROM local_users")
            return (cur.fetchone() or [0])[0] > 0
        finally:
            con.close()
    except Exception:
        return False

# ── Onboarding ────────────────────────────────────────────────────────────────

_PRESETS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5"),
}

# Conservative default for arbitrary OpenAI-compatible chat endpoints.  The
# value is persisted explicitly because the runtime intentionally refuses to
# guess a missing context window.
DEFAULT_LOCAL_CONTEXT_LENGTH = 32768

def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val or default

def bootstrap_admin(username: str, password: str) -> str:
    """Create the local admin account and elevate it to super_admin.

    Returns the new user_id. Raises RuntimeError on failure. This is the single
    place that writes ``users_shadow.extra_data.role='super_admin'`` — the one
    step missing everywhere else in the codebase.
    """
    from core.db.engine import SessionLocal
    from core.auth.local_users import LocalUserService
    from core.auth.users import UserService

    db = SessionLocal()
    try:
        res = LocalUserService(db).create_by_admin(username=username, password=password)
        if not res.ok or not res.user_id:
            raise RuntimeError(res.message)
        UserService(db).update_user_metadata(res.user_id, {"role": "super_admin"})
        return res.user_id
    finally:
        db.close()

def configure_model(
    base_url: str,
    api_key: str,
    model_name: str,
    provider_type: str = "chat",
    test: bool = True,
    context_length: Optional[int] = None,
) -> None:
    """Create a provider, assign it to every chat role, invalidate the cache.

    When ``test`` is set, pings the endpoint first and raises on failure.
    """
    import asyncio

    from core.models import repository as mr
    from core.db.engine import SessionLocal

    if test:
        from api.routes.v1.models import _test_connection

        result = asyncio.get_event_loop().run_until_complete(
            _test_connection(
                provider="openai_compatible",
                provider_type=provider_type,
                base_url=base_url,
                api_key=api_key,
                model_name=model_name,
            )
        )
        if not result.get("success"):
            raise RuntimeError(result.get("error") or "连通性测试失败")

    db = SessionLocal()
    try:
        extra_config = {}
        if provider_type == "chat":
            resolved_context_length = int(context_length or DEFAULT_LOCAL_CONTEXT_LENGTH)
            if resolved_context_length <= 0:
                raise ValueError("context_length 必须是正整数")
            extra_config["context_length"] = resolved_context_length

        provider = mr.create_provider(
            db,
            display_name=f"{model_name} (local)",
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            extra_config=extra_config,
        )
        # Assign the onboarded model to every role of its type — derive the set
        # from the single source of truth (ROLE_DEFINITIONS) so a new chat role
        # is picked up automatically.
        roles = [k for k, v in mr.ROLE_DEFINITIONS.items() if v["type"] == provider_type]
        for role in roles:
            mr.assign_role(db, role, provider.provider_id, updated_by="onboard")
    finally:
        db.close()

def mark_web_onboarding_complete(user_id: str) -> None:
    """Avoid repeating the browser setup after the terminal wizard succeeds."""
    from core.db.engine import SessionLocal
    from core.auth.local_users_constants import CE_ONBOARDING_VERSION
    from core.auth.users import UserService

    db = SessionLocal()
    try:
        UserService(db).update_user_metadata(
            user_id,
            {
                "onboarding_completed_version": CE_ONBOARDING_VERSION,
                "onboarding_required": False,
            },
        )
    finally:
        db.close()

# ── Plugins ───────────────────────────────────────────────────────────────────

# Recommended default plugin set for a personal single-machine install: task
# scheduling, skill authoring/market, and conversational site-building. Others
# (IM / email / low-code) need per-user credentials, so they're opt-in only.
_DEFAULT_PLUGIN_BOOTSTRAP_ENV = "HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS"

def _default_plugins() -> list:
    """推荐的默认插件集合，与库里的引导保持同一份来源。"""
    from core.capabilities.plugins.management import DEFAULT_BOOTSTRAP_PLUGIN_SLUGS

    return list(DEFAULT_BOOTSTRAP_PLUGIN_SLUGS)

def _default_plugins_marker() -> Path:
    from core.capabilities.plugins.management import LOCAL_BOOTSTRAP_MARKER_NAME

    return data_dir() / LOCAL_BOOTSTRAP_MARKER_NAME

def list_installable_plugins() -> list:
    """Local-filesystem plugin bundles (default + marketplace) with install state."""
    from core.db.engine import SessionLocal
    from core.capabilities.plugins import management as plugin_service
    from features.marketplace import plugin_market

    from features import register_package_sources, register_resource_owners
    register_package_sources()
    register_resource_owners()

    db = SessionLocal()
    try:
        items = plugin_market.list_plugins(db, None, include_disabled=True)
    finally:
        db.close()
    # Only the shipped bundles are installable offline; DB-market packages need an
    # admin upload that doesn't exist on a fresh local install.
    return [it for it in items if it.get("source") == "builtin"]

def install_plugins(slugs: list) -> list:
    """Install the given plugin slugs globally (owner_user_id=None). Best-effort."""
    from core.db.engine import SessionLocal
    from core.capabilities.plugins import management as plugin_service

    done = []
    from features import register_package_sources, register_resource_owners
    register_package_sources()
    register_resource_owners()

    db = SessionLocal()
    try:
        for slug in slugs:
            try:
                plugin_service.install_plugin(db, slug, owner_user_id=None, created_by="onboard")
            except Exception as exc:  # noqa: BLE001
                _status(f"  ! 插件 {slug} 安装失败：{exc}", file=sys.stderr)
            else:
                done.append(slug)
                _status(f"  ✓ 已安装插件：{slug}")
    finally:
        db.close()
    return done

def ensure_default_plugins_once() -> bool:
    """Install local-install defaults once without overriding later user choices.

    The marker intentionally lives beside the local SQLite database instead of
    being inferred from installed rows: after the first bootstrap, uninstalling
    a default plugin is an explicit user choice and must survive future desktop
    restarts and upgrades.  A failed or partial install leaves no marker, so the
    next launch retries the complete default set.

    Returns ``True`` when this call performed the first successful bootstrap.
    """
    marker = _default_plugins_marker()
    if marker.is_file():
        return False

    defaults = _default_plugins()
    installed = install_plugins(defaults)
    missing = [slug for slug in defaults if slug not in installed]
    if missing:
        raise RuntimeError(f"默认插件安装不完整：{', '.join(missing)}")
    temporary = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(defaults) + "\n", encoding="utf-8")
    os.replace(temporary, marker)
    return True

def _select_plugins_interactively(available: list) -> list:
    """Show the plugin menu, return the slugs the user picked."""
    recommended = _default_plugins()
    print("\n[插件] 选择要安装的插件（可在插件市场随时增减）")
    for i, it in enumerate(available, 1):
        rec = " ★推荐" if it["slug"] in recommended else ""
        installed = "（已装）" if it.get("installed") else ""
        desc = (it.get("description") or "")[:42]
        print(f"  {i:>2}. {it['name']}{rec}{installed} — {desc}")
    rec_nums = ",".join(
        str(i) for i, it in enumerate(available, 1) if it["slug"] in recommended
    )
    print("  输入序号（逗号分隔）、'all' 全装、'none' 跳过；直接回车装推荐项。")
    raw = _prompt("  选择", rec_nums).strip().lower()
    if raw in ("none", "-", "无"):
        return []
    if raw == "all":
        return [it["slug"] for it in available if not it.get("installed")]
    picked = []
    for tok in raw.replace("，", ",").split(","):
        tok = tok.strip()
        if tok.isdigit() and 1 <= int(tok) <= len(available):
            it = available[int(tok) - 1]
            if not it.get("installed"):
                picked.append(it["slug"])
    return picked

# ── File parser (PDF / document parsing on upload) ────────────────────────────

def configure_file_parser(
    api_url: str, *, backend: str = "pipeline", parse_method: str = "auto"
) -> None:
    """Write the file-parser service config into system_configs.

    PDF (and scanned-document) parsing on upload calls an external MinerU/pipeline
    service at ``file_parser.api_url``. Excel/CSV/PPTX/text parse in-process and
    need none of this.
    """
    from core.config.store.system_config import SystemConfigService

    svc = SystemConfigService.get_instance()
    # Touch the cache once so the seed rows exist before we set() them
    # (set() skips unknown keys; _load_from_db seeds on first access).
    svc.get_all_configs()
    svc.bulk_set(
        [
            {"key": "file_parser.api_url", "value": api_url},
            {"key": "file_parser.backend", "value": backend},
            {"key": "file_parser.parse_method", "value": parse_method},
        ],
        updated_by="onboard",
    )

# ── Internet search (Tavily / Baidu / LangSearch / DeepSeek key) ─────────────

def configure_search_engine(engine: str, api_key: str) -> None:
    """Write the internet-search engine + key into system_configs.

    Same path as file parser: the DB is the single source of truth (env
    TAVILY_API_KEY/BAIDU_API_KEY/LANGSEARCH_API_KEY/DEEPSEEK_SEARCH_API_KEY only
    as fallback); after writing, the MCP subprocess picks up the new key via
    runtime_env. It can
    also be changed later in the Web UI under Settings -> System Management.
    """
    from core.config.store.system_config import SystemConfigService

    svc = SystemConfigService.get_instance()
    svc.get_all_configs()  # touch cache so seed rows exist before set()
    key_fields = {
        "tavily": "tavily_api_key",
        "baidu": "baidu_api_key",
        "langsearch": "langsearch_api_key",
        "deepseek": "deepseek_api_key",
    }
    if engine not in key_fields:
        raise ValueError(f"不支持的互联网搜索引擎: {engine}")
    svc.bulk_set(
        [
            {"key": "internet_search.engine", "value": engine},
            {"key": f"internet_search.{key_fields[engine]}", "value": api_key},
        ],
        updated_by="onboard",
    )

# ── Site-building template (React path) ──────────────────────────────────────

def report_site_template(verbose: bool = True) -> bool:
    """向导里报一句建站模板的状态；铺设本身在安装站点插件时就做了。"""
    from features.desktop.local.plugins.assets import site_template_home

    home = site_template_home()
    ready = (home / "init-react-site.sh").is_file()
    if not verbose:
        return ready
    if not ready:
        _status("  ! 站点模板未就绪，React 建站不可用", file=sys.stderr)
        return False
    tools = _probe_host_tools()
    if tools["node"] and tools["npm"]:
        _status(f"  ✓ React 建站模板已就绪：{home}")
    else:
        _status(f"  ✓ React 建站模板已铺入：{home}（未检测到 Node/npm，首次构建时需要）")
    return True

def _probe_host_tools() -> dict:
    """Which optional host tools are present (affects local-mode capabilities)."""
    import shutil

    from core.files.content.office import find_libreoffice_binary

    return {
        "node": shutil.which("node") or shutil.which("nodejs"),
        "npm": shutil.which("npm"),
        "pandoc": shutil.which("pandoc"),
        "libreoffice": find_libreoffice_binary(),
        # Asked of the sandbox layer itself rather than probed here, so the
        # doctor can never disagree with what the executor will actually do.
        "os_sandbox": _os_sandbox_status(),
    }

def _os_sandbox_status() -> tuple[str, str]:
    """``(backend name, reason it is unavailable)``; the reason is empty when ready."""
    from core.sandbox.permissions.os_gate import backend_name
    from core.sandbox.permissions.os_gate import confinement_unavailable_reason

    return backend_name(), confinement_unavailable_reason()

def _print_capability_summary() -> None:
    """End-of-onboard note on what host tools gate which capabilities."""
    tools = _probe_host_tools()
    print("\n本机能力概览（缺失项对应能力会降级，可事后补装）：")
    node_ok = bool(tools["node"] and tools["npm"])
    print(
        f"  [{'✓' if node_ok else '·'}] Node.js + npm — React 对话建站"
        + ("" if node_ok else "（未装：建站将只能手写静态站；装 Node ≥ 20 后可用）")
    )
    print(
        f"  [{'✓' if tools['pandoc'] else '·'}] pandoc — Word 文档转换"
        + ("" if tools["pandoc"] else "（未装：Word 转换降级）")
    )
    print(
        f"  [{'✓' if tools['libreoffice'] else '·'}] libreoffice — Office 格式转换"
        + (
            ""
            if tools["libreoffice"]
            else "（未装：PPT/Word 无法预览；重新运行一键安装器可选择补装）"
        )
    )
    sandbox_backend, sandbox_reason = tools["os_sandbox"]
    print(
        f"  [{'✓' if not sandbox_reason else '·'}] {sandbox_backend or 'OS 沙箱'}"
        " — 本机代码执行的文件与网络隔离"
        + ("" if not sandbox_reason else f"（{sandbox_reason}：除「完全放开」外的权限档会拒绝执行）")
    )

def _configure_aux_model_step(
    args, *, ptype: str, title: str, ok_label: str, bu, key, model, interactive: bool
) -> None:
    """Optionally configure a non-chat model role (embedding / reranker).

    Uses flag-provided creds when present, else prompts interactively; a blank
    base_url skips silently. These roles are optional, so a connectivity/config
    failure warns but never aborts onboard.
    """
    if interactive and not bu:
        print(f"\n{title}")
        bu = _prompt("  base_url（留空跳过）", "")
        if bu:
            model = _prompt("  模型名", "")
            import getpass

            key = getpass.getpass("  api_key: ").strip()
    if bu and model:
        try:
            configure_model(bu, key, model, provider_type=ptype, test=not args.no_test)
            print(f"✓ {ok_label}：{model}")
        except Exception as exc:  # noqa: BLE001
            print(f"! {ok_label}失败（该能力将不可用）：{exc}", file=sys.stderr)

def cmd_onboard(args) -> int:
    apply_local_env(args.port)
    print("HugAgentOS 本地初始化\n" + "─" * 40)
    from hugagent_cli.schema import ensure_schema_and_seed

    ensure_schema_and_seed()

    # Step 1 — admin account
    if args.username and args.password:
        username, password = args.username, args.password
    else:
        print("\n[1/2] 设置管理员账号")
        username = _prompt("  用户名", "admin")
        import getpass

        password = getpass.getpass("  密码: ").strip()
        confirm = getpass.getpass("  确认密码: ").strip()
        if password != confirm:
            print("✗ 两次密码不一致", file=sys.stderr)
            return 1
    try:
        user_id = bootstrap_admin(username, password)
        print(f"✓ 管理员账号已创建：{username}（super_admin）")
    except Exception as exc:
        print(f"✗ 账号创建失败：{exc}", file=sys.stderr)
        return 1

    interactive = not (args.username and args.password)

    # Step 2 — chat model (the main model; assigned to all 7 chat roles)
    if args.model_base_url and args.model_api_key and args.model_name:
        base_url, api_key, model_name = args.model_base_url, args.model_api_key, args.model_name
        context_length = args.model_context_length
    else:
        print("\n[2] 配置对话模型（主模型，OpenAI 兼容端点）")
        print("  会指派给全部对话角色（主智能体 / 摘要 / 追问 / 记忆 / 计划 / 代码执行）")
        print("  预设：" + " / ".join(_PRESETS))
        preset = _prompt("  选择预设或直接回车自定义", "").lower()
        pb, pm = _PRESETS.get(preset, ("", ""))
        base_url = _prompt("  base_url", pb)
        model_name = _prompt("  模型名", pm)
        try:
            context_length = int(
                _prompt("  上下文窗口（context_length）", str(DEFAULT_LOCAL_CONTEXT_LENGTH))
            )
        except ValueError:
            print("✗ context_length 必须是正整数", file=sys.stderr)
            return 1
        import getpass

        api_key = getpass.getpass("  api_key: ").strip()
    try:
        configure_model(
            base_url,
            api_key,
            model_name,
            test=not args.no_test,
            context_length=context_length,
        )
        print(f"✓ 模型已配置并联通：{model_name}")
    except Exception as exc:
        print(f"✗ 模型配置失败：{exc}", file=sys.stderr)
        if not args.model_base_url:  # interactive: let them retry later
            print("  可稍后运行 `hugagent-server onboard` 重配。")
        return 1

    # Step 2b (optional) — index/embedding model. Enables the self-built vector
    # knowledge base (embedded Milvus Lite) + L2 memory vectorization.
    _configure_aux_model_step(
        args,
        ptype="embedding",
        title="[2b] 向量 / 索引模型（embedding，知识库检索与记忆用；直接回车跳过）",
        ok_label="向量/索引模型已配置",
        bu=args.embed_base_url,
        key=args.embed_api_key,
        model=args.embed_model,
        interactive=interactive,
    )

    # Step 2c (optional) — reranker model. Re-ranks KB hybrid-search results for
    # sharper retrieval; skippable (retrieval still works without it).
    _configure_aux_model_step(
        args,
        ptype="reranker",
        title="[2c] 重排模型（reranker，知识库检索结果重排增强；直接回车跳过）",
        ok_label="重排模型已配置",
        bu=args.reranker_base_url,
        key=args.reranker_api_key,
        model=args.reranker_model,
        interactive=interactive,
    )

    # Step 3 — plugins. Non-interactive: --plugins <slug,slug|all|none|default>.
    if args.plugins is None and interactive:
        slugs = _select_plugins_interactively(list_installable_plugins())
    else:
        # No --plugins in a non-interactive run == "default"; otherwise parse it.
        raw = (args.plugins or "default").strip().lower()
        avail_slugs = {it["slug"] for it in list_installable_plugins() if not it.get("installed")}
        if raw in ("", "default"):
            slugs = [s for s in _default_plugins() if s in avail_slugs]
        elif raw in ("none", "-"):
            slugs = []
        elif raw == "all":
            slugs = list(avail_slugs)
        else:
            slugs = [
                s.strip() for s in raw.replace("，", ",").split(",") if s.strip() in avail_slugs
            ]
    if slugs:
        print(f"\n[插件] 安装 {len(slugs)} 个插件…")
        installed_slugs = install_plugins(slugs)
        # 站点插件的建站模板在安装时就铺好了，这里只把状态说给用户听。
        if "sites" in installed_slugs:
            report_site_template()

    # Step 4 — file parser (PDF / scanned-document parsing on upload). Optional.
    fp_url = args.file_parser_url
    if fp_url is None and interactive:
        print("\n[可选] 配置 PDF / 文档解析服务（上传 PDF 扫描件时用；直接回车跳过）")
        print("  Excel / CSV / PPTX / 文本无需此项，进程内即可解析。")
        fp_url = _prompt("  文件解析服务 API URL（留空跳过）", "")
    if fp_url:
        try:
            configure_file_parser(fp_url)
            print(f"✓ 文件解析服务已配置：{fp_url}")
        except Exception as exc:  # noqa: BLE001
            print(f"! 文件解析配置失败：{exc}", file=sys.stderr)

    # Step 5 — internet search (Tavily / Baidu / LangSearch / DeepSeek key). Optional.
    search_key = args.search_api_key
    search_engine = (args.search_engine or "tavily").strip().lower()
    if search_key is None and interactive:
        print("\n[可选] 配置互联网搜索（智能体联网检索用；直接回车跳过）")
        print("  支持 tavily（默认）/ baidu（千帆 AppBuilder）/ langsearch / deepseek。")
        eng = _prompt("  搜索引擎 (tavily/baidu/langsearch/deepseek)", "tavily").strip().lower()
        search_engine = eng if eng in ("tavily", "baidu", "langsearch", "deepseek") else "tavily"
        search_key = _prompt("  API Key（留空跳过）", "")
    if search_key:
        try:
            configure_search_engine(search_engine, search_key)
            print(f"✓ 互联网搜索已配置：{search_engine}")
        except Exception as exc:  # noqa: BLE001
            print(f"! 搜索引擎配置失败：{exc}", file=sys.stderr)

    _print_capability_summary()
    mark_web_onboarding_complete(user_id)

    print("\n✓ 初始化完成。", end=" ")
    if args.no_serve:
        print("运行 `hugagent-server` 启动服务。")
        return 0
    print("正在启动服务…\n")
    return cmd_serve(args)

# ── Serve ─────────────────────────────────────────────────────────────────────

def _open_browser_when_ready(port: int) -> None:
    url = f"http://127.0.0.1:{port}/"
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.5)
    else:
        return
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass

def bootstrap_default_plugins_if_needed() -> None:
    """按形态决定要不要装本机默认插件。

    混合模式（桥接已注入）下能力一律以云端账号同步下来的为准，本机不自带。Windows
    壳在双模式下本就不会设引导变量，UOS 壳是无条件设的——判断放在这里，两端和历史
    启动脚本才是同一套规矩。
    """
    from features.desktop.capabilities import device_catalog

    if os.getenv(_DEFAULT_PLUGIN_BOOTSTRAP_ENV) != "1" or device_catalog.active():
        return
    try:
        bootstrapped = ensure_default_plugins_once()
    except Exception as exc:  # noqa: BLE001
        # The desktop readiness endpoint must not go healthy with only a
        # partial default-plugin set. No marker is written on failure, so
        # the next installer-managed start will retry the full bootstrap.
        raise RuntimeError(f"默认插件初始化失败，下次启动将重试：{exc}") from exc
    if bootstrapped:
        _status("✓ 默认插件已就绪：定时任务、技能管理、站点发布")

def cmd_serve(args) -> int:
    apply_local_env(args.port)
    from hugagent_cli.schema import ensure_schema_and_seed

    ensure_schema_and_seed()
    bootstrap_default_plugins_if_needed()
    import uvicorn
    from api.app import app

    host = args.host
    port = int(os.environ["PORT"])
    print(f"HugAgentOS 监听于 http://{host}:{port}/  (Ctrl-C 停止)")
    if not args.no_browser:
        threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()
    # 访问日志由 LoggingMiddleware 统一输出（带 trace_id、耗时、状态码），
    # uvicorn 自带的那条是同一次请求的第二份记录，关掉避免日志翻倍。
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
    return 0

# ── Engine (headless loopback backend for the `hugagent` terminal client) ────
#
# Contract: packages/cli/DESIGN.md §8.1. The terminal client finds a running
# engine through the ready file and talks to it over plain HTTP + SSE with a
# personal API key, exactly like a remote server.

ENGINE_KEY_NAME = "hugagent-cli"
ENGINE_READY_VERSION = 1
EXIT_NOT_INITIALIZED = 3
# How long a second `engine` waits for the one already starting (the client
# itself gives up after 120 s), and how long startup may take to become ready.
_ENGINE_WAIT_SECONDS = 120.0
_ENGINE_READY_TIMEOUT_SECONDS = 300.0

def _engine_error(code: str, **extra) -> None:
    print(json.dumps({"error": code, **extra}, ensure_ascii=False), file=sys.stderr)

def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a probe on Windows: signal 0 is CTRL_C_EVENT.
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and (
                code.value == 259  # STILL_ACTIVE
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def _loopback_get(url: str, token: str = "", timeout: float = 2.0) -> int:
    """Status code of a GET to a loopback URL (0 when unreachable), never via a proxy."""
    from urllib.error import HTTPError
    from urllib.request import ProxyHandler, Request, build_opener

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with build_opener(ProxyHandler({})).open(Request(url, headers=headers), timeout=timeout) as r:
            return r.status
    except HTTPError as exc:
        return exc.code
    except (OSError, ValueError):
        return 0

def _read_live_engine(ready_file: Path, home: Path) -> Optional[dict]:
    """The ready file's contents when it describes a live engine for ``home``."""
    try:
        info = json.loads(ready_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("version") != ENGINE_READY_VERSION:
        return None
    url, pid = info.get("url"), info.get("pid")
    if (
        not isinstance(url, str)
        or not url.startswith("http://127.0.0.1:")
        or not isinstance(pid, int)
        or info.get("home") != str(home)
        or not _process_alive(pid)
    ):
        return None
    return info if _loopback_get(url + "/health") == 200 else None

def _write_ready_file(path: Path, payload: dict) -> None:
    """Atomically publish ``payload`` as a 0600 JSON file (it holds a credential)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise

def _remove_ready_file(path: Path) -> None:
    """Delete the ready file, but only while it still describes this process."""
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") != os.getpid():
            return
        path.unlink()
    except (OSError, ValueError, AttributeError):
        pass

def _try_engine_lock(path: Path):
    """Hold ``path`` exclusively until the returned stack closes; ``None`` when taken."""
    from core.infra.file_lock import LockTimeout, locked

    stack = contextlib.ExitStack()
    try:
        stack.enter_context(locked(path, timeout=0))
    except LockTimeout:
        return None
    return stack

def _engine_admin_user_id() -> Optional[str]:
    """The active local super_admin the engine acts as (oldest first)."""
    from core.db.engine import SessionLocal
    from core.db.models import LocalUser, UserShadow

    db = SessionLocal()
    try:
        rows = (
            db.query(UserShadow)
            .join(LocalUser, LocalUser.user_id == UserShadow.user_id)
            .filter(LocalUser.status == "active")
            .order_by(LocalUser.created_at)
            .all()
        )
        for row in rows:
            if (row.extra_data or {}).get("role") == "super_admin":
                return str(row.user_id)
        return None
    finally:
        db.close()

def issue_engine_token(user_id: str) -> str:
    """Mint this start's ``hugagent-cli`` personal key and revoke the earlier ones.

    Personal keys only authenticate when the owner may use API keys, and the
    local admin has no such flag until something grants it. Grant it when it
    was never set; an explicit ``False`` is the owner's choice and is refused.
    Call it after importing ``api.app``: revoking a key cancels the runs it
    started, which needs the run services the app composes.
    """
    from core.auth.api_keys import ApiKeyService
    from core.auth.users import UserService
    from core.db.engine import SessionLocal
    from core.db.models import UserShadow

    db = SessionLocal()
    try:
        shadow = db.query(UserShadow).filter(UserShadow.user_id == user_id).one()
        allowed = (shadow.extra_data or {}).get("can_use_api_key")
        if allowed is False:
            raise PermissionError("api_key_disabled")
        if allowed is not True:
            UserService(db).update_user_metadata(user_id, {"can_use_api_key": True})
        service = ApiKeyService(db)
        for key in service.list_keys(user_id):
            if key.name == ENGINE_KEY_NAME:
                service.revoke_key(user_id, key.id)
        _row, raw = service.create_key(user_id, ENGINE_KEY_NAME)
        return raw
    finally:
        db.close()

class _ActivityTracker:
    """ASGI wrapper recording when the last HTTP/WebSocket request ended.

    A request in flight (an open SSE stream included) counts as activity for
    as long as it lasts. ``on_shutdown`` runs when lifespan shutdown begins:
    uvicorn re-raises SIGTERM once it has shut down, so code after
    ``server.run`` never sees that path.
    """

    def __init__(self, app, on_shutdown=None) -> None:
        self.app = app
        self.on_shutdown = on_shutdown
        self.in_flight = 0
        self.last_activity = time.monotonic()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan" and self.on_shutdown is not None:
            async def watched_receive():
                message = await receive()
                if message.get("type") == "lifespan.shutdown":
                    self.on_shutdown()
                return message

            return await self.app(scope, watched_receive, send)
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        self.in_flight += 1
        self.last_activity = time.monotonic()
        try:
            return await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1
            self.last_activity = time.monotonic()

    def idle_for(self) -> float:
        return 0.0 if self.in_flight else time.monotonic() - self.last_activity

def _wait_for_other_engine(ready_file: Path, home: Path, lock_path: Path):
    """Another engine holds the lock: wait for its ready file or for the lock."""
    deadline = time.monotonic() + _ENGINE_WAIT_SECONDS
    while time.monotonic() < deadline:
        live = _read_live_engine(ready_file, home)
        if live:
            return live, None
        lock = _try_engine_lock(lock_path)
        if lock is not None:
            return None, lock
        time.sleep(0.5)
    return None, None

def _engine_monitor(server, tracker, *, ready_file: Path, payload: dict,
                    idle_timeout: float, stopped: threading.Event, outcome: dict) -> None:
    """Publish the ready file once the engine serves, then exit it when idle."""
    deadline = time.monotonic() + _ENGINE_READY_TIMEOUT_SECONDS
    while not server.started:
        if stopped.is_set():
            return
        if time.monotonic() > deadline:
            outcome["error"] = "startup_timeout"
            server.should_exit = True
            return
        time.sleep(0.1)
    # An authenticated route proves both that the engine serves and that the
    # ready file never advertises a credential the engine would refuse.
    url = payload["url"]
    status = _loopback_get(url + "/v1/chats?page=1&page_size=1", payload["token"], 30)
    if status != 200:
        outcome["error"] = f"token_check_failed:{status}"
        server.should_exit = True
        return
    _write_ready_file(ready_file, payload)
    tracker.last_activity = time.monotonic()
    _status(f"HugAgentOS engine ready at {url} (pid {os.getpid()}, ready file {ready_file})")
    if idle_timeout <= 0:
        return
    from core.runs.runtime import has_local_runs

    while not stopped.wait(min(5.0, max(0.5, idle_timeout / 4))):
        if tracker.idle_for() >= idle_timeout and not has_local_runs():
            _status(f"HugAgentOS engine idle for {int(idle_timeout)}s, shutting down")
            # Withdraw the file first so no client picks a stopping engine.
            _remove_ready_file(ready_file)
            server.should_exit = True
            return

def cmd_engine(args) -> int:
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    home = data_dir().resolve()
    ready_file = (
        Path(args.ready_file).expanduser().resolve() if args.ready_file else home / "engine.json"
    )
    live = _read_live_engine(ready_file, home)
    if live:
        print(json.dumps(live, ensure_ascii=False))
        return 0
    if not _is_initialized():
        _engine_error("not_initialized")
        return EXIT_NOT_INITIALIZED

    lock_path = home / "engine.lock"
    lock = _try_engine_lock(lock_path)
    if lock is None:
        live, lock = _wait_for_other_engine(ready_file, home, lock_path)
        if live:
            print(json.dumps(live, ensure_ascii=False))
            return 0
        if lock is None:
            _engine_error("engine_busy", lock=str(lock_path))
            return 1
    try:
        return _run_engine(args, home=home, ready_file=ready_file, started_at=started_at)
    finally:
        lock.close()

def _run_engine(args, *, home: Path, ready_file: Path, started_at: str) -> int:
    # Own the listening socket before anything reads PORT: with --port 0 the
    # kernel picks the port, and BACKEND_INTERNAL_URL must name the real one.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(args.port)))
    except OSError as exc:
        sock.close()
        _engine_error("port_unavailable", detail=str(exc))
        return 1
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    # An inherited PORT (or a desktop BACKEND_INTERNAL_URL) must not win here.
    os.environ["PORT"] = os.environ["BACKEND_PORT"] = str(port)
    os.environ["BACKEND_INTERNAL_URL"] = url
    # Headless: point the static hosting at nothing so only the API is served.
    os.environ["FRONTEND_DIST_DIR"] = os.devnull
    try:
        apply_local_env(port)
        from hugagent_cli.schema import ensure_schema_and_seed

        ensure_schema_and_seed()
        bootstrap_default_plugins_if_needed()
        user_id = _engine_admin_user_id()
        if user_id is None:
            _engine_error("not_initialized")
            return EXIT_NOT_INITIALIZED
        import uvicorn
        # Importing the app composes its services; revoking the previous token
        # cancels the runs it started, which needs the run services in place.
        from api.app import app

        try:
            token = issue_engine_token(user_id)
        except PermissionError as exc:
            _engine_error(str(exc))
            return 1

        tracker = _ActivityTracker(app, on_shutdown=lambda: _remove_ready_file(ready_file))
        server = uvicorn.Server(
            uvicorn.Config(tracker, log_level="info", access_log=False, lifespan="on")
        )
        payload = {
            "version": ENGINE_READY_VERSION,
            "url": url,
            "token": token,
            "pid": os.getpid(),
            "started_at": started_at,
            "home": str(home),
        }
        stopped = threading.Event()
        outcome: dict = {}
        monitor = threading.Thread(
            target=_engine_monitor,
            args=(server, tracker),
            kwargs=dict(
                ready_file=ready_file,
                payload=payload,
                idle_timeout=float(args.idle_timeout),
                stopped=stopped,
                outcome=outcome,
            ),
            name="engine-monitor",
            daemon=True,
        )
        monitor.start()
        try:
            # uvicorn's own SIGINT/SIGTERM handling gives the graceful
            # shutdown path: lifespan shutdown stops the sidecars.
            server.run(sockets=[sock])
        except SystemExit as exc:
            # Uvicorn uses 3 for lifespan startup failure. Our wire contract
            # reserves 3 for an uninitialized HOME, so never misreport a
            # sidecar/port/startup failure as onboarding/authentication needed.
            if not server.started and exc.code == 3:
                _engine_error("startup_failed")
                return 1
            raise
        finally:
            stopped.set()
            _remove_ready_file(ready_file)
        if outcome.get("error"):
            _engine_error(outcome["error"])
            return 1
        return 0 if server.started else 1
    finally:
        sock.close()

# ── Doctor ────────────────────────────────────────────────────────────────────

def cmd_doctor(args) -> int:
    apply_local_env(args.port)
    ok = True

    def check(label: str, passed: bool, detail: str = "", *, required: bool = True) -> None:
        nonlocal ok
        mark = "✓" if passed else ("✗" if required else "·")
        print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
        if required:
            ok = ok and passed

    print("HugAgentOS 环境自检\n" + "─" * 40)
    check("Python ≥ 3.11", sys.version_info >= (3, 11), sys.version.split()[0])

    dd = data_dir()
    writable = os.access(dd, os.W_OK)
    check("数据目录可写", writable, str(dd))

    port = int(os.environ["PORT"])
    free = True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
    except OSError:
        free = False
    check(f"端口 {port} 可用", free)

    dist = _resolve_frontend_dist()
    check("前端已构建 (dist)", dist is not None, dist or "缺失：cd src/frontend && npm run build")

    _tools = _probe_host_tools()
    _sandbox_backend, _sandbox_reason = _tools["os_sandbox"]
    check(
        f"OS 沙箱（本机 bash 的文件与网络隔离，后端 {_sandbox_backend or '无'}）",
        not _sandbox_reason,
        _sandbox_reason,
        # Every platform has a backend now, so a missing one is a real defect on
        # any of them rather than an accepted gap on some.
        required=True,
    )
    check(
        "Node.js + npm（可选，React 对话建站）",
        bool(_tools["node"] and _tools["npm"]),
        "" if _tools["node"] and _tools["npm"] else "未装 Node ≥ 20，建站仅能手写静态站",
        required=False,
    )
    check(
        "pandoc（可选，Word 转换 / 文档导出）",
        _tools["pandoc"] is not None,
        "" if _tools["pandoc"] else "未安装，Word 转换降级",
        required=False,
    )
    check(
        "libreoffice（PPT/Word 在线预览、Office 格式转换）",
        _tools["libreoffice"] is not None,
        ("" if _tools["libreoffice"] else "未安装；重新运行一键安装器，或手动安装后重启服务"),
        required=False,
    )

    check(
        "已初始化（存在管理员）",
        _is_initialized(),
        "" if _is_initialized() else "运行 `hugagent-server onboard`",
    )

    print("─" * 40)
    print("✓ 自检通过" if ok else "✗ 存在需处理的问题（见上）")
    return 0 if ok else 1

# ── Entry ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    # Shared options usable both before and after the subcommand
    # (`hugagent-server --host X --port Y serve` and options after `serve` both work).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--host",
        default=os.getenv("HUGAGENT_BIND_HOST", "127.0.0.1"),
        help="监听地址（默认 127.0.0.1；远程访问可显式设为 0.0.0.0）",
    )
    common.add_argument("--port", type=int, default=int(os.getenv("PORT", "3001")))

    p = argparse.ArgumentParser(
        prog="hugagent-server", parents=[common], description="HugAgentOS 无 Docker 本地版"
    )
    sub = p.add_subparsers(dest="command")

    po = sub.add_parser("onboard", parents=[common], help="首次引导（账号 + 模型 + 启动）")
    po.add_argument("--username")
    po.add_argument("--password")
    po.add_argument("--model-base-url")
    po.add_argument("--model-api-key")
    po.add_argument("--model-name")
    po.add_argument(
        "--model-context-length",
        type=int,
        default=int(os.getenv("MODEL_CONTEXT_LENGTH", str(DEFAULT_LOCAL_CONTEXT_LENGTH))),
        help=f"主模型上下文窗口（默认 {DEFAULT_LOCAL_CONTEXT_LENGTH}）",
    )
    po.add_argument("--embed-base-url", help="向量/索引模型端点（可选，用于知识库/记忆）")
    po.add_argument("--embed-api-key")
    po.add_argument("--embed-model")
    po.add_argument("--reranker-base-url", help="重排模型端点（可选，知识库检索增强）")
    po.add_argument("--reranker-api-key")
    po.add_argument("--reranker-model")
    po.add_argument(
        "--plugins", help="要安装的插件：逗号分隔 slug / all / none / default（缺省交互选择）"
    )
    po.add_argument("--file-parser-url", help="PDF/文档解析服务 API URL（可选）")
    po.add_argument(
        "--search-engine",
        choices=["tavily", "baidu", "langsearch", "deepseek"],
        help="互联网搜索引擎（可选，默认 tavily）",
    )
    po.add_argument("--search-api-key", help="互联网搜索 API Key（可选）")
    po.add_argument("--no-test", action="store_true", help="跳过模型连通性实测")
    po.add_argument("--no-serve", action="store_true", help="初始化后不自动起服务")
    po.add_argument("--no-browser", action="store_true")
    po.set_defaults(func=cmd_onboard)

    ps = sub.add_parser("serve", parents=[common], help="启动服务")
    ps.add_argument("--no-browser", action="store_true")
    ps.set_defaults(func=cmd_serve)

    sub.add_parser("doctor", parents=[common], help="环境自检").set_defaults(func=cmd_doctor)

    # No `common` parent: the engine always binds 127.0.0.1 and picks its port.
    pe = sub.add_parser("engine", help="无头本机引擎（供 hugagent 终端客户端使用）")
    pe.add_argument("--port", type=int, default=0, help="监听端口（默认 0：自动挑选空闲端口）")
    pe.add_argument(
        "--idle-timeout",
        type=float,
        default=900,
        help="无请求且无运行中会话多少秒后自动退出（默认 900；0 表示不自动退出）",
    )
    pe.add_argument("--ready-file", help="就绪文件路径（默认 $HUGAGENT_HOME/engine.json）")
    pe.set_defaults(func=cmd_engine)
    return p

def main(argv: Optional[list] = None) -> int:
    _configure_standard_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # bare `hugagent-server`: onboard if fresh, else serve.
        args.func = cmd_serve if _is_initialized() else cmd_onboard
        for attr, val in (
            ("username", None),
            ("password", None),
            ("model_base_url", None),
            ("model_api_key", None),
            ("model_name", None),
            ("model_context_length", DEFAULT_LOCAL_CONTEXT_LENGTH),
            ("embed_base_url", None),
            ("embed_api_key", None),
            ("embed_model", None),
            ("reranker_base_url", None),
            ("reranker_api_key", None),
            ("reranker_model", None),
            ("plugins", None),
            ("file_parser_url", None),
            ("no_test", False),
            ("no_serve", False),
            ("no_browser", False),
        ):
            setattr(args, attr, getattr(args, attr, val))
    return args.func(args) or 0

if __name__ == "__main__":
    sys.exit(main())
