"""Agent factory - creates AgentScope agents with pluggable configuration.

This module is separated from core.chat.agent to avoid circular dependencies:
- routing modules can import from this factory
- this factory can import orchestration.registry without creating cycles
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

# AgentScope 2.0
from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.agent._config import ModelConfig  # not in agent's public exports
from agentscope.mcp import MCPClient
from agentscope.tool import Toolkit

from core.agent_skills.loader import get_skill_loader
from core.config.catalog import get_enabled_ids
from core.config.catalog_loader import DB_HIDDEN_SERVERS, DB_UMBRELLA_ID
from core.services.mcp_service import McpServerConfigService
from core.llm.chat_models import get_default_model, make_chat_model
from core.llm.providers.registry import get_spec, split_provider_extra
from core.llm.tool_collector import ToolCollector
from core.llm.middlewares import (
    ActingToolCallIdMiddleware,
    AgentRuntimeState,
    DynamicModelMiddleware,
    FileContextMiddleware,
    FinishPinGuardMiddleware,
    GoalAnchorReminderMiddleware,
    IterBudgetReminderMiddleware,
    WorkspacePinHintMiddleware,
)
from core.llm.tools import (
    ReadStateTracker,
    register_bash,
    register_get_data_context,
    register_delete,
    register_edit,
    register_glob,
    register_grep,
    register_mkdir,
    register_move,
    register_myspace_tools,
    register_pin_to_workspace,
    register_read,
    register_read_artifact,
    register_sandbox_get_artifact,
    register_sandbox_put_artifact,
    register_sandboxed_view_text_file,
    register_write,
)
from core.llm.tools._common import resolve_sandbox_session
from core.llm.mcp_manager import close_clients
from core.llm.mcp_pool import MCPConnectionPool
from prompts.prompt_config import load_prompt_config
from prompts.prompt_runtime import build_system_prompt, build_subagent_system_prompt, select_tools


# Batch-execution-mode system prompt — appended at the end of the regular system prompt.
# All the details of the trigger rules are still carried by the
# batch_runner_mcp.server.batch_plan docstring; this only declares that the user
# has actively chosen the batch entry point, making the model more proactive
# about calling batch_plan.
_BATCH_MODE_HINT = (
    "\n\n## 批量执行模式（用户已主动进入）\n"
    "用户从「应用中心 → 批量执行」入口进入了本会话，明确希望以批量方式处理任务。\n"
    "当用户的请求涉及对一组对象（公司/文件/文本项/行项目等）做同一件事时，\n"
    "**必须优先调用 `batch_plan` 工具**生成可确认的执行计划，不要尝试自己循环回答。\n"
    "调用 `batch_plan` 后立即结束本回合，等待用户在弹窗中确认；\n"
    "确认后系统会自动逐条执行并把结果实时推送给用户，无需你重复调用。\n"
    "若请求确实只针对单一对象/单一概念，再走普通回答即可。\n"
)
from orchestration.registry import AgentSpec

load_dotenv()

# Per-server failure cooldown for HTTP MCP connects. When a server fails
# (upstream 503, transient SSE drop, etc.), skip it for COOLDOWN seconds
# before retrying. Avoids per-request log spam and the noisy anyio
# cancel-scope warnings that come with each failed cleanup.
_HTTP_MCP_FAIL_AT: Dict[str, float] = {}
_HTTP_MCP_FAIL_COOLDOWN_S = 60.0


def _effective_mcp_server_keys(
    cfg,
    agent_spec: Optional[AgentSpec],
    enabled_mcp_ids: Optional[list[str]] = None,
    enabled_kb_ids: Optional[list[str]] = None,
    owned_servers: Optional[dict] = None,
) -> list[str]:
    all_servers = dict(McpServerConfigService.get_instance().get_all_servers(enabled_only=True))
    # Merge in the current user's self-added private MCPs (owner-isolated; already filtered by user_id at the service layer)
    if owned_servers:
        all_servers.update(owned_servers)
    all_keys = list(all_servers.keys())
    # Include the "database query" umbrella id in the gating set so it survives
    # the runtime/catalog/spec intersection filters (it isn't a real server, so
    # at the end it is expanded into the real DB servers and then discarded).
    allow: Set[str] = set(all_keys) | {DB_UMBRELLA_ID}

    # NOTE: Prompt config mcp_servers.enabled whitelist is intentionally
    # skipped here. All MCP servers are now DB-managed via admin panel,
    # and the catalog + user override + runtime filters provide sufficient
    # gating. The legacy prompt config whitelist would block newly added
    # admin MCP servers that aren't in the static config.

    if isinstance(enabled_mcp_ids, list):
        runtime_set = set([x for x in enabled_mcp_ids if isinstance(x, str) and x.strip()])
        allow &= runtime_set
    else:
        catalog_set = set(get_enabled_ids("mcp"))
        allow &= catalog_set

    if agent_spec is not None:
        spec_enabled = getattr(getattr(agent_spec, "mcp_servers", None), "enabled", None) or []
        if spec_enabled:
            spec_set = set([x for x in spec_enabled if isinstance(x, str) and x.strip()])
            allow &= spec_set

    # Note: empty enabled_kb_ids [] means no KBs selected in frontend (e.g. catalog
    # KB list was empty due to Dify being unreachable). We do NOT remove the tool
    # in this case — the MCP impl will auto-resolve available KBs at call time.

    # "Database query" umbrella expansion: when the user/catalog selects the
    # single database_query, allow the actually enabled DB servers under it
    # (query_database / db_query / es_query; apply switches is_enabled by data
    # source type).
    if DB_UMBRELLA_ID in allow:
        allow |= {k for k in all_keys if k in DB_HIDDEN_SERVERS}
    allow.discard(DB_UMBRELLA_ID)

    return [k for k in all_keys if k in allow]


def _filter_mcp_servers_by_keys(
    enabled_keys: list[str], owned_servers: Optional[dict] = None
) -> dict:
    enabled_set = set(enabled_keys)
    all_servers = dict(McpServerConfigService.get_instance().get_all_servers(enabled_only=True))
    if owned_servers:
        all_servers.update(owned_servers)
    return {k: v for k, v in all_servers.items() if k in enabled_set}


def _filter_skill_ids_for_user(skill_ids: list[str], user_id: Optional[str]) -> list[str]:
    """Strip out private skill ids belonging to other users, preventing unauthorized invocation.

    Kept: public skills (owner_user_id empty, including filesystem/built-in
    skills not in the admin_skills table) + the user's own private skills.
    Dropped: other users' private skills.
    """
    if not skill_ids:
        return skill_ids
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill

        with SessionLocal() as db:
            owned = dict(
                db.query(AdminSkill.skill_id, AdminSkill.owner_user_id)
                .filter(
                    AdminSkill.skill_id.in_(skill_ids),
                    AdminSkill.owner_user_id.isnot(None),
                )
                .all()
            )
    except Exception:
        return skill_ids
    return [sid for sid in skill_ids if owned.get(sid) in (None, user_id)]


def _filter_kb_ids_for_user(kb_ids: list[str], user_id: Optional[str]) -> list[str]:
    """Strip out KB ids the current user has no access to (local KBs + Dify datasets), preventing unauthorized ids passed in from the frontend.

    Single source of truth ``core.auth.kb_permissions``: public KBs are visible
    to everyone, private KBs to their owner, and scoped-visibility KBs per
    grant. On failure, fall back to returning the input unchanged (this doesn't
    escalate permissions — it only avoids hurting availability, and the
    downstream retrieve's authorization intercepts again).
    """
    if not kb_ids or not user_id:
        return kb_ids
    try:
        from core.db.engine import SessionLocal
        from core.auth.kb_permissions import filter_accessible_kb_ids

        with SessionLocal() as db:
            return filter_accessible_kb_ids(db, str(user_id), kb_ids)
    except Exception:
        return kb_ids


def _expand_plugin_bindings(plugin_ids: list[str]) -> tuple[list[str], list[str]]:
    """Expand bound plugin install_ids into (skill id list, MCP server id list).

    Takes each plugin's bundled skills / mcp from
    ``InstalledPlugin.component_ids``. Returns empty on failure — best-effort,
    never blocks agent construction.
    """
    if not plugin_ids:
        return [], []
    skills: list[str] = []
    mcp: list[str] = []
    try:
        from core.db.engine import SessionLocal
        from core.db.models import InstalledPlugin

        with SessionLocal() as db:
            rows = (
                db.query(InstalledPlugin).filter(InstalledPlugin.install_id.in_(plugin_ids)).all()
            )
            for r in rows:
                cids = r.component_ids or {}
                skills.extend(cids.get("skills") or [])
                mcp.extend(cids.get("mcp") or [])
    except Exception:  # noqa: BLE001
        return [], []
    return skills, mcp


from core.config.settings import settings as _settings
from core.services.system_config import code_capability_enabled
from mcp_servers._ports import PORTS as _MCP_PORTS

# Fallback URL when a server config is missing ``url``. ``configs/mcp_config.py``
# is the canonical builder; the port comes from mcp_servers/_ports.py (the
# declared single source of truth) instead of a duplicated literal.
KB_MCP_HTTP_URL = f"http://{_settings.server.mcp_host}:{_MCP_PORTS['retrieve_dataset_content']}/mcp/"


def _inject_runtime_headers(
    enabled_servers: dict,
    *,
    current_user_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    enabled_kb_ids: Optional[list[str]] = None,
    channel_origin: Optional[Dict[str, Any]] = None,
    reranker_enabled: bool = False,
) -> dict:
    """Inject the "per-request runtime context" as HTTP headers into ALL enabled MCP servers — no special-casing by server name.

    Each MCP server takes what it needs: KB reads X-Allowed-*/X-Reranker-Enabled,
    scheduled tasks read X-Channel-*/X-Conversation-*, and any server can read
    X-Current-User-Id. New MCP plugins get the context without modifying this
    file ("treated as an ordinary plugin"). streamable_http/sse use headers;
    stdio (a few runtime plugins/legacy paths) falls back to equivalent env
    variables. Injecting into every server is safe — servers that don't care
    simply ignore unknown headers.
    """
    if not enabled_servers:
        return enabled_servers

    normalized = [str(x).strip() for x in (enabled_kb_ids or []) if str(x).strip()]
    dify_ids = [x for x in normalized if not x.startswith("kb_")]
    local_ids = [x for x in normalized if x.startswith("kb_")]
    origin = channel_origin or {}

    ctx_headers = {
        "X-Current-User-Id": current_user_id or "",
        # X-Chat-Id = this chat's id (for web main conversations it is also the
        # sandbox session key). MCPs that need to reach the user's sandbox
        # (site_publish etc.) use it to locate the session; X-Conversation-Id
        # only has a value on external channels (DingTalk etc.).
        "X-Chat-Id": chat_id or "",
        "X-Channel-Id": origin.get("channel_id") or "",
        "X-Conversation-Id": origin.get("conversation_id") or "",
        "X-Allowed-Dataset-Ids": ",".join(dify_ids),
        "X-Allowed-Kb-Ids": ",".join(local_ids),
        "X-Reranker-Enabled": "true" if reranker_enabled else "false",
    }
    ctx_env = {
        "CURRENT_USER_ID": current_user_id or "",
        "CURRENT_CHAT_ID": chat_id or "",
        "DIFY_ALLOWED_DATASET_IDS": ",".join(dify_ids),
        "LOCAL_KB_ALLOWED_IDS": ",".join(local_ids),
        "RERANKER_ENABLED": "true" if reranker_enabled else "false",
    }

    out: dict = {}
    for key, cfg in enabled_servers.items():
        if not isinstance(cfg, dict):
            out[key] = cfg
            continue
        c = dict(cfg)
        is_http = bool(c.get("url")) or c.get("transport") in ("streamable_http", "sse")
        if is_http:
            headers = dict(c.get("headers") or {})
            headers.update(ctx_headers)
            c["headers"] = headers
        else:
            env_cfg = dict(c.get("env") or {})
            env_cfg.update(ctx_env)
            c["env"] = env_cfg
        out[key] = c
    return out


async def warmup_mcp_tools() -> None:
    """Initialize the MCP connection pool at startup.

    Reads MCP server configs from DB (via McpServerConfigService) and
    connects to all stable servers. Per-request servers (e.g.
    retrieve_dataset_content) are spawned on demand.
    """
    import logging
    import time

    log = logging.getLogger(__name__)

    # DB overlays (model config, system config) are already applied inside
    # McpServerConfigService._build_env(), so no manual overlay needed here.
    svc = McpServerConfigService.get_instance()
    servers = svc.get_all_servers(enabled_only=True)

    if not servers:
        log.info("[warmup] No MCP servers configured – skipping warmup")
        return

    log.info("[warmup] Initializing MCP connection pool for %d server(s)…", len(servers))
    start = time.monotonic()

    try:
        pool = MCPConnectionPool.get_instance()
        await pool.initialize(servers)
        elapsed = time.monotonic() - start
        log.info(
            "[warmup] MCP pool initialized: %d stable connections in %.2fs",
            pool.stable_client_count,
            elapsed,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        log.warning("[warmup] MCP pool initialization failed after %.2fs: %s", elapsed, exc)


def _effective_main_available_skills() -> list[str]:
    """Resolve main-agent skills from currently enabled catalog skills."""
    enabled_ids = [sid for sid in get_enabled_ids("skills") if isinstance(sid, str) and sid.strip()]
    if enabled_ids:
        return enabled_ids

    try:
        loader = get_skill_loader()
        discovered = sorted(loader.load_all_metadata().keys())
        if discovered:
            return discovered
    except Exception:
        pass

    return []


async def create_agent_executor(
    agent_spec: Optional[AgentSpec] = None,
    user_query: Optional[str] = None,
    disable_tools: bool = False,
    enabled_skill_ids: Optional[list[str]] = None,
    enabled_mcp_ids: Optional[list[str]] = None,
    enabled_kb_ids: Optional[list[str]] = None,
    current_user_id: Optional[str] = None,
    reranker_enabled: bool = False,
    model_name: Optional[str] = None,
    model_provider_id: Optional[str] = None,
    chat_mode: Optional[str] = None,
    memory_enabled: bool = False,
    user_agent: Optional[Any] = None,
    visible_subagents: Optional[List[Dict[str, Any]]] = None,
    isolated: bool = False,
    max_iters: Optional[int] = None,
    plan_mode: bool = False,
    batch_mode: bool = False,
    # top_level_chat: whether this construction is a "top-level interactive main
    # conversation capable of hosting plan mode" — astream_chat_workflow passes
    # True explicitly after determining (has chat_id, not
    # channel/automation/batch/plan_chat). The enter_plan_mode tool is
    # registered ONLY on this positive signal. All derived/non-interactive paths
    # (plan generation, plan-execute steps, subagents, batch, autonomous loop,
    # channels, non-streaming…) default to False → they naturally never get the
    # tool, eliminating "plan within plan" nesting and all kinds of context
    # leaks at the root.
    top_level_chat: bool = False,
    chat_id: Optional[str] = None,
    sandbox_session_id: Optional[str] = None,
    project_ctx: Optional[Dict[str, Any]] = None,
    channel_origin: Optional[Dict[str, Any]] = None,
    automation_run: bool = False,
    # read_only: read-only agent (for reviewers/auditors) — registers no
    # file-mutating tools (edit/write/delete/move/mkdir/myspace writes/
    # put_artifact), keeping only read/glob/grep/view/get_artifact + bash (bash
    # is for read-only verification; the prompt side constrains it from
    # writing). Mirrors the Codex reviewer's sandbox_mode=read-only.
    read_only: bool = False,
) -> Tuple[Agent, List[MCPClient]]:
    """Create and return an AgentScope 2.0 Agent along with its MCP client list.

    Returns:
        Tuple of (agent, mcp_clients). Caller is responsible for closing
        mcp_clients after use via close_clients().
    """
    import logging
    import time

    _log = logging.getLogger(__name__)
    _t0 = time.monotonic()

    def _elapsed():
        return f"{(time.monotonic() - _t0)*1000:.0f}ms"

    import asyncio

    cfg = load_prompt_config()
    _log.info("[factory] +%s config loaded", _elapsed())
    if agent_spec is not None and agent_spec.prompt_parts:
        cfg = replace(
            cfg,
            system_prompt=replace(cfg.system_prompt, parts=list(agent_spec.prompt_parts)),
        )

    # ── Sub-agent overrides ──────────────────────────────────────────
    if user_agent is not None:
        # Override capability bindings from user_agent config
        enabled_mcp_ids = list(user_agent.mcp_server_ids or [])
        enabled_skill_ids = list(user_agent.skill_ids or [])
        enabled_kb_ids = user_agent.kb_ids or []
        # Expand bound plugins into their component skills + MCPs (a plugin = a detachable capability bundle). Merge with the loose bindings, deduplicated.
        plugin_ids = user_agent.plugin_ids or []
        if plugin_ids:
            p_skills, p_mcp = _expand_plugin_bindings(plugin_ids)
            enabled_skill_ids = list(dict.fromkeys(enabled_skill_ids + p_skills))
            enabled_mcp_ids = list(dict.fromkeys(enabled_mcp_ids + p_mcp))

    # Security: strip out other users' private skills, preventing unauthorized skill_ids passed in from the frontend
    if enabled_skill_ids:
        enabled_skill_ids = _filter_skill_ids_for_user(enabled_skill_ids, current_user_id)

    # Security: strip out KBs the current user has no access to (public-KB
    # permission assignment) — the frontend-supplied enabled_kb_ids may include
    # unauthorized scoped KBs; filter by the user's visible set here so the
    # agent only retrieves from authorized KBs.
    if enabled_kb_ids:
        enabled_kb_ids = _filter_kb_ids_for_user(enabled_kb_ids, current_user_id)

    # The current user's self-added private MCPs (owner-isolated, queried from the DB on demand)
    owned_mcp_servers: dict = {}
    if current_user_id:
        try:
            owned_mcp_servers = McpServerConfigService.get_instance().get_owned_servers(
                str(current_user_id),
                # A sub-agent's binding is explicit and may opt into one of the
                # owner's personally disabled MCPs without enabling it for the
                # main agent. The explicit enabled_mcp_ids list below remains
                # the final allowlist, so unrelated private MCPs are not loaded.
                enabled_only=user_agent is None,
            )
        except Exception:
            owned_mcp_servers = {}

    # Determine which MCP servers to connect
    enabled_mcp_keys = _effective_mcp_server_keys(
        cfg,
        agent_spec,
        enabled_mcp_ids=enabled_mcp_ids,
        enabled_kb_ids=enabled_kb_ids,
        owned_servers=owned_mcp_servers,
    )
    enabled_servers = _filter_mcp_servers_by_keys(enabled_mcp_keys, owned_servers=owned_mcp_servers)
    enabled_servers = _inject_runtime_headers(
        enabled_servers,
        current_user_id=current_user_id,
        chat_id=chat_id,
        enabled_kb_ids=enabled_kb_ids,
        channel_origin=channel_origin,
        reranker_enabled=reranker_enabled,
    )

    # ── Phase 1: Concurrent pre-loading ────────────────────────────────
    # DB overlays, skill metadata, and prompt DB parts are independent —
    # run them in parallel via thread pool to cut first-token latency.

    def _preload_skill_metadata():
        """Pre-warm skill metadata cache so registration is fast."""
        loader = get_skill_loader()
        loader.load_all_metadata()
        return loader

    # DB prompt parts are now pre-loaded at startup via warmup_prompt_cache(),
    # so no need to fetch them per-request.
    # DB-driven env overlays are already applied inside McpServerConfigService,
    # so no manual overlay step is needed here.

    loader = await asyncio.to_thread(_preload_skill_metadata)
    _log.info("[factory] +%s skill metadata pre-loaded", _elapsed())

    # ── Phase 2: MCP toolkit (async, may spawn per-request subprocesses) ──
    mcp_clients: List[MCPClient] = []
    # Stable (pooled) clients must be reused and must never be closed at the end
    # of a request; only transient (per-request spawned) stdio clients go on the
    # close list. mcp_clients contains stable+transient (for Toolkit
    # construction); transient_mcp_clients is only for closing.
    transient_mcp_clients: List[MCPClient] = []
    http_clients: List[MCPClient] = []
    # AgentScope 2.0: the Toolkit is constructed once — there are no incremental
    # register_* calls. Use ToolCollector to duck-type-compatibly collect our
    # in-house tools/skills (the register_* functions barely change), then
    # construct the real Toolkit at the end.
    toolkit = ToolCollector()
    # ⚠️ This is a Jinja2 template, rendered by toolkit.get_skill_instructions()
    # with the ``skills`` variable. It MUST contain the
    # ``{% for skill in skills %}`` loop to actually list the skills — otherwise
    # only the header prints, the skill list is entirely empty, and the model
    # sees no skills and never auto-triggers them (a missing loop once made all
    # skills effectively unloaded, invocable only manually via /). Keeps the
    # Chinese view_text_file guidance + restores the skill-list loop.
    #
    # ⚠️ ``skill.dir`` is the **backend materialized path** AgentScope received
    # when registering the skill (DB skills → /app/storage/sandbox_skills/<id>;
    # built-ins → the source tree). Rendering it directly makes the model take
    # the backend path into bash / file tools (relative references like
    # `./references/...` also get joined onto it), while in the sandbox the
    # skill actually lives at /workspace/skills/<id>. The backend path doesn't
    # exist in the sandbox → ls/python report `No such file or directory`. The
    # _repoint at registration time modifies the ToolCollector (which has no
    # .skills) and has no effect on the final real Toolkit, so here at the
    # render layer we rewrite dir to the sandbox path: the basename IS the skill
    # id (holds uniformly for DB / built-in / private / market skills), and when
    # view_text_file reads it, _resolve_skill_path maps back to the backend
    # file.
    _SKILL_INSTRUCTION_TEMPLATE = (
        "# 技能（Agent Skills）\n"
        "以下是当前可用的技能列表。**技能不是工具，不能直接调用。**\n"
        "当用户请求匹配某技能的描述时，你**必须先**使用 `view_text_file` 工具读取该技能 "
        "`<dir>` 目录下的 `SKILL.md` 文件，然后严格按其中指令执行。\n"
        "**禁止跳过加载步骤直接调用 MCP 工具。**\n\n"
        "# 可用技能：{% for skill in skills %}\n"
        "<skill>\n"
        "<name>{{ skill.name }}</name>\n"
        "<description>{{ skill.description }}</description>\n"
        "<dir>/workspace/skills/{{ skill.dir.rstrip('/').split('/')[-1] }}</dir>\n"
        "</skill>{% endfor %}"
    )

    # Human confirmation for the scheduled-task plugin (borrowed from the §13
    # My Space write confirmation): the gate is attached to automation-mutating
    # tools ONLY in web interactive conversations. Channel runs (IM bots, no
    # approval UI) and non-interactive modes (batch/subagent/plan-execute/no
    # chat_id) never get the gate → pass through directly, no confirmation
    # dialog.
    from core.llm.mcp_confirm import CONFIRM_MCP_SERVERS

    _is_channel_run = bool((channel_origin or {}).get("channel_id"))
    _mcp_confirm_should_gate = (
        chat_id is not None
        and not batch_mode
        and not isolated
        and not plan_mode
        and not _is_channel_run
    )

    if not disable_tools and enabled_servers:
        from core.llm.mcp_pool import HTTP_TRANSPORTS, make_client

        http_server_cfgs = {
            k: v for k, v in enabled_servers.items() if v.get("transport") in HTTP_TRANSPORTS
        }
        stdio_servers = {
            k: v for k, v in enabled_servers.items() if v.get("transport") not in HTTP_TRANSPORTS
        }

        # ``isolated`` callers run in their own event loop (subagent_tool
        # worker threads), so they MUST NOT touch the shared MCP pool — pool
        # clients are bound to the main loop's task scope and would crash
        # anyio on cross-loop teardown. Spawn fresh per-request stdio + HTTP
        # instead, and rely on close_clients() in the caller's loop.
        if isolated:
            from core.llm.mcp_manager import connect_mcp_clients

            mcp_clients = await connect_mcp_clients(stdio_servers)
            transient_mcp_clients = mcp_clients  # all freshly spawned → all closable
            per_request_http = http_server_cfgs
        else:
            pool = MCPConnectionPool.get_instance()
            pool_managed = pool.stable_server_ids if pool.is_initialized else frozenset()
            per_request_http = {k: v for k, v in http_server_cfgs.items() if k not in pool_managed}
            if pool.is_initialized:
                per_request_stdio = {
                    k: v for k, v in stdio_servers.items() if k not in pool_managed
                }
                # 2.0: the pool returns a list of connected MCPClients
                # (stable+transient); the Toolkit(mcps=...) is constructed
                # uniformly below.
                mcp_clients, transient_mcp_clients = await pool.get_request_clients(
                    enabled_keys=enabled_mcp_keys,
                    per_request_servers_cfg=per_request_stdio,
                )
            else:
                from core.llm.mcp_manager import connect_mcp_clients

                mcp_clients = await connect_mcp_clients(stdio_servers)
                transient_mcp_clients = mcp_clients  # pool off → all transient

        # Per-request HTTP — pool can't carry per-request headers that some
        # servers (e.g. retrieve_dataset_content) require. BaseException is
        # caught because the mcp HTTP client's SSE task can propagate
        # CancelledError on transient failures.
        async def _connect_http(key: str, cfg: dict):
            start = time.monotonic()
            last_fail = _HTTP_MCP_FAIL_AT.get(key, 0.0)
            if last_fail and start - last_fail < _HTTP_MCP_FAIL_COOLDOWN_S:
                return None
            # ⚠️ 2.0 key point: HTTP MCP uses is_stateful=False (a new connection
            # per call), avoiding the stateful client's task-binding problem
            # (the connect task differs from the request task → cancel-scope
            # crash, so tool_result is never received). Stateless clients need
            # not and must not connect(); a single list_tools serves as the
            # liveness probe (lazy connect + enumerate, verifying reachability),
            # after which the Toolkit opens a fresh connection on every call.
            _http_cfg = {**cfg, "url": cfg.get("url", KB_MCP_HTTP_URL)}
            # Confirm-required servers (e.g. automation_task) are swapped for a
            # gated client in web interactive conversations: before their
            # mutating tools' __call__, gate() suspends awaiting user
            # confirmation.
            if _mcp_confirm_should_gate and key in CONFIRM_MCP_SERVERS:
                from core.llm.mcp_confirm import (
                    confirm_specs_for,
                    make_confirm_gated_client,
                )

                client = make_confirm_gated_client(
                    key, _http_cfg, chat_id=chat_id, specs=confirm_specs_for(key)
                )
            else:
                client = make_client(key, _http_cfg, is_stateful=False)
            try:
                await client.list_tools()
                _HTTP_MCP_FAIL_AT.pop(key, None)
                _log.info(
                    "[factory] HTTP MCP '%s' (stateless) probed in %.0fms",
                    key,
                    (time.monotonic() - start) * 1000,
                )
                return client
            except BaseException as exc:
                _HTTP_MCP_FAIL_AT[key] = time.monotonic()
                _log.warning(
                    "[factory] HTTP MCP '%s' connect failed (%s, cooldown %.0fs): %s",
                    key,
                    type(exc).__name__,
                    _HTTP_MCP_FAIL_COOLDOWN_S,
                    exc,
                )
                # Only propagate CancelledError when the *outer* task is itself
                # being cancelled (real user/system cancel). anyio's SSE-client
                # cleanup raises CancelledError as a scope-exit signal even when
                # nobody cancelled us — re-raising those was killing the whole
                # chat run whenever any single HTTP MCP (e.g. a freshly-removed
                # word_mcp / ppt_mcp / excel_mcp / pdf_mcp whose admin_mcp_servers row was still
                # ``is_enabled=true``) was unreachable.
                if isinstance(exc, asyncio.CancelledError):
                    current = asyncio.current_task()
                    if current is not None and getattr(current, "cancelling", lambda: 0)() > 0:
                        raise
                return None

        if per_request_http:
            results = await asyncio.gather(
                *(_connect_http(k, v) for k, v in per_request_http.items()),
                return_exceptions=False,
            )
            http_clients.extend(c for c in results if c is not None)

        _log.info(
            "[factory] +%s MCP tools loaded (transient_stdio=%d, http=%d)",
            _elapsed(),
            len(mcp_clients),
            len(http_clients),
        )

    # ── Phase 3: Skill registration (fast — metadata already cached) ──
    # disable_tools=True is a "bare LLM" mode used by plan-generate and the
    # final-summary pass: caller wants pure text output, no tool access at
    # all. Skip skills AND sandbox/artifact/file tools — otherwise the agent
    # happily calls bash/view_text_file mid-generation and corrupts JSON.
    skill_ids_to_register = enabled_skill_ids
    if skill_ids_to_register is None:
        skill_ids_to_register = _effective_main_available_skills()
    # Note: a subagent's (user_agent) enabled_skill_ids is always a list ([]
    # when unconfigured) and never hits the None fallback above — i.e. "a
    # subagent with no skills configured has no skills"; strictly per its own
    # config, no inheriting the full catalog set.

    allowed_skill_dirs: list[str] = []
    if not disable_tools and skill_ids_to_register:
        n = loader.register_skills_to_toolkit(toolkit, skill_ids_to_register)
        if n > 0:
            _log.info("Registered %d agent skills to toolkit", n)
        for sid in skill_ids_to_register:
            d = loader.get_skill_dir(sid)
            if d:
                allowed_skill_dirs.append(d)

    if not disable_tools:
        from core.agent_skills.config import get_enabled_skill_sources, get_sandbox_skills_dir

        for src in get_enabled_skill_sources():
            root = str(src.root_dir)
            if os.path.isdir(root) and root not in allowed_skill_dirs:
                allowed_skill_dirs.append(root)
        # Unified skills dir (DB skills materialize here; see get_sandbox_skills_dir).
        # Blanket-allow it so view_text_file can read any materialized skill, even
        # one not in skill_ids_to_register.
        _skills_root = str(get_sandbox_skills_dir())
        if _skills_root not in allowed_skill_dirs:
            allowed_skill_dirs.append(_skills_root)

    _log.info("[factory] +%s skills registered", _elapsed())

    loaded_skill_ids: set[str] = set()
    # Effective sandbox session: callers may pass an explicit id to layer sessions
    # (main/plan execution → chat_id persistent kernel; batch/subagent → "" ephemeral).
    # ``None`` means "not specified" → fall back to chat_id (legacy behavior).
    _sbx_sess: Optional[str] = resolve_sandbox_session(sandbox_session_id, chat_id)
    # Interactive mode = a human is in the loop and confirmations can be shown
    # (main/plan-execute). Batch items / subagents (isolated/batch) have no
    # human in the loop → non-interactive, and §13 rejects /myspace writes
    # outright.
    _interactive: bool = not (isolated or batch_mode)
    if not disable_tools:
        register_sandboxed_view_text_file(
            toolkit,
            allowed_skill_dirs,
            loader,
            loaded_skill_ids=loaded_skill_ids,
        )

        # ── Phase 3.5: Register sandbox tools (bash + artifact in/out) ──
        # Skill files reach the sandbox via the unified /workspace/skills bind
        # mount (built-in synced at startup, DB skills materialized on demand —
        # see agent_skills.config.get_sandbox_skills_dir), so bash needs no
        # per-call sync. loader/loaded_skill_ids kept for backward compat.
        register_bash(
            toolkit,
            loader=loader,
            loaded_skill_ids=loaded_skill_ids,
            chat_id=chat_id,
            sandbox_session_id=_sbx_sess,
            user_id=current_user_id,
            interactive=_interactive,
        )
        if not read_only:
            register_sandbox_put_artifact(
                toolkit,
                chat_id=chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=current_user_id,
            )
        register_sandbox_get_artifact(
            toolkit,
            chat_id=chat_id,
            sandbox_session_id=_sbx_sess,
            user_id=current_user_id,
        )
        # Site publishing is now plugin-based: the sites plugin's site_publish
        # MCP provides the publish_site tool; the built-in native tool is no
        # longer registered here (see mcp_servers/site_publish_mcp +
        # plugin_bundles/marketplace/sites).

        # Site-builder design pick (choose one of three): registered only for
        # sessions with the site-builder skill enabled (per-run conditional
        # registration, not in the catalog). Interactivity uses the second-tier
        # judgment _ui_reachable (≠ _interactive): write confirmation has the
        # allow_session out-of-band pre-authorization path on IM channels and
        # the automation confirmation panel in automation sessions, but the
        # suspended picker has no clickable UI in either place — so degrade to
        # non-interactive (the tool just lets the model pick its own design
        # instead of suspending for 2h).
        from core.llm.tools import design_picker_tool

        _ui_reachable = _interactive and not _is_channel_run and not automation_run
        if skill_ids_to_register and any(
            design_picker_tool.skill_uses_choose_design(str(sid))
            for sid in skill_ids_to_register
        ):
            design_picker_tool.register_choose_design(
                toolkit,
                chat_id=chat_id,
                interactive=_ui_reachable,
            )

        # ── Phase 3.6: Register file-operation tools (Read/Edit/Write/Glob/
        # Grep/Delete/Move + myspace). These tools share a single
        # ReadStateTracker, keeping the Edit/Write "must Read first" invariant
        # consistent across multiple tool calls.
        #
        # Gating (docs §3.2): CODE_CAPABILITY_ENABLED=true → available by
        # default in all modes. This block is already nested inside
        # `if not disable_tools:`, so the plan-generation phase naturally gets
        # no file capability.
        # Project mode: hooks up the folder name + subtree scoping; the fs/
        # MySpace tools below and pin_to_workspace (Phase 3.8) share the same
        # scope.
        _proj_folder_name = (project_ctx or {}).get("project_folder_name") or None
        from core.services.project_scope import project_scope_from_context

        _proj_scope = project_scope_from_context(project_ctx or {})
        if code_capability_enabled():
            _read_state = ReadStateTracker()
            register_read(
                toolkit,
                chat_id=chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=current_user_id,
                state=_read_state,
                project_folder_name=_proj_folder_name,
                scope=_proj_scope,
            )
            if not read_only:
                register_edit(
                    toolkit,
                    chat_id=chat_id,
                    sandbox_session_id=_sbx_sess,
                    user_id=current_user_id,
                    state=_read_state,
                    interactive=_interactive,
                    project_folder_name=_proj_folder_name,
                    scope=_proj_scope,
                )
                register_write(
                    toolkit,
                    chat_id=chat_id,
                    sandbox_session_id=_sbx_sess,
                    user_id=current_user_id,
                    state=_read_state,
                    interactive=_interactive,
                    project_folder_name=_proj_folder_name,
                    scope=_proj_scope,
                )
            register_glob(
                toolkit,
                chat_id=chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=current_user_id,
                project_folder_name=_proj_folder_name,
                scope=_proj_scope,
            )
            register_grep(
                toolkit,
                chat_id=chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=current_user_id,
                project_folder_name=_proj_folder_name,
                scope=_proj_scope,
            )
            if not read_only:
                register_delete(
                    toolkit,
                    chat_id=chat_id,
                    sandbox_session_id=_sbx_sess,
                    user_id=current_user_id,
                    state=_read_state,
                    interactive=_interactive,
                    project_folder_name=_proj_folder_name,
                    scope=_proj_scope,
                )
                register_move(
                    toolkit,
                    chat_id=chat_id,
                    sandbox_session_id=_sbx_sess,
                    user_id=current_user_id,
                    state=_read_state,
                    interactive=_interactive,
                    project_folder_name=_proj_folder_name,
                    scope=_proj_scope,
                )
                register_mkdir(
                    toolkit,
                    chat_id=chat_id,
                    sandbox_session_id=_sbx_sess,
                    user_id=current_user_id,
                    interactive=_interactive,
                    project_folder_name=_proj_folder_name,
                    scope=_proj_scope,
                )
                register_myspace_tools(
                    toolkit,
                    user_id=current_user_id,
                    scope=_proj_scope,
                )

        # ── Phase 3.7: Register read_artifact for cross-turn file access ──
        # Unconditional: any user may have uploaded files in prior turns of this chat,
        # and the hook injects historical-file summaries referencing this tool.
        register_read_artifact(toolkit, user_id=current_user_id)

        # ── Phase 3.8: Register pin_to_workspace ──
        # Lets the agent gate which generated files reach the user-visible
        # assistant message. See core/llm/workspace.py for the per-run state.
        register_pin_to_workspace(toolkit, scope=_proj_scope)

        # ── Phase 3.9: get_data_context (the "data dictionary" tool for direct-DB data retrieval) ──
        # Three gates combined: (1) a direct DB server is enabled this run
        # (db_query / es_query); (2) the external NL2SQL black box is excluded
        # (query_database isn't in the set, so it naturally doesn't trigger);
        # (3) the corresponding data source has annotation content. If any is
        # unmet, don't attach — avoid adding a useless tool that misleads the
        # model. The metadata only ever appears as a tool return value, never in
        # the system prompt. See db_metadata_service.
        _db_servers_on = {"db_query", "es_query"} & set(enabled_mcp_keys)
        if _db_servers_on:
            try:
                from core.services import db_metadata_service as _dbmeta

                _eligible_ds = await asyncio.to_thread(
                    _dbmeta.eligible_datasource_ids, _db_servers_on
                )
            except Exception as _e:  # noqa: BLE001
                _eligible_ds = []
                _log.warning("[factory] eligible_datasource_ids failed: %s", _e)
            if _eligible_ds:
                register_get_data_context(toolkit, _eligible_ds)

    # ── Phase 4: Build system prompt (DB parts pre-fetched) ──
    _agent_ref: Optional[Dict] = None

    def _build_toolkit() -> Toolkit:
        # ``toolkit`` here is still the ToolCollector; construct the real Toolkit from the current collection state.
        return Toolkit(
            tools=toolkit.function_tools,
            mcps=[*mcp_clients, *http_clients],
            skills_or_loaders=toolkit.skill_loaders or None,
            skill_instruction_template=_SKILL_INSTRUCTION_TEMPLATE,
        )

    # Compute schemas first in the "subagent tools not yet registered" state
    # (consistent with 1.x: subagent tools are registered after
    # get_json_schemas, so they don't enter the system_prompt's tool list).
    tool_schemas = await _build_toolkit().get_tool_schemas()
    if user_agent is not None:
        system_prompt = build_subagent_system_prompt(
            user_agent,
            tool_schemas,
            enabled_mcp_keys,
            enabled_kb_ids=enabled_kb_ids,
        )
        _log.info(
            "[factory] +%s subagent system prompt built (%d chars)", _elapsed(), len(system_prompt)
        )
    else:
        _sp_ctx: Dict[str, Any] = {
            "tools": tool_schemas,
            "mcp_servers": enabled_mcp_keys,
            "enabled_kbs": enabled_kb_ids,
        }
        # Project mode: let _build_project_section receive project_name / instructions / files / folder
        if project_ctx:
            _sp_ctx.update(project_ctx)
        system_prompt = build_system_prompt(cfg, ctx=_sp_ctx)
        _log.info("[factory] +%s system prompt built (%d chars)", _elapsed(), len(system_prompt))

        # ── Inject code-capability system prompt ──
        # Gating: CODE_CAPABILITY_ENABLED=true injects in all modes.
        # Single source of truth render_code_capability_segment (same source as the Config console preview).
        if code_capability_enabled():
            try:
                from core.services import prompt_version_service as _pvs

                _code_exec_text = _pvs.render_code_capability_segment()
            except Exception:
                _code_exec_text = ""
            if _code_exec_text:
                system_prompt += "\n\n" + _code_exec_text
                _log.info(
                    "[factory] +%s code execution prompt injected (%d chars)",
                    _elapsed(),
                    len(_code_exec_text),
                )

        # ── Inject batch execution hint (App Center batch-execution sessions only) ──
        if batch_mode:
            system_prompt += _BATCH_MODE_HINT
            _log.info("[factory] +%s batch mode hint injected", _elapsed())

        # ── Register call_subagent tool for main agent ──
        if visible_subagents:
            from core.llm.subagent_tool import register_subagent_tool, build_subagent_prompt_section

            _agent_ref = {"agent": None}  # set after creation
            register_subagent_tool(
                toolkit,
                visible_subagents,
                current_user_id or "",
                agent_ref=_agent_ref,
                chat_id=chat_id,
            )
            # `mentioned_agent_ids` is consumed by the caller via
            # build_subagent_mention_hint() and injected into the current user
            # message — NOT into the system prompt. Keeping it out of the
            # system prompt preserves the LLM provider's prefix cache.
            subagent_section = build_subagent_prompt_section(visible_subagents)
            if subagent_section:
                system_prompt = system_prompt + "\n\n" + subagent_section
            _log.info(
                "[factory] +%s subagent tool registered (%d agents)",
                _elapsed(),
                len(visible_subagents),
            )

        # ── Register enter_plan_mode tool (top-level interactive main conversations only, positive opt-in) ──
        # Lets the main agent proactively switch into plan mode when it judges a
        # task complex enough (generate plan → user confirms → execute).
        # Recognizes ONLY the single positive signal top_level_chat (passed in
        # by astream_chat_workflow after it determines this is an interactive
        # main conversation) — not a negative exclusion list of "not batch and
        # not plan_mode and not …". A negative list leaks the tool with every
        # derived context it misses (historically, plan-execute steps,
        # plan-generation disable_tools, and channel runs all leaked this way,
        # producing "plan within plan" nesting); a positive opt-in has one
        # single source of truth, and all derived/non-interactive constructions
        # get nothing by default. The DB switch auto_plan_entry_enabled (which
        # itself returns False on config-layer errors) can turn this off
        # entirely.
        from core.services.system_config import auto_plan_entry_enabled

        if top_level_chat and auto_plan_entry_enabled():
            from core.llm.plan_entry_tool import (
                build_enter_plan_prompt_section,
                register_enter_plan_tool,
            )

            register_enter_plan_tool(toolkit)
            _ep_section = build_enter_plan_prompt_section()
            if _ep_section:
                system_prompt = system_prompt + "\n\n" + _ep_section
            _log.info(
                "[factory] +%s enter_plan_mode tool registered (chat_id=%s)",
                _elapsed(), chat_id,
            )

    # Create model (streaming enabled for SSE)
    # Mode-specific model role: plan mode → plan_agent → falls back to
    # main_agent; everything else → main_agent. Code execution is not a
    # standalone mode and does not select a model by code_exec (docs §6). The
    # `code_exec` role is kept as an optional ops override (operators can map it
    # explicitly in model_config), but it is not referenced by default.
    default_model = None
    _selected_provider_cfg = None
    _selected_provider_id = (model_provider_id or "").strip()
    if _selected_provider_id:
        try:
            from core.services.model_config import ModelConfigService

            _selected_provider_cfg = ModelConfigService.get_instance().resolve_provider(
                _selected_provider_id
            )
            if _selected_provider_cfg:
                _mode = (chat_mode or "medium").lower()
                _disable_thinking = _mode == "fast"
                _supports_effort = bool(
                    (_selected_provider_cfg.extra or {}).get("supports_reasoning_effort")
                )
                _reasoning_effort = (
                    _mode
                    if (
                        not _disable_thinking
                        and _supports_effort
                        and _mode in ("medium", "high", "max")
                    )
                    else None
                )
                default_model = make_chat_model(
                    model=_selected_provider_cfg.model_name,
                    temperature=_selected_provider_cfg.temperature,
                    max_tokens=_selected_provider_cfg.max_tokens,
                    timeout=_selected_provider_cfg.timeout,
                    base_url=_selected_provider_cfg.base_url,
                    api_key=_selected_provider_cfg.api_key,
                    provider=_selected_provider_cfg.provider,
                    provider_extra=_selected_provider_cfg.provider_extra,
                    disable_thinking=_disable_thinking,
                    reasoning_effort=_reasoning_effort,
                    stream=True,
                )
                _log.info(
                    "[factory] using user-selected model: %s",
                    _selected_provider_cfg.model_name,
                )
        except Exception as exc:
            _log.warning("[factory] selected model resolve failed: %s, falling back", exc)
    _mode_role = "plan_agent" if plan_mode else None
    if default_model is None and _mode_role:
        try:
            from core.services.model_config import ModelConfigService

            _mode_cfg = ModelConfigService.get_instance().resolve(_mode_role)
            if _mode_cfg:
                default_model = make_chat_model(
                    model=_mode_cfg.model_name,
                    temperature=_mode_cfg.temperature,
                    max_tokens=_mode_cfg.max_tokens,
                    timeout=_mode_cfg.timeout,
                    base_url=_mode_cfg.base_url,
                    api_key=_mode_cfg.api_key,
                    provider=_mode_cfg.provider,
                    provider_extra=_mode_cfg.provider_extra,
                    stream=True,
                )
                _log.info("[factory] using %s model: %s", _mode_role, _mode_cfg.model_name)
        except Exception as exc:
            _log.warning(
                "[factory] %s model resolve failed: %s, falling back to main_agent", _mode_role, exc
            )
    if default_model is None:
        default_model = get_default_model(cfg.model, stream=True)

    # ── Sub-agent config override (model / temperature / max_tokens) ──
    # Triggers when user_agent specifies a custom model provider, a non-null
    # temperature, or a non-null max_tokens. Non-overridden fields fall back to
    # the main_agent model config so temperature-only overrides still work.
    # A subagent with an explicitly configured model → set the pin; downstream DynamicModelMiddleware must not override it by chat_mode.
    _subagent_model_pinned = False
    if user_agent is not None:
        _user_temp = float(user_agent.temperature) if user_agent.temperature is not None else None
        _user_max_tokens = user_agent.max_tokens or None
        _user_timeout = user_agent.timeout or None
        _user_provider_id = user_agent.model_provider_id

        if _user_provider_id or _user_temp is not None or _user_max_tokens:
            try:
                from core.db.engine import SessionLocal
                from core.db.models import ModelProvider
                from core.services.model_config import ModelConfigService

                provider = None
                if _user_provider_id:
                    with SessionLocal() as _db:
                        provider = (
                            _db.query(ModelProvider)
                            .filter(
                                ModelProvider.provider_id == _user_provider_id,
                                ModelProvider.is_active == True,
                            )
                            .first()
                        )

                # Fallback model config (main_agent) for params the user didn't override
                _fallback_cfg = ModelConfigService.get_instance().resolve("main_agent")

                _final_model = (
                    provider.model_name
                    if provider
                    else (_fallback_cfg.model_name if _fallback_cfg else None)
                )
                _final_base_url = (
                    provider.base_url
                    if provider
                    else (_fallback_cfg.base_url if _fallback_cfg else None)
                )
                _final_api_key = (
                    provider.api_key
                    if provider
                    else (_fallback_cfg.api_key if _fallback_cfg else None)
                )
                if provider:
                    _final_provider = getattr(provider, "provider", None) or "openai_compatible"
                    _final_provider_extra = split_provider_extra(
                        get_spec(_final_provider), provider.extra_config or {}
                    )
                else:
                    _final_provider = (
                        _fallback_cfg.provider if _fallback_cfg else "openai_compatible"
                    )
                    _final_provider_extra = _fallback_cfg.provider_extra if _fallback_cfg else {}
                _final_temp = (
                    _user_temp
                    if _user_temp is not None
                    else (_fallback_cfg.temperature if _fallback_cfg else 0.6)
                )
                _final_max_tokens = _user_max_tokens or (
                    _fallback_cfg.max_tokens if _fallback_cfg else 8192
                )
                _final_timeout = _user_timeout or (_fallback_cfg.timeout if _fallback_cfg else 120)

                if _final_model and _final_base_url and _final_api_key:
                    default_model = make_chat_model(
                        model=_final_model,
                        temperature=_final_temp,
                        max_tokens=_final_max_tokens,
                        timeout=_final_timeout,
                        base_url=_final_base_url,
                        api_key=_final_api_key,
                        provider=_final_provider,
                        provider_extra=_final_provider_extra,
                        stream=True,
                    )
                    # Only an explicitly selected model provider pins; changing
                    # only temp/max_tokens (provider is None, model falls back
                    # to the main config) does not pin, preserving dynamic
                    # chat_mode switching.
                    _subagent_model_pinned = provider is not None
                    _log.info(
                        "[factory] subagent config override: model=%s, temp=%s, max_tokens=%s, pinned=%s",
                        _final_model,
                        _final_temp,
                        _final_max_tokens,
                        _subagent_model_pinned,
                    )
                else:
                    _log.warning(
                        "[factory] subagent override skipped: missing model/base_url/api_key"
                    )
            except Exception as exc:
                _log.warning("[factory] subagent config override failed: %s, using default", exc)

    _log.info("[factory] +%s model created", _elapsed())

    # ── Compression-window logging: read the actually effective context_size directly off the model object ──
    # make_chat_model already resolves the real context_length from the Config
    # model configuration and bakes it into the model (no default fallback —
    # construction errors when unconfigured), so what we log here is exactly the
    # value the AS2 compression decision actually uses; we no longer resolve a
    # separate "logging-only" window (the old implementation's log once
    # disagreed with the actually effective value).
    _ctx_window = int(getattr(default_model, "context_size", 0) or 0)
    # In-turn compression ratio: the Config console's "System settings →
    # Conversation & context compression" DB config takes precedence
    # (chat.compress_in_turn_ratio, effective ≤30s after saving, no restart
    # needed); the env CHAT_COMPRESS_IN_TURN_RATIO is only the default
    # fallback. Out-of-range/invalid values are ignored.
    _in_turn_ratio = _settings.compaction.in_turn_trigger_ratio
    try:
        from core.services.system_config import SystemConfigService

        _db_ratio = (SystemConfigService.get_instance().get("chat.compress_in_turn_ratio") or "").strip()
        if _db_ratio:
            _parsed = float(_db_ratio)
            if 0.1 <= _parsed <= 0.99:
                _in_turn_ratio = _parsed
            else:
                _log.warning("[factory] chat.compress_in_turn_ratio 越界(%s)，忽略", _db_ratio)
    except Exception as _cfg_exc:  # noqa: BLE001
        _log.warning("[factory] 读 chat.compress_in_turn_ratio 失败: %s", _cfg_exc)
    _log.info(
        "[factory] CompressionConfig: model=%s, context_size=%d, trigger_threshold=%d (ratio=%s)",
        getattr(default_model, "model", None) or "(unknown)",
        _ctx_window,
        int(_ctx_window * _in_turn_ratio),
        _in_turn_ratio,
    )

    # AgentScope 2.0: CompressionConfig → ContextConfig; trigger_threshold
    # (absolute) → trigger_ratio (fraction). There is no compression_model slot
    # — L1/L2 are handled by the framework per reserve_ratio/tool_result_limit,
    # and the L3 fallback already lives in
    # StructuredFallbackMixin.generate_structured_output.
    # The ratio is configurable (env CHAT_COMPRESS_IN_TURN_RATIO, default 0.82):
    # 2.0's count_tokens estimates via utf-8 bytes/4 — overestimates Chinese,
    # near-accurate for English/code. We initially used 0.6 to compensate for
    # the overestimate, but in practice it triggered too early (compression
    # kicked in while real occupancy was far below the threshold, and
    # tool-heavy sessions compressed repeatedly), so it has been relaxed.
    context_config = ContextConfig(
        trigger_ratio=_in_turn_ratio,
        tool_result_limit=20_000,
        compression_prompt=(
            "<system-hint>你一直在处理上述任务但尚未完成，对话历史即将被本摘要替换。"
            "请生成一份【可恢复 ReAct 工作流】的结构化续写摘要，使你能在新的上下文窗口"
            "中继续按工具调用方式推进任务，而不是把已完成的结果当成定论复述。\n\n"
            "摘要必须使用中文，并按下列固定小节输出（缺项写「无」，禁止省略小节）：\n"
            "1. 用户原始诉求：完整复述用户最近一次（以及尚未满足的更早一次）请求的"
            "原文/核心意图，包含字数、章节数、表格、图表等可量化约束。\n"
            "2. 已调用工具记录（按时间顺序，每条一行）：\n"
            "   tool_name(关键参数=值, …) → 关键产出/artifact_id/错误。\n"
            "   - 必须保留所有 artifact_id、文件名、storage_url、chart_id、kb_id 等"
            "下游可引用的 ID；找不到具体值时写 <missing> 而不是省略。\n"
            "   - 工具的关键入参（如 query、symbol、file_id、template_id、industry、"
            "section_title 等）至少保留一项，便于复算。\n"
            "3. 关键事实数据（来自工具结果，必须标注来源工具名）：列出后续推理还会"
            "用到的数字/结论；【禁止把上一题的数据迁移到新题】——若新任务主题不同，"
            "明确写「以下数据仅适用于<旧主题>，新主题需重新调用工具获取」。\n"
            "4. 当前进度与下一步：明确列出「已完成 / 进行中 / 待办」三栏；待办里写出"
            "需要再次调用哪些工具、用什么参数。\n"
            "5. 注意事项：用户的偏好、不要再犯的错误、模板/格式约束等。\n\n"
            "硬性要求：\n"
            "- 不要写成「报告已生成完毕」这类结论性语言，除非用户已明确表示满意。\n"
            "- 工具调用清单必须真实、来自历史 tool_use/tool_result，禁止编造。\n"
            "- 如果用户的最新请求是「按上一份的模板再做一份 X」，必须在「待办」中明确"
            "写出「需重新调用<相应工具>获取 X 的真实数据，不得复用旧主题数字」。\n"
            "</system-hint>"
        ),
    )

    # ── Phase 5: Long-term memory ──
    #
    # **Important change**: starting with the layered-memory architecture, we no
    # longer use AgentScope's native long_term_memory mounting
    # (`long_term_memory=...` + `static_control` mode), because:
    #
    # 1. `ReActAgent._retrieve_from_long_term_memory` **synchronously awaits**
    #    the mem0 vector retrieval before every reply, dragging Milvus latency
    #    straight into the SSE first-frame latency.
    # 2. Before the reply ends it synchronously awaits
    #    `long_term_memory.record(...)`, hanging the extraction LLM call on the
    #    reply_task wrap-up chain and further delaying the SSE meta event.
    #
    # All memory operations now go through the manual path:
    # - Retrieval: the `routing/workflow.py` entry point has a budget timeout;
    #   Profile reads the DB directly, Fact vector retrieval has a budget
    #   (default 600ms) and is skipped on timeout.
    # - Saving: after SSE close, the bounded background pipeline
    #   `schedule_post_response_tasks()` — never blocks the main conversation.
    #
    # The `memory_enabled` parameter is kept only for logging and downstream switches.
    if memory_enabled and current_user_id:
        _log.info("[factory] +%s memory=on (manual non-blocking pipeline)", _elapsed())

    # Skills are now registered via toolkit.register_agent_skill() above.
    # AgentScope's ReActAgent.sys_prompt automatically appends
    # toolkit.get_agent_skill_prompt(), so no separate hook is needed.

    # ── Resolve agent name and max_iters ──
    _DEFAULT_MAIN_ITERS = 50
    _DEFAULT_SUBAGENT_ITERS = 10
    _agent_name = "hugagent_agent"
    _max_iters = _DEFAULT_MAIN_ITERS
    if max_iters is not None:
        _max_iters = max_iters
    elif user_agent is not None:
        _agent_name = (
            f"subagent_{user_agent.agent_id}" if isolated else f"agent_{user_agent.agent_id}"
        )
        _max_iters = user_agent.max_iters or (
            _DEFAULT_SUBAGENT_ITERS if isolated else _DEFAULT_MAIN_ITERS
        )
    elif isolated:
        _max_iters = _DEFAULT_SUBAGENT_ITERS

    # ── Create the Agent (AgentScope 2.0) ──
    # Note: long_term_memory is not passed — mem0 is fully stripped from the SSE
    # main path (manual non-blocking pipeline).
    # hooks → middlewares (onion model, first in the list is outermost);
    # _jx_context → AgentRuntimeState. Only fields known to this function are
    # filled; per-request fields (chat_mode / user_message_text /
    # uploaded_files / historical_files) are set on agent.state by the caller
    # (streaming/workflow) after creation (replacing 1.x's
    # agent._jx_context = ModelContext(...)).
    # Permissions: MCP tools and built-in tools default to
    # check_permissions=ASK (docs risk #8), which triggers the native
    # RequireUserConfirmEvent and pauses execution. Early on we bypassed
    # everything with PermissionMode.BYPASS, but BYPASS also skips the built-in
    # tools' bypass-immune danger checks (step 3) — a sledgehammer. We switched
    # to 2.0's native allow_rules (PermissionEngine order: deny→ask→
    # tool-specific safety checks→**allow_rules**→BYPASS→default-ask): seed one
    # ALLOW rule per tool we register (empty rule_content matches any input),
    # letting our in-house/MCP tools through while keeping the built-in tools'
    # own safety checks. allow_rules are seeded after toolkit construction once
    # all tool names are known (see below).
    from agentscope.permission import PermissionContext

    _state = AgentRuntimeState(
        # The effective model name is read directly off the model object (the AS2 attribute is .model), same source as the compression window
        model_name=getattr(default_model, "model", None) or model_name or "",
        model_pinned=_subagent_model_pinned,
        user_id=current_user_id,
        chat_id=chat_id,
        permission_context=PermissionContext(),
    )

    _middlewares: list = [
        DynamicModelMiddleware(),  # on_reply: switch models by chat_mode
        FileContextMiddleware(),  # on_reply: inject file context
        WorkspacePinHintMiddleware(),  # on_reasoning: remind to pin
        IterBudgetReminderMiddleware(),  # on_reasoning: inject a wrap-up reminder near max_iters
        ActingToolCallIdMiddleware(),  # on_acting: expose call_subagent's tool_call.id to tools (parent-child linkage)
    ]
    if not batch_mode:
        _middlewares.append(GoalAnchorReminderMiddleware(chat_id=chat_id, batch_mode=False))
    _middlewares.append(FinishPinGuardMiddleware(batch_mode=batch_mode))

    # Collect all tool names (the collector still holds function_tools; tool_schemas covers Python+MCP)
    _all_tool_names = {ft.name for ft in toolkit.function_tools}
    _all_tool_names |= {
        s.get("function", {}).get("name") for s in tool_schemas if s.get("function", {}).get("name")
    }
    # At this point the collector has gathered all tools (including any subagent tools) → construct the final Toolkit.
    toolkit = _build_toolkit()

    # Allow all registered tools via native allow_rules (replacing BYPASS, see the explanation above).
    from agentscope.permission import PermissionRule, PermissionBehavior

    _state.permission_context.allow_rules = {
        n: [
            PermissionRule(
                tool_name=n, rule_content="", behavior=PermissionBehavior.ALLOW, source="jx_trusted"
            )
        ]
        for n in _all_tool_names
    }

    # Offloader: when compressing/truncating overlong tool results, spill the
    # overflow to the sandbox at /workspace/.offload/ (rather than silently
    # discarding it); the model can read it back on demand via Read/bash. Only
    # mounted when sandbox tools are enabled — otherwise the agent has no
    # Read/bash and spilling is pointless. Uses the same _sbx_sess as bash/Read.
    _offloader = None
    if not disable_tools and os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() == "true":
        try:
            from core.llm.offloader import SandboxOffloader
            from core.sandbox.factory import get_sandbox_provider

            _offloader = SandboxOffloader(get_sandbox_provider(), _sbx_sess)
        except Exception as exc:  # noqa: BLE001
            _log.warning("[factory] offloader 初始化跳过: %s", exc)

    agent = Agent(
        name=_agent_name,
        system_prompt=system_prompt,
        model=default_model,
        toolkit=toolkit,
        middlewares=_middlewares,
        state=_state,
        context_config=context_config,
        model_config=ModelConfig(max_retries=3, fallback_model=None),
        react_config=ReActConfig(max_iters=_max_iters),
        offloader=_offloader,
    )

    # Set the agent reference so the call_subagent closure can extract shared context
    if _agent_ref is not None:
        _agent_ref["agent"] = agent

    _log.info("[factory] +%s agent created, TOTAL setup done", _elapsed())

    # Only transient (per-request) stdio clients + HTTP clients get closed;
    # pooled stable clients stay open for reuse (closing them would defeat the
    # pool and hand dead clients to the next request).
    all_transient = [*transient_mcp_clients, *http_clients]
    return agent, all_transient
