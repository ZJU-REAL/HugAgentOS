"""HugAgentOS no-Docker local CLI (``hugagent`` console entry).

hermes-agent-style quick install: one process, SQLite, in-process fakeredis,
subprocess MCP + sandbox — zero Docker / Postgres / Redis.

    hugagent            # not initialized → onboard; else serve + open browser
    hugagent onboard    # first-run wizard (admin account → model → serve); re-runnable
    hugagent serve      # start the server
    hugagent doctor     # environment self-check

Data lives under ``~/.hugagent/`` (override with ``HUGAGENT_HOME``):
``data.db`` (SQLite), ``storage/``, ``workspace/`` (sandbox), ``logs/``, ``config.env``.

IMPORTANT: this module sets the local-profile environment **before** importing
anything that reads ``core.config.settings`` (settings snapshot env at import
time), so every heavy import is done lazily inside the command functions.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# ── Data dir & environment ────────────────────────────────────────────────────


def data_dir() -> Path:
    return Path(os.getenv("HUGAGENT_HOME", str(Path.home() / ".hugagent"))).expanduser()


def _resolve_frontend_dist() -> Optional[str]:
    """Locate the built frontend for StaticFiles hosting."""
    env = os.getenv("FRONTEND_DIST_DIR", "").strip()
    if env and (Path(env) / "index.html").exists():
        return env
    # src/backend/cli.py → parents[1] = src/backend, .parent = src
    src = Path(__file__).resolve().parents[1]
    cand = src / "frontend" / "dist"
    return str(cand) if (cand / "index.html").exists() else None


def apply_local_env(port: int) -> dict:
    """Populate the local-profile env (idempotent; real env wins) + data dirs."""
    dd = data_dir()
    for sub in ("", "storage", "workspace", "logs", "node", "node/browsers", "fonts"):
        (dd / sub).mkdir(parents=True, exist_ok=True)

    dist = _resolve_frontend_dist()
    defaults = {
        "DEPLOY_PROFILE": "local",
        "JX_EDITION": "ce",
        "BRAND_PRODUCT_NAME": "HugAgentOS",
        "DATABASE_URL": f"sqlite:///{dd / 'data.db'}",
        "REDIS_URL": "memory://",
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
        "SANDBOX_SKILLS_DIR": str(dd / "workspace" / "skills"),
        # Office Agent Skills use locally installed Node packages without
        # requiring a writable global npm prefix.
        "NODE_PATH": str(dd / "node" / "node_modules"),
        "PLAYWRIGHT_BROWSERS_PATH": str(dd / "node" / "browsers"),
        "JX_FONT_DIR": str(dd / "fonts"),
        "MCP_HOST": "127.0.0.1",
        "STORAGE_TYPE": "local",
        "STORAGE_PATH": str(dd / "storage"),
        "LOG_FILE_PATH": str(dd / "logs" / "backend.log"),
        # Self-built vector KB uses embedded Milvus Lite (a local file uri triggers
        # the server-less engine; kb_vector degrades to dense-only there). Requires
        # an embedding model — configure one in `onboard`.
        "MILVUS_URL": str(dd / "milvus.db"),
        # L2/L3 mem0 memory stays off by default (L3 graph needs Neo4j; L2 can be
        # enabled experimentally over the same Lite backend by setting MEM0_ENABLED=true).
        "MEM0_ENABLED": "false",
        # Conversational React site-building: the site-builder skill / init script /
        # vite config all reference a container-canonical /opt/site-template + /workspace.
        # In local mode we provision the template under the data dir and point the
        # scripts at the real workspace via these envs (Docker keeps its baked defaults).
        "SITE_TEMPLATE_HOME": str(dd / "site-template"),
        "SITE_TEMPLATE_DIR": str(dd / "site-template" / "react-vite"),
        "SITE_NODE_BASE": str(dd / "workspace" / ".site-node"),
        "PORT": str(port),
    }
    if dist:
        defaults["FRONTEND_DIST_DIR"] = dist
    for k, v in defaults.items():
        os.environ.setdefault(k, v)
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


# ── DB helpers (import lazily, after env is set) ─────────────────────────────


def _ensure_schema_and_seed():
    """create_all + built-in catalog seed (mirrors the app's startup hooks)."""
    from core.db.engine import SessionLocal, init_db
    from core.services.mcp_service import seed_builtin_mcp_servers_if_empty

    init_db()
    db = SessionLocal()
    try:
        seed_builtin_mcp_servers_if_empty(db)
    finally:
        db.close()


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
    from core.services.local_user_service import LocalUserService
    from core.services.user_service import UserService

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

    from core.db import model_repository as mr
    from core.db.engine import SessionLocal
    from core.services.model_config import ModelConfigService

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
    # Repository writes don't invalidate the read cache — do it explicitly.
    ModelConfigService.get_instance().invalidate_cache()


def mark_web_onboarding_complete(user_id: str) -> None:
    """Avoid repeating the browser setup after the terminal wizard succeeds."""
    from core.db.engine import SessionLocal
    from core.services.local_user_service import CE_ONBOARDING_VERSION
    from core.services.user_service import UserService

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
_DEFAULT_PLUGINS = ["automation", "skill-manager", "sites"]


def list_installable_plugins() -> list:
    """Local-filesystem plugin bundles (default + marketplace) with install state."""
    from core.db.engine import SessionLocal
    from core.services import plugin_service

    db = SessionLocal()
    try:
        items = plugin_service.list_plugins(db, None, include_disabled=True)
    finally:
        db.close()
    # Only the shipped bundles are installable offline; DB-market packages need an
    # admin upload that doesn't exist on a fresh local install.
    return [it for it in items if it.get("source") == "builtin"]


def install_plugins(slugs: list) -> list:
    """Install the given plugin slugs globally (owner_user_id=None). Best-effort."""
    from core.db.engine import SessionLocal
    from core.services import plugin_service

    done = []
    db = SessionLocal()
    try:
        for slug in slugs:
            try:
                plugin_service.install_plugin(db, slug, owner_user_id=None, created_by="onboard")
                done.append(slug)
                print(f"  ✓ 已安装插件：{slug}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! 插件 {slug} 安装失败：{exc}", file=sys.stderr)
    finally:
        db.close()
    return done


def _select_plugins_interactively(available: list) -> list:
    """Show the plugin menu, return the slugs the user picked."""
    print("\n[插件] 选择要安装的插件（可在插件市场随时增减）")
    for i, it in enumerate(available, 1):
        rec = " ★推荐" if it["slug"] in _DEFAULT_PLUGINS else ""
        installed = "（已装）" if it.get("installed") else ""
        desc = (it.get("description") or "")[:42]
        print(f"  {i:>2}. {it['name']}{rec}{installed} — {desc}")
    rec_nums = ",".join(
        str(i) for i, it in enumerate(available, 1) if it["slug"] in _DEFAULT_PLUGINS
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
    from core.services.system_config import SystemConfigService

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


# ── Internet search (Tavily / Baidu key) ─────────────────────────────────────


def configure_search_engine(engine: str, api_key: str) -> None:
    """Write the internet-search engine + key into system_configs.

    Same path as file parser: the DB is the single source of truth (env
    TAVILY_API_KEY/BAIDU_API_KEY only as fallback); after writing, the MCP
    subprocess picks up the new key via runtime_env. It can also be changed
    later in the Web UI under Settings -> System Management.
    """
    from core.services.system_config import SystemConfigService

    svc = SystemConfigService.get_instance()
    svc.get_all_configs()  # touch cache so seed rows exist before set()
    key_field = "baidu_api_key" if engine == "baidu" else "tavily_api_key"
    svc.bulk_set(
        [
            {"key": "internet_search.engine", "value": engine},
            {"key": f"internet_search.{key_field}", "value": api_key},
        ],
        updated_by="onboard",
    )


# ── Site-building template (React path) ──────────────────────────────────────


def _repo_site_template_dir() -> Optional[Path]:
    """Locate the shipped React site template (docker/site-template/) in the repo."""
    # src/backend/cli.py → parents[2] = repo root
    root = Path(__file__).resolve().parents[2]
    cand = root / "docker" / "site-template"
    return cand if (cand / "init-react-site.sh").is_file() else None


def provision_site_template(verbose: bool = False) -> bool:
    """Copy the React site template into the data dir so path-B building works.

    Docker bakes this into the sandbox image at /opt/site-template; the host
    subprocess sandbox has no such layer, so we materialize it under
    ``SITE_TEMPLATE_HOME`` (set in ``apply_local_env``). node_modules is populated
    lazily by the init script on first build (self-heal), keeping onboard fast.
    Returns True if the template is in place.
    """
    import shutil

    src = _repo_site_template_dir()
    home = Path(os.environ.get("SITE_TEMPLATE_HOME", str(data_dir() / "site-template")))
    if src is None:
        if verbose:
            print("  ! 未找到站点模板（docker/site-template/），React 建站不可用")
        return False
    try:
        # Lay down the react-vite template WITHOUT node_modules (init script runs
        # npm install into it on first use) only when it's not already there —
        # leave any built deps alone. The init script itself is always refreshed.
        home.mkdir(parents=True, exist_ok=True)
        if not (home / "init-react-site.sh").is_file():
            shutil.copytree(
                src / "react-vite",
                home / "react-vite",
                ignore=shutil.ignore_patterns("node_modules"),
                dirs_exist_ok=True,
            )
        shutil.copy2(src / "init-react-site.sh", home / "init-react-site.sh")
        os.chmod(home / "init-react-site.sh", 0o755)
        if verbose:
            tools = _probe_host_tools()
            if tools["node"] and tools["npm"]:
                print(f"  ✓ React 建站模板已就绪：{home}")
            else:
                print(
                    f"  ✓ React 建站模板已铺入：{home}"
                    "（缺 Node/npm，装 Node ≥ 20 后首次建站会自动装依赖）"
                )
        return True
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  ! 站点模板铺入失败：{exc}", file=sys.stderr)
        return False


# ── Host tool capability probe ────────────────────────────────────────────────


def _probe_host_tools() -> dict:
    """Which optional host tools are present (affects local-mode capabilities)."""
    import shutil

    from core.content.office import find_libreoffice_binary

    return {
        "node": shutil.which("node") or shutil.which("nodejs"),
        "npm": shutil.which("npm"),
        "pandoc": shutil.which("pandoc"),
        "libreoffice": find_libreoffice_binary(),
    }


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
    _ensure_schema_and_seed()

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
        print("  会指派给全部对话角色（主智能体 / 摘要 / 追问 / 记忆 / 图表 / 计划 / 代码执行）")
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
            print("  可稍后运行 `hugagent onboard` 重配。")
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
            slugs = [s for s in _DEFAULT_PLUGINS if s in avail_slugs]
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
        # Site-building plugin → provision the React project template so path B works locally.
        if "sites" in installed_slugs:
            provision_site_template(verbose=True)

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

    # Step 5 — internet search (Tavily / Baidu key). Optional.
    search_key = args.search_api_key
    search_engine = (args.search_engine or "tavily").strip().lower()
    if search_key is None and interactive:
        print("\n[可选] 配置互联网搜索（智能体联网检索用；直接回车跳过）")
        print("  支持 tavily（默认，tavily.com 注册取 key）/ baidu（千帆 AppBuilder）。")
        eng = _prompt("  搜索引擎 (tavily/baidu)", "tavily").strip().lower()
        search_engine = eng if eng in ("tavily", "baidu") else "tavily"
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
        print("运行 `hugagent` 启动服务。")
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


def cmd_serve(args) -> int:
    apply_local_env(args.port)
    _ensure_schema_and_seed()
    import uvicorn
    from api.app import app

    host = args.host
    port = int(os.environ["PORT"])
    print(f"HugAgentOS 监听于 http://{host}:{port}/  (Ctrl-C 停止)")
    if not args.no_browser:
        threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


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

    try:
        import fakeredis  # noqa: F401

        check("fakeredis 已安装", True)
    except Exception:
        check("fakeredis 已安装", False, "pip install fakeredis")

    _tools = _probe_host_tools()
    check(
        "Node.js + npm（可选，React 对话建站）",
        bool(_tools["node"] and _tools["npm"]),
        "" if _tools["node"] and _tools["npm"] else "未装 Node ≥ 20，建站仅能手写静态站",
        required=False,
    )
    check(
        "pandoc（可选，Word 转换 / 报告导出）",
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
        "" if _is_initialized() else "运行 `hugagent onboard`",
    )

    print("─" * 40)
    print("✓ 自检通过" if ok else "✗ 存在需处理的问题（见上）")
    return 0 if ok else 1


# ── Entry ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    # Shared options usable both before and after the subcommand
    # (`hugagent --host X --port Y serve` and options after `serve` both work).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--host",
        default=os.getenv("HUGAGENT_BIND_HOST", "127.0.0.1"),
        help="监听地址（默认 127.0.0.1；远程访问可显式设为 0.0.0.0）",
    )
    common.add_argument("--port", type=int, default=int(os.getenv("PORT", "3001")))

    p = argparse.ArgumentParser(
        prog="hugagent", parents=[common], description="HugAgentOS 无 Docker 本地版"
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
        "--search-engine", choices=["tavily", "baidu"], help="互联网搜索引擎（可选，默认 tavily）"
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
    return p


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # bare `hugagent`: onboard if fresh, else serve.
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
