"""Agent factory - creates AgentScope agents with pluggable configuration.

This module is separated from core.chat.agent to avoid circular dependencies:
- routing modules can import from this factory
- this factory can import orchestration.registry without creating cycles
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Dict, List, Optional, Set, Tuple

# AgentScope 2.0
from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.agent._config import ModelConfig  # not in agent's public exports
from agentscope.mcp import MCPClient
from agentscope.tool import Toolkit
from core.agent_skills.loader import get_skill_loader
from core.config.catalog import get_enabled_ids
from core.config.catalog_loader import DB_HIDDEN_SERVERS, DB_UMBRELLA_ID
from core.llm.agentscope_hook_adapter import AgentScopeHookAdapter
from core.llm.chat_models import get_default_model, make_chat_model
from core.llm.compacting_agent import CompactingAgent
from core.llm.compaction import SUMMARIZATION_PROMPT
from core.llm.context_manager import AUTO_COMPACT_MAX_RATIO
from core.llm.execution_manifest import PromptManifestBuilder, tool_manifest_from_schemas
from core.llm.manifest_agent import ManifestBoundAgent
from core.llm.mcp_manager import close_clients
from core.llm.mcp_pool import MCPConnectionPool
from core.llm.middlewares import (
    ActingToolCallIdMiddleware,
    AgentRuntimeState,
    CitationAnchorMiddleware,
    DynamicModelMiddleware,
    ExplicitConnectorToolChoiceMiddleware,
    ExplicitPluginToolChoiceMiddleware,
    ExplicitSkillToolChoiceMiddleware,
    FileContextMiddleware,
    FinishPinGuardMiddleware,
    IterBudgetReminderMiddleware,
    JobLedgerReminderMiddleware,
    OntologyGateMiddleware,
    PlanStaleReminderMiddleware,
    StallInterventionMiddleware,
    SteerMiddleware,
    ToolEffectMiddleware,
    WorkspacePinHintMiddleware,
)
from core.llm.providers.registry import get_spec, split_provider_extra
from core.llm.tool_collector import ToolCollector
from core.llm.tools import (
    ReadStateTracker,
    register_bash,
    register_channel_attachment,
    register_delete,
    register_edit,
    register_get_data_context,
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
    register_view_image,
    register_write,
)
from core.llm.tools._common import resolve_sandbox_session
from core.ontology.toolkit import OntologyFilteredToolkit
from core.ontology.validator import register_runtime_asset_tags, render_runtime_prompt
from core.services.mcp_service import McpServerConfigService
from dotenv import load_dotenv
from prompts.prompt_config import load_prompt_config
from prompts.prompt_runtime import build_subagent_system_prompt, build_system_prompt, select_tools

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


def cache_compaction_execution_surface(agent: Any, base_prompt: str, surface: Any) -> None:
    """Mirror the exact surface used by the latest model request for compaction."""
    skill_instructions = str(getattr(surface, "skill_instructions", "") or "")
    agent._jx_compaction_system_prompt = (
        f"{base_prompt}\n{skill_instructions}" if skill_instructions else base_prompt
    )
    agent._jx_compaction_tool_schemas = getattr(surface, "tool_schemas", None)


def _default_allow_builtin_tools(
    *,
    channel_origin: Optional[Dict[str, Any]],
    automation_run: bool,
) -> bool:
    """Whether this trusted unattended entry point bypasses built-in policy."""
    return bool((channel_origin or {}).get("channel_id") or automation_run)


# 工作流模式提示段 —— 只在用户显式进入工作流模式时拼进系统提示（与 _BATCH_MODE_HINT 同源做法）。
# 不进入这个模式时，下面这一整套批量规则**完全不存在**，普通问答不受任何干扰。
_WORKFLOW_MODE_HINT = (
    "\n\n## 工作流模式（用户已主动进入）\n"
    "用户显式进入了工作流模式，明确希望用**批量作业**的方式处理任务。你有 `run_job` 工具。\n"
    "\n"
    "### 什么时候必须用\n"
    "当任务是「对 N 个**同构**工作项做同一件事」——补全表格某列、逐份审阅文档、逐个文件改代码、"
    "逐条数据打标——且 N 较多（经验刻度 ~20 起，真正的判据是：对象彼此独立、处理逻辑同构、总量大）"
    "时，**禁止在对话主循环里逐项处理**。主循环每一轮都要重发全部累积历史，成本随进度二次方增长，"
    "必然做不完。\n"
    "\n"
    "### 怎么做（三步）\n"
    "1. 用 `write` 把作业脚本写进沙箱（例如 `/workspace/jobs/fill.py`）。脚本是普通 Python，"
    "开头 `from hugagent_job import ledger, agent, job, log`，SDK 由系统注入：\n"
    "   - `ledger.seed(items)` 建台账（每项给 key 与 payload 两个字段），按 key 幂等；\n"
    "   - `job.map(items, fn, concurrency=8)` 并发跑，**fn 返回 dict 会自动逐项落账**"
    "（返回 None 表示你自己 update 过了；抛异常自动记 failed）；\n"
    "   - `agent(prompt, schema=..., tools=[...], item_key=...)` 派子智能体做每项的模型判断；\n"
    "   - `ledger.pending()` / `ledger.stats()` / `job.budget()` / `log(...)`。\n"
    "\n"
    "   **照这个骨架写**——最常踩的坑是 seed 和 map 用了两份不同的列表：\n"
    "   ```python\n"
    "   items = [                                   # 一份列表，seed 和 map 都用它\n"
    '       {"key": f"row_{r[\'idx\']}", "payload": {"标题": r["标题"]}}\n'
    "       for r in records\n"
    "   ]\n"
    "   ledger.seed(items)\n"
    "\n"
    "   def handle(item):           # item 就是 items 里的元素，原样传入\n"
    '       p = item["payload"]     # 只有传 items 时才有 payload\n'
    '       return {"分类": agent(PROMPT.format(**p), schema=SCHEMA,\n'
    '                             item_key=item["key"])["分类"]}\n'
    "\n"
    "   job.map(items, handle, concurrency=8)       # 传 items，不是原始 records\n"
    "   ```\n"
    "   `job.map(items, fn)` 把 `items` 的元素**原样**交给 `fn`——fn 收到的是你传进去的"
    "那个对象本身，不是台账记录。直接 map 原始业务列表也行，但那时 item 里没有 `payload`，"
    "且必须让它带得出台账主键（`key`/`item_key`/`id`/`seq` 之一，"
    '或 `job.map(..., key="idx")` 显式指定），否则结果无法回写、台账全程 pending。\n'
    "   ⚠️ `job.map` 会先试跑头两项：若都抛同一类脚本异常（KeyError/NameError/TypeError…），"
    "判定是脚本写错而非数据个例，**整份作业立刻中止报错**——你会拿到 traceback，改脚本后"
    '用 `run_job(action="start", on_conflict="replace")` 重跑即可。\n'
    "   其余一切用标准 Python：抓网页、解析、写 Excel、`subprocess` 跑校验命令。\n"
    "   **能机检的验收就别烧模型**——`mypy` / `pytest` / 一段校验函数都比 `agent()` 便宜得多。\n"
    "   ⚠️ `agent()` 在工具全挂/配额打爆/超时时会**抛异常**，不要 try/except 把它转写成"
    "「未查询到」——那是把环境故障固化成数据。让异常抛出去，该项会记 failed 留在台账等续跑。\n"
    "   同理：真的查无请用 `_status` 标 `not_found`，**不要**把占位串写进结果字段。\n"
    '2. `run_job(action="start", script_path=..., name=...)` 提交。\n'
    '3. 作业结束后 `run_job(action="export", job_id=...)` 把台账导成沙箱里的 JSONL，'
    "再用 bash/python 读它写产物。\n"
    "\n"
    "### 两条硬规矩\n"
    "- **默认后台跑，别在前台干等**：`wait` 默认 `false`（后台跑），"
    "**这就是绝大多数作业该用的值**。\n"
    "  - **只有确信一分钟内一定能收**（十来项、纯脚本处理、没有逐项 `agent()` 调用）"
    "才用 `wait=true` 原地等，省掉唤醒那一圈。\n"
    "  - **只要每项都要过模型**（哪怕只有几十项），或者要抓网页、要跑长命令，一律用默认的 "
    "`wait=false` 丢后台，然后**马上把本轮回复收掉**——告诉用户作业已在后台开始、"
    "输入框上方的状态条会实时显示进度、期间可以继续聊别的也可以随时取消。\n"
    "  - **估不准也不用怕**：前台等待有 **90 秒硬上限**，超时系统会自动把作业转入后台"
    "（作业不中断），并让你按后台语义收尾。但那等于白白让用户对着转圈等了 90 秒，"
    "别拿这个兜底当默认策略。\n"
    "  后台作业每隔一段时间会**主动叫醒你播报进度**（默认 5 分钟，`progress_wake_sec` 可调），"
    "跑完还会再叫你一次做交付，不需要你守着——**任何情况下都别反复调 `status` 轮询干等**，"
    "那是纯粹浪费推理轮次。用户那边有独立的作业状态条实时显示进度，"
    "不需要你复述作业还活着。\n"
    "- **被进度唤醒时只汇报、不干活**：那一轮只需一两句话转述进度，"
    "**不要**重复提交作业（它还在跑）、不要 `export`、不要把逐项结果读进对话。"
    '确实要换脚本重跑，用 `run_job(action="start", on_conflict="replace")`——'
    "它会先停掉旧作业；默认的拦截只是不让你**意外**叠加两份，不是不让重跑。\n"
    "- **用户说停就立刻停**：用户说「停止任务/别跑了/取消」时，"
    '马上 `run_job(action="cancel", job_id=...)` 把作业停掉再回话，'
    "**不要**先解释、不要反问要不要保留进度——台账已经落库，随时可以 `resume` 续跑。"
    '不知道 job_id 就先 `run_job(action="status")` 查，别让作业在用户喊停后还在烧预算。\n'
    "- **逐项结果不进对话**：要用结果就 `export` 成文件再脚本处理。\n"
    "\n"
    "### 交付纪律\n"
    "台账里还剩多少是**可查证的事实**。不得以「边际收益递减」「消耗较大」为由在只完成一部分时"
    "转向交付；确实未做完，必须报出分母、已完成数与未覆盖清单，并给出续跑方式"
    '（`run_job(action="resume", job_id=...)`，已完成的项不会重做）。\n'
    "「查无 / 待定 / 失败」只能落在独立的状态字段，**不得**把占位串写进原始数据位——"
    "一旦写入，「哪些还没做」就不再可判定。\n"
)

from orchestration.registry import AgentSpec

load_dotenv()

# Per-server failure cooldown for HTTP MCP connects. When a server fails
# (upstream 503, transient SSE drop, etc.), skip it for COOLDOWN seconds
# before retrying. Avoids per-request log spam and the noisy anyio
# cancel-scope warnings that come with each failed cleanup.
_HTTP_MCP_FAIL_AT: Dict[str, float] = {}
_HTTP_MCP_FAIL_COOLDOWN_S = 60.0


def _render_turn_budget_hint(max_iters: int) -> str:
    """Tell an agent that *does* have a turn budget how to spend it.

    Stating the budget up front is cheaper than shouting about it at the end:
    the wrap-up reminder (:class:`IterBudgetReminderMiddleware`) can only ask a
    loop that already burned its rounds one tool at a time to salvage
    something, while this changes how the rounds get spent in the first place.
    Parallel fan-out within one round is the lever — a round is one reasoning
    step, not one tool call, so the budget binds serial round-trips, never the
    total number of tool calls.

    Only rendered where a bound actually exists (sub-agents, turbo, custom
    agents, an explicit operator cap). The main agent is unbounded and must not
    be told otherwise — a false scarcity claim would make it cut work short.
    """
    return (
        "\n\n## 轮次预算\n"
        f"本次运行最多 {max_iters} 轮「推理 → 工具调用」。一轮里可以同时发起任意多个"
        "工具调用，所以受限的是串行往返次数，不是工具调用总数。\n"
        "- 先想清楚需要哪些信息，再**在同一轮里并行发起**全部彼此独立的调用"
        "（一次读多个文件、一次检索多个关键词），不要一轮只调一个。\n"
        "- 只有后一步的参数确实要等前一步的结果时，才分成下一轮。\n"
        "- 同一个操作连续失败两次就换思路或如实报告，不要用剩余轮次反复重试。\n"
    )


def _effective_mcp_server_keys(
    cfg,
    agent_spec: Optional[AgentSpec],
    enabled_mcp_ids: Optional[list[str]] = None,
    enabled_kb_ids: Optional[list[str]] = None,
    owned_servers: Optional[dict] = None,
    bridge_servers: Optional[dict] = None,
) -> list[str]:
    all_servers = dict(McpServerConfigService.get_instance().get_all_servers(enabled_only=True))
    # Config-source precedence (later update wins on same server_id):
    #   global rows < bridge (desktop cloud gateway; cloud is the source of
    #   truth for a capability it takes over) < owned (a user's own private
    #   MCP is the narrowest, most explicit grant).
    if bridge_servers:
        all_servers.update(bridge_servers)
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
    # KB list was empty because an external provider was unreachable). We do NOT remove the tool
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
    enabled_keys: list[str],
    owned_servers: Optional[dict] = None,
    bridge_servers: Optional[dict] = None,
) -> dict:
    enabled_set = set(enabled_keys)
    all_servers = dict(McpServerConfigService.get_instance().get_all_servers(enabled_only=True))
    # Same precedence as _effective_mcp_server_keys: global < bridge < owned.
    if bridge_servers:
        all_servers.update(bridge_servers)
    if owned_servers:
        all_servers.update(owned_servers)
    return {k: v for k, v in all_servers.items() if k in enabled_set}


def _required_mcp_server_keys(
    required_mcp_ids: List[str], enabled_mcp_keys: List[str]
) -> List[str]:
    """Resolve user-facing connector IDs to the concrete connected servers."""
    enabled = set(enabled_mcp_keys)
    resolved: List[str] = []
    for connector_id in required_mcp_ids:
        if connector_id == DB_UMBRELLA_ID:
            candidates = [key for key in enabled_mcp_keys if key in DB_HIDDEN_SERVERS]
        else:
            candidates = [connector_id] if connector_id in enabled else []
        for key in candidates:
            if key not in resolved:
                resolved.append(key)
    return resolved


def _filter_skill_ids_for_user(skill_ids: list[str], user_id: Optional[str]) -> list[str]:
    """Strip out skill ids this user must not load.

    Two independent rules, applied at the one choke point every agent (main,
    sub-agent, batch) passes through:

    1. **Ownership.** Kept: public skills (``owner_user_id`` empty, including
       filesystem/built-in skills absent from ``admin_skills``) + the user's own
       private skills. Dropped: other users' private skills.
    2. **Release exposure.** An evolution-authored skill is only loadable once
       its release actually reaches this user — ``active``, or ``canary`` with the
       user in the bucket. A ``shadow`` release reaches nobody. Without this the
       release ladder was decorative: a materialised skill was a global public
       row, so a ``risk_tier=high`` capability the system wrote about itself went
       to every user at once.
    """
    if not skill_ids:
        return skill_ids
    try:
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill
        from core.evolution.exposure import filter_skill_ids as filter_evolved

        with SessionLocal() as db:
            owned = dict(
                db.query(AdminSkill.skill_id, AdminSkill.owner_user_id)
                .filter(
                    AdminSkill.skill_id.in_(skill_ids),
                    AdminSkill.owner_user_id.isnot(None),
                )
                .all()
            )
            allowed = [sid for sid in skill_ids if owned.get(sid) in (None, user_id)]
            return filter_evolved(allowed, user_id=user_id or "", db=db)
    except Exception:
        # Ownership filtering degrades to "keep what was asked for", as before.
        # Exposure does not: an evolved skill whose release state we could not
        # read is withheld, because the unsafe direction here is loading an
        # unvetted self-authored capability.
        try:
            from core.evolution.exposure import is_evolved_skill_id

            return [sid for sid in skill_ids if not is_evolved_skill_id(sid)]
        except Exception:
            return skill_ids


def _filter_kb_ids_for_user(kb_ids: list[str], user_id: Optional[str]) -> list[str]:
    """Strip out KB ids the current user has no access to (local + external collections), preventing unauthorized ids passed in from the frontend.

    Single source of truth ``core.auth.kb_permissions``: public KBs are visible
    to everyone, private KBs to their owner, and scoped-visibility KBs per
    grant. On failure, fall back to returning the input unchanged (this doesn't
    escalate permissions — it only avoids hurting availability, and the
    downstream retrieve's authorization intercepts again).
    """
    if not kb_ids or not user_id:
        return kb_ids
    try:
        from core.auth.kb_permissions import filter_accessible_kb_ids
        from core.db.engine import SessionLocal

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
KB_MCP_HTTP_URL = (
    f"http://{_settings.server.mcp_host}:{_MCP_PORTS['retrieve_dataset_content']}/mcp/"
)


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

    from core.kb.external_provider import runtime_request_context

    normalized = [str(x).strip() for x in (enabled_kb_ids or []) if str(x).strip()]
    local_ids = [x for x in normalized if x.startswith("kb_")]
    external_headers, external_env = runtime_request_context(normalized)
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
        "X-Allowed-Kb-Ids": ",".join(local_ids),
        "X-Reranker-Enabled": "true" if reranker_enabled else "false",
    }
    ctx_headers.update(external_headers)
    ctx_env = {
        "CURRENT_USER_ID": current_user_id or "",
        "CURRENT_CHAT_ID": chat_id or "",
        "LOCAL_KB_ALLOWED_IDS": ",".join(local_ids),
        "RERANKER_ENABLED": "true" if reranker_enabled else "false",
    }
    ctx_env.update(external_env)

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


def _vision_bridge_needed() -> bool:
    """Whether to hand the agent a ``view_image`` tool.

    Only when a vision model is reachable *and* the main model can't see images
    itself — a natively multimodal model receives the picture inline, so proxying
    it through a second model would only lose fidelity.
    """
    try:
        from core.services.model_config import ModelConfigService
        from core.vision import is_available, model_supports_vision

        if model_supports_vision(ModelConfigService.get_instance().resolve("main_agent")):
            return False
        return is_available()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[factory] vision availability probe failed: %s", exc)
        return False


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


def _mcp_ids_bound_to_skills(skill_ids: list[str]) -> list[str]:
    """Collect explicit MCP bindings declared by enabled skills."""
    if not skill_ids:
        return []
    try:
        metadata = get_skill_loader().load_all_metadata()
    except Exception:
        return []
    result: list[str] = []
    for skill_id in skill_ids:
        item = metadata.get(skill_id)
        if item is None:
            continue
        for server_id in item.mcp_server_ids or []:
            if server_id and server_id not in result:
                result.append(server_id)
    return result


# The one place evolved prompt content may live, and it is a *named block* at
# the end of the system prompt rather than an edit anywhere inside it.
#
# Three things follow from making it a block instead of a patch:
#   · the hand-written prompt is never touched, so a generated line can never
#     weaken or delete a clause somebody wrote deliberately;
#   · removing evolved content is deleting one section, whatever the base prompt
#     has done in the meantime;
#   · an operator reading the assembled prompt can see exactly which part the
#     system wrote about itself, which an inline patch makes impossible.
DYNAMIC_BLOCK_HEADER = "## 动态补充（由能力进化沉淀，可在进化控制台停用）"


def _render_dynamic_block(fragments: list[str]) -> str:
    """Render evolved prompt fragments as one clearly-attributed section."""
    lines = [DYNAMIC_BLOCK_HEADER, ""]
    lines.extend(fragment.strip() for fragment in fragments if fragment.strip())
    return "\n\n".join(lines)


def _resolve_prompt_fragments(fragment_ids: list[str]) -> list[str]:
    """The text of each fragment a profile references.

    Fragments are stored as prompt candidates and referenced by id, not copied
    into the profile. Copying would mean a fragment corrected in one place stays
    wrong everywhere it was pasted, and there would be no single row to roll
    back. A reference that no longer resolves is skipped rather than rendered as
    an empty line, so a retired fragment leaves no trace in the prompt.
    """
    if not fragment_ids:
        return []
    try:
        from core.db.engine import SessionLocal
        from core.db.models.evolution import EvolutionCandidate

        with SessionLocal() as db:
            rows = (
                db.query(EvolutionCandidate)
                .filter(
                    EvolutionCandidate.target_kind == "prompt",
                    EvolutionCandidate.target_asset_id.in_(list(fragment_ids)),
                    EvolutionCandidate.status == "active",
                )
                .all()
            )
        by_id = {
            str(row.target_asset_id): str(
                ((row.ir or {}).get("changes") or [{}])[0].get("fragment") or ""
            )
            for row in rows
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[factory] prompt fragments unavailable: %s", exc)
        return []
    return [by_id[fid] for fid in fragment_ids if by_id.get(fid)]


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
    # model_role: 指定按「模型管理 → 角色分配」的哪个角色解析默认模型（如
    # "loop_reviewer"）。优先级低于用户显式选择的 model_provider_id/model_name，
    # 高于 main_agent 兜底；plan_mode=True 等价于 model_role="plan_agent"。
    model_role: Optional[str] = None,
    batch_mode: bool = False,
    # workflow_mode: 工作流模式（用户显式触发：斜杠命令 /workflow 或 + 菜单选「工作流模式」）。
    # 只有它为 True 才注册 run_job 并注入作业脚本写法——与计划模式/批量执行同属"用户触发的
    # 模式"，不触发就完全不存在，普通问答不会被无关的批量规则干扰。
    workflow_mode: bool = False,
    # top_level_chat: whether this construction is a "top-level interactive main
    # conversation capable of hosting plan mode" — astream_chat_workflow passes
    # True explicitly after determining (has chat_id, not
    # channel/automation/batch/plan_chat). The update_plan tool is
    # registered ONLY on this positive signal. All derived/non-interactive paths
    # (plan generation, plan-execute steps, subagents, batch, autonomous loop,
    # channels, non-streaming…) default to False → they naturally never get the
    # tool, eliminating "plan within plan" nesting and all kinds of context
    # leaks at the root.
    top_level_chat: bool = False,
    chat_id: Optional[str] = None,
    # run_id lets the Runtime Binder (GCE ticket 03) key this run's frozen asset
    # bundle, so evidence assembled after the response can look up exactly which
    # versions were in play. Optional — non-chat paths simply bind anonymously.
    run_id: Optional[str] = None,
    journal_owner: Optional[str] = None,
    # Workspace scope is part of the frozen memory-policy ref and execution
    # context hash. Keep the default for non-chat/internal callers.
    workspace_id: str = "default",
    sandbox_session_id: Optional[str] = None,
    project_ctx: Optional[Dict[str, Any]] = None,
    channel_origin: Optional[Dict[str, Any]] = None,
    automation_run: bool = False,
    # read_only: read-only agent (for reviewers/auditors) — registers no
    # file-mutating tools (edit/write/delete/move/mkdir/myspace writes/
    # put_artifact), keeping only read/glob/grep/view/get_artifact. Callers may
    # independently disable bash for a hard read-only boundary.
    read_only: bool = False,
    allow_bash: bool = True,
    # approval_mode: 用户自选的权限档（core.llm.tool_permissions 的 ask / auto /
    # full）。auto 让普通写入不再弹确认框、删除等危险操作照旧问；full 一律不问。
    # 留空表示"照用户自己存的那一档"——子智能体、计划步骤这些新起 agent 的入口
    # 不必逐个透传，也就不会漏掉一个就悄悄退回逐项确认。
    approval_mode: Optional[str] = None,
    # turbo_mode: 极速模式（quick-lookup entry）。Retrieval-only assembly:
    # carries exactly the admin-configured turbo MCP set（系统配置「极速模式」，
    # deliberately independent of catalog enable/disable state）, drops skills,
    # sandbox/file tools, subagents and plan tooling, swaps in the standalone
    # "turbo" system prompt, and hard-caps react iterations so the agent
    # answers within 1-2 (parallel) tool rounds. The two turbo_explicit_*
    # params are the only pass-throughs: capabilities the user explicitly
    # summoned this turn (slash skill / plugin expansion); the caller likewise
    # narrows visible_subagents to the @-mentioned agent only.
    turbo_mode: bool = False,
    turbo_explicit_skill_ids: Optional[List[str]] = None,
    turbo_explicit_mcp_ids: Optional[List[str]] = None,
    # invoked_skill_ids / invoked_mcp_ids: capabilities the user explicitly
    # summoned THIS turn（斜杠技能 / 插件 chip 展开的 skill_ids/mcp_ids），mode
    # 无关。Consumed by progressive plugin loading: an explicitly invoked
    # plugin must not be deferred (its user-message injection promises the
    # capability is active), and the invocation is persisted as a sticky
    # activation for the chat. turbo_explicit_* remain the turbo-only narrowing
    # pass-throughs.
    invoked_skill_ids: Optional[List[str]] = None,
    invoked_mcp_ids: Optional[List[str]] = None,
    # required_mcp_ids: connectors explicitly selected in the composer. Unlike
    # plugin MCP activation (available on demand), these IDs carry a fail-closed
    # contract: the first model round must execute one of their real tools.
    required_mcp_ids: Optional[List[str]] = None,
    # required_skill_*: the exact skill selected through the slash picker. It
    # must be loaded through view_text_file before the model can finish.
    required_skill_id: Optional[str] = None,
    required_skill_name: Optional[str] = None,
    # required_plugin_*: authoritative components of the plugin explicitly
    # selected this turn. Unlike ordinary activation, this is a real-use
    # contract: the model must read one of the plugin's SKILL.md files or call
    # one of its MCP tools before it may finish the answer.
    required_plugin_id: Optional[str] = None,
    required_plugin_name: Optional[str] = None,
    required_plugin_skill_ids: Optional[List[str]] = None,
    required_plugin_mcp_ids: Optional[List[str]] = None,
    # mode_spec: 对话模式的装配契约（core/services/chat_mode_service.ChatModeSpec）。
    # 「模式」把原来写死的极速模式泛化成一张表：工具面 / 技能 / 插件 / 提示词 kind /
    # 迭代上限都由它给。turbo_mode 现在的含义是"这个模式要收窄工具面"，收窄成什么
    # 由 mode_spec 说了算。缺省（老调用方没传）时退回历史的 turbo.* 配置键读取，
    # 保证升级期间不 regress。
    mode_spec: Optional[Any] = None,
    ontology_runtime: Optional[Dict[str, Any]] = None,
    # Per-tool-result context cap override (tokens). The 20k default suits
    # exploratory chat, but document-heavy workloads (autonomous-loop workers
    # polishing a 180k-char report) re-send every accumulated tool result on
    # each ReAct round — with 15-20 rounds/iteration that grows quadratically
    # to ~1M tokens per iteration. Loop callers pass a tighter cap; the
    # offloader keeps full content readable on demand from /workspace/.offload.
    tool_result_limit: Optional[int] = None,
) -> Tuple[Agent, List[MCPClient]]:
    """Create and return an AgentScope 2.0 Agent along with its MCP client list.

    Returns:
        Tuple of (agent, mcp_clients). Caller is responsible for closing
        mcp_clients after use via close_clients().
    """
    from core.llm.middlewares import CURRENT_RUN_BINDING

    inherited_run_binding = CURRENT_RUN_BINDING.get()
    if inherited_run_binding is not None:
        run_id = run_id or inherited_run_binding[0]
        journal_owner = journal_owner or inherited_run_binding[1]

    import logging
    import time

    from core.llm.tool_permissions import resolve_approval_mode

    approval_mode = resolve_approval_mode(approval_mode, user_id=current_user_id)

    _log = logging.getLogger(__name__)
    _t0 = time.monotonic()
    _required_connector_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in (required_mcp_ids or [])
            if isinstance(item, str) and item.strip()
        )
    )
    _required_skill_id = str(required_skill_id or "").strip()
    _required_skill_name = str(required_skill_name or _required_skill_id or "技能").strip()
    _required_plugin_id = str(required_plugin_id or "").strip()
    _required_plugin_name = str(required_plugin_name or _required_plugin_id or "插件").strip()
    _required_plugin_skill_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in (required_plugin_skill_ids or [])
            if isinstance(item, str) and item.strip()
        )
    )
    _required_plugin_mcp_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in (required_plugin_mcp_ids or [])
            if isinstance(item, str) and item.strip()
        )
    )
    if _required_plugin_id and not (_required_plugin_skill_ids or _required_plugin_mcp_ids):
        raise RuntimeError(
            f"显式调用的插件「{_required_plugin_name}」当前没有可执行能力；本轮已停止。"
        )
    _sticky_plugin_skill_ids: List[str] = []
    _sticky_plugin_mcp_ids: List[str] = []
    _sticky_direct_skill_ids: List[str] = []
    _sticky_direct_mcp_ids: List[str] = []

    def _elapsed():
        return f"{(time.monotonic() - _t0) * 1000:.0f}ms"

    import asyncio

    cfg = load_prompt_config()
    _log.info("[factory] +%s config loaded", _elapsed())
    if agent_spec is not None and agent_spec.prompt_parts:
        cfg = replace(
            cfg,
            system_prompt=replace(cfg.system_prompt, parts=list(agent_spec.prompt_parts)),
        )

    # ── Sub-agent overrides ──────────────────────────────────────────
    # _subagent_progressive: the sub-agent's bound plugins, progressively
    # loaded — deferred here, directory + load_plugin injected in the subagent
    # prompt branch below. Activation is in-run only (no sticky persistence:
    # sub-agent runs are short-lived and isolated, and writing under the parent
    # chat's key would leak the activation into the main agent's assembly).
    _subagent_progressive = None
    if user_agent is not None:
        # Override capability bindings from user_agent config
        enabled_mcp_ids = list(user_agent.mcp_server_ids or [])
        enabled_skill_ids = list(user_agent.skill_ids or [])
        enabled_kb_ids = user_agent.kb_ids or []
        # Expand bound plugins into their component skills + MCPs (a plugin = a detachable capability bundle). Merge with the loose bindings, deduplicated.
        plugin_ids = user_agent.plugin_ids or []
        if plugin_ids:
            from core.llm import plugin_loader as _plug_sub

            if not disable_tools and _plug_sub.progressive_plugin_loading_enabled():
                try:

                    def _resolve_bound():
                        # Same ownership/release narrowing the eager expansion
                        # would have received via _filter_skill_ids_for_user.
                        return _plug_sub.resolve_bound_progressive_plugins(
                            list(plugin_ids),
                            skill_filter=lambda sids: _filter_skill_ids_for_user(
                                sids, current_user_id
                            ),
                        )

                    _subagent_progressive = await asyncio.to_thread(_resolve_bound)
                    if not _subagent_progressive.directory:
                        _subagent_progressive = None
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "[factory] subagent progressive plugin resolve failed（回退全量装配）: %s",
                        exc,
                    )
                    _subagent_progressive = None
            p_skills, p_mcp = _expand_plugin_bindings(plugin_ids)
            if _subagent_progressive is not None:
                # Deferred components stay out of the assembly; stdio-transport
                # plugins (absent from deferred_*) still expand eagerly.
                p_skills = [
                    s for s in p_skills if s not in _subagent_progressive.deferred_skill_ids
                ]
                p_mcp = [m for m in p_mcp if m not in _subagent_progressive.deferred_mcp_ids]
            enabled_skill_ids = list(dict.fromkeys(enabled_skill_ids + p_skills))
            enabled_mcp_ids = list(dict.fromkeys(enabled_mcp_ids + p_mcp))

    # ── Turbo mode (极速模式): retrieval-only assembly ──────────────────────
    # Normalized before any capability resolution so every downstream gate
    # (skill-bound MCP expansion, subagent merge, update_plan opt-in, file
    # tools) sees the narrowed grants rather than needing its own turbo check.
    #
    # _turbo_code_exec: 收窄模式保留代码执行（chat_modes.code_exec_enabled）。
    # 收窄的是 MCP/技能/插件面，沙箱与文件工具照常注册——"收窄=无代码"只是
    # 内置极速模式的契约，不是所有收窄模式的。
    _turbo_code_exec = False
    _mode_manual_invoke = True
    if turbo_mode:
        if mode_spec is not None:
            # 模式表是真源：这几个取值全部来自 chat_modes 那一行。
            _mode_manual_invoke = bool(getattr(mode_spec, "manual_invoke_enabled", True))
            _mode_mcp_ids = list(getattr(mode_spec, "mcp_server_ids", ()) or ())
            _mode_skill_ids = list(getattr(mode_spec, "skill_ids", ()) or ())
            _mode_plugin_ids = list(getattr(mode_spec, "plugin_ids", ()) or ())
            _turbo_code_exec = bool(getattr(mode_spec, "code_exec_enabled", False))
        else:
            from core.services.system_config import (
                turbo_manual_invoke_enabled,
                turbo_mcp_server_ids,
                turbo_plugin_ids,
                turbo_skill_ids,
            )

            _mode_manual_invoke = turbo_manual_invoke_enabled()
            _mode_mcp_ids = sorted(turbo_mcp_server_ids())
            _mode_skill_ids = list(turbo_skill_ids())
            _mode_plugin_ids = list(turbo_plugin_ids())

        if not _mode_manual_invoke:
            # Manual summoning disabled by ops: turbo is strictly its own set.
            if _required_connector_ids or _required_plugin_id or _required_skill_id:
                raise RuntimeError(
                    "当前对话模式禁止手动调用技能、连接器或插件；"
                    "本轮已停止，未静默忽略用户选择。"
                )
            turbo_explicit_skill_ids = None
            turbo_explicit_mcp_ids = None
            visible_subagents = None
        # Admin-configured turbo plugins, expanded into their component skills +
        # MCPs. A plugin is the installable/removable unit — some capabilities
        # (e.g. a crawler, a ticket system) ship only as a plugin and have no
        # loose MCP row to pick, so without this they were unreachable in turbo.
        turbo_plugin_skill_ids, turbo_plugin_mcp_ids = _expand_plugin_bindings(
            list(_mode_plugin_ids)
        )
        # Skills in turbo = admin-configured set (turbo.skill_ids + those bundled
        # with a configured plugin) + the ones explicitly summoned this turn.
        # With none of the three the agent carries no skills at all (and no skill
        # list enters the prompt) — the original quick-lookup contract.
        enabled_skill_ids = list(
            dict.fromkeys(
                [
                    *_mode_skill_ids,
                    *[s for s in turbo_plugin_skill_ids if isinstance(s, str) and s.strip()],
                    *[
                        s
                        for s in (turbo_explicit_skill_ids or [])
                        if isinstance(s, str) and s.strip()
                    ],
                ]
            )
        )
        top_level_chat = False
        if not _turbo_code_exec:
            read_only = True
            allow_bash = False
        # The turbo tool surface comes from admin config alone — deliberately
        # NOT intersected with catalog/user enable-disable state. Explicitly
        # summoned plugin MCPs are appended on top; MCPs bound to a summoned
        # skill merge in the shared binding step below.
        enabled_mcp_ids = list(
            dict.fromkeys(
                [
                    *_mode_mcp_ids,
                    *[m for m in turbo_plugin_mcp_ids if isinstance(m, str) and m.strip()],
                    *[
                        m
                        for m in (turbo_explicit_mcp_ids or [])
                        if isinstance(m, str) and m.strip()
                    ],
                ]
            )
        )

    # A plugin explicitly selected for this turn is stronger than the default
    # catalog, a dedicated agent's saved bindings, and a restricted mode's
    # ordinary capability set. The API already enforced ownership/admin/deps;
    # merge only those authoritative components here so the execution guard
    # below has a real surface to require.
    if _required_plugin_id:
        base_skill_ids = (
            list(enabled_skill_ids)
            if isinstance(enabled_skill_ids, list)
            else _effective_main_available_skills()
        )
        enabled_skill_ids = list(dict.fromkeys([*base_skill_ids, *_required_plugin_skill_ids]))
        base_mcp_ids = (
            list(enabled_mcp_ids)
            if isinstance(enabled_mcp_ids, list)
            else [item for item in get_enabled_ids("mcp") if isinstance(item, str)]
        )
        enabled_mcp_ids = list(dict.fromkeys([*base_mcp_ids, *_required_plugin_mcp_ids]))

    if _required_skill_id:
        base_skill_ids = (
            list(enabled_skill_ids)
            if isinstance(enabled_skill_ids, list)
            else _effective_main_available_skills()
        )
        enabled_skill_ids = list(dict.fromkeys([*base_skill_ids, _required_skill_id]))

    # A direct connector selection is a stronger per-turn user instruction
    # than the normal catalog/profile assembly. Keep the ordinary defaults and
    # add the selected connector; ownership/enabled-server filtering below is
    # still the final security boundary.
    if _required_connector_ids:
        base_mcp_ids = (
            list(enabled_mcp_ids)
            if isinstance(enabled_mcp_ids, list)
            else [item for item in get_enabled_ids("mcp") if isinstance(item, str)]
        )
        enabled_mcp_ids = list(dict.fromkeys([*base_mcp_ids, *_required_connector_ids]))

    # Successful explicit activation is a chat-level capability grant, not a
    # one-request hint. Restore plugins, direct skills and direct connectors
    # before progressive deferral and profile narrowing so later turns keep
    # exactly the same surface even while personal catalog switches remain off.
    # Both resolvers recheck visibility, admin state and dependencies each turn.
    if (
        not disable_tools
        and current_user_id
        and chat_id
        and (not turbo_mode or _mode_manual_invoke)
    ):
        from core.llm import plugin_loader as _sticky_plugins
        from core.llm import session_capabilities as _sticky_direct

        try:
            _sticky, _direct = await asyncio.gather(
                asyncio.to_thread(
                    _sticky_plugins.resolve_sticky_plugin_capabilities,
                    user_id=str(current_user_id),
                    chat_id=chat_id,
                ),
                asyncio.to_thread(
                    _sticky_direct.resolve_session_activated_capabilities,
                    user_id=str(current_user_id),
                    chat_id=chat_id,
                ),
            )
            _sticky_plugin_skill_ids = list(_sticky.skill_ids)
            _sticky_plugin_mcp_ids = list(_sticky.mcp_ids)
            _sticky_direct_skill_ids = list(_direct.skill_ids)
            _sticky_direct_mcp_ids = list(_direct.mcp_ids)
            sticky_skill_ids = list(
                dict.fromkeys([*_sticky_plugin_skill_ids, *_sticky_direct_skill_ids])
            )
            sticky_mcp_ids = list(dict.fromkeys([*_sticky_plugin_mcp_ids, *_sticky_direct_mcp_ids]))
            if sticky_skill_ids:
                base_skill_ids = (
                    list(enabled_skill_ids)
                    if isinstance(enabled_skill_ids, list)
                    else _effective_main_available_skills()
                )
                enabled_skill_ids = list(dict.fromkeys([*base_skill_ids, *sticky_skill_ids]))
            if sticky_mcp_ids:
                base_mcp_ids = (
                    list(enabled_mcp_ids)
                    if isinstance(enabled_mcp_ids, list)
                    else [item for item in get_enabled_ids("mcp") if isinstance(item, str)]
                )
                enabled_mcp_ids = list(dict.fromkeys([*base_mcp_ids, *sticky_mcp_ids]))
            if _sticky.install_ids or sticky_skill_ids or sticky_mcp_ids:
                _log.info(
                    "[factory] sticky capabilities restored chat=%s installs=%s "
                    "skills=%d mcp=%d",
                    chat_id,
                    _sticky.install_ids,
                    len(sticky_skill_ids),
                    len(sticky_mcp_ids),
                )
        except Exception as exc:  # noqa: BLE001
            _log.warning("[factory] sticky capability restore failed: %s", exc)

    # Security: strip out other users' private skills, preventing unauthorized skill_ids passed in from the frontend
    if enabled_skill_ids:
        enabled_skill_ids = _filter_skill_ids_for_user(enabled_skill_ids, current_user_id)

    # ── Progressive plugin loading（渐进式插件加载）────────────────────────
    # Main-path only: a sub-agent's plugin binding is the owner's deliberate
    # configuration and a restricted mode's plugin set is the admin's deliberate
    # narrowing — both stay eager. Here, installed plugins' components are
    # removed from this run's enabled sets and replaced by a one-line directory
    # entry + a `load_plugin` activation tool (see core/llm/plugin_loader.py).
    # Deferral happens BEFORE the skill-bound-MCP merge below so a deferred
    # skill doesn't pull its bound MCP servers into the assembly either.
    _progressive = None
    if not disable_tools and not turbo_mode and user_agent is None and current_user_id:
        from core.llm import plugin_loader as _plug

        if _plug.progressive_plugin_loading_enabled():
            try:
                # Normalize the None fallbacks to their concrete resolutions
                # (identical sources to the later phases) so the subtraction
                # below has explicit lists to operate on.
                if enabled_skill_ids is None:
                    enabled_skill_ids = _filter_skill_ids_for_user(
                        _effective_main_available_skills(), current_user_id
                    )
                if not isinstance(enabled_mcp_ids, list):
                    enabled_mcp_ids = [
                        x for x in get_enabled_ids("mcp") if isinstance(x, str) and x.strip()
                    ]
                _progressive = await asyncio.to_thread(
                    _plug.resolve_progressive_plugins,
                    user_id=str(current_user_id),
                    chat_id=chat_id,
                    enabled_skill_ids=enabled_skill_ids,
                    enabled_mcp_ids=enabled_mcp_ids,
                    invoked_skill_ids=invoked_skill_ids,
                    invoked_mcp_ids=invoked_mcp_ids,
                )
                if _progressive.deferred_skill_ids:
                    enabled_skill_ids = [
                        s for s in enabled_skill_ids if s not in _progressive.deferred_skill_ids
                    ]
                if _progressive.deferred_mcp_ids:
                    enabled_mcp_ids = [
                        m for m in enabled_mcp_ids if m not in _progressive.deferred_mcp_ids
                    ]
                if not _progressive.directory:
                    _progressive = None
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "[factory] progressive plugin resolve failed（回退全量装配）: %s",
                    exc,
                )
                _progressive = None

    # A skill's MCP binding is an explicit capability grant, just like a sub-agent binding.
    # Merge only the MCPs declared by enabled skills; unrelated disabled MCPs remain disabled.
    skill_ids_for_bindings = (
        enabled_skill_ids if enabled_skill_ids is not None else _effective_main_available_skills()
    )
    skill_bound_mcp_ids = (
        _mcp_ids_bound_to_skills(skill_ids_for_bindings) if not disable_tools else []
    )
    if skill_bound_mcp_ids:
        base_mcp_ids = (
            enabled_mcp_ids
            if isinstance(enabled_mcp_ids, list)
            else [item for item in get_enabled_ids("mcp") if isinstance(item, str)]
        )
        enabled_mcp_ids = list(dict.fromkeys([*base_mcp_ids, *skill_bound_mcp_ids]))

    # Security: strip out KBs the current user has no access to (public-KB
    # permission assignment) — the frontend-supplied enabled_kb_ids may include
    # unauthorized scoped KBs; filter by the user's visible set here so the
    # agent only retrieves from authorized KBs.
    if enabled_kb_ids:
        enabled_kb_ids = _filter_kb_ids_for_user(enabled_kb_ids, current_user_id)

    # ── Orchestration profile (the assembly this task type calls for) ───────
    # Resolved per run, per subject. This is the only place the profile is read,
    # so everything it governs — which tools are visible, which skills are
    # offered, what the retrieval budget is — is decided once, here, from one
    # reviewed artefact rather than from constants scattered across the module.
    from core.auth.tenancy import tenant_of
    from core.evolution.agent_profile import builtin_profile, load_active_profile

    profile = builtin_profile()
    if user_agent is None:
        # A user-built sub-agent carries its own explicit bindings. Applying a
        # learned profile on top would override a person's deliberate
        # configuration with a statistical one, which is not a trade the person
        # agreed to.
        try:
            profile = await asyncio.to_thread(
                load_active_profile,
                task_type=str(chat_mode or "chat"),
                user_id=current_user_id or "",
                tenant_id=tenant_of(current_user_id),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("[factory] profile load failed, using built-in: %s", exc)

    if profile.skill_ids:
        # The profile decides what this task type gets offered at all. Anything
        # outside it is not a candidate, which is the mechanism that stops a
        # skill distilled from one family from being in view during another —
        # the leak that text annotations were measured to be unable to close.
        allowed = set(profile.skill_ids)
        skill_ids_for_bindings = [
            sid for sid in (skill_ids_for_bindings or []) if sid in allowed
        ] or list(profile.skill_ids)
        if enabled_skill_ids is not None:
            enabled_skill_ids = [sid for sid in enabled_skill_ids if sid in allowed]
        # A learned/default orchestration profile may narrow ambient skills,
        # but it must not erase chat-sticky or currently explicit capabilities.
        skill_ids_for_bindings = list(
            dict.fromkeys(
                [
                    *skill_ids_for_bindings,
                    *_sticky_plugin_skill_ids,
                    *_sticky_direct_skill_ids,
                    *_required_plugin_skill_ids,
                    *([_required_skill_id] if _required_skill_id else []),
                ]
            )
        )
        if enabled_skill_ids is not None:
            enabled_skill_ids = list(
                dict.fromkeys(
                    [
                        *enabled_skill_ids,
                        *_sticky_plugin_skill_ids,
                        *_sticky_direct_skill_ids,
                        *_required_plugin_skill_ids,
                        *([_required_skill_id] if _required_skill_id else []),
                    ]
                )
            )

    # ── Skill availability evidence (GCE ticket 10) ─────────────────────────
    # Which skills were available to the model this turn. Skill *loading* keeps
    # its own logic — every enabled skill's name and description goes into the
    # prompt and the model opens what it wants — so this records availability,
    # not a ranked choice, and ``degraded`` says so.
    #
    # Whether a skill was actually *used* is a separate observation
    # (``skill.opened``), recorded from the tool log. Keeping the two apart is
    # what the decremental engine needs; narrowing what gets loaded is not.
    skill_selection = None
    try:
        from core.agent_skills.selection_record import build_selection

        skill_selection = build_selection(
            all_candidate_ids=list(skill_ids_for_bindings or []),
            selected_ids=list(skill_ids_for_bindings or []),
            strategy="passthrough",
            degraded=True,
            degrade_reason="top_k_disabled",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[skill-select] record skipped: %s", exc)

    # The profile's sub-agent routes, applied. A route says "for this task type,
    # this work goes to a separate context"; anything the profile does not route
    # to stays out of view for this task type, which is the same narrowing
    # argument as the tool allowlist — an unroutable sub-agent in the prompt is
    # a delegation the model can attempt and should not.
    #
    # Evolution-authored agents ship disabled until their route is approved as a
    # prompt change, so this never surfaces one the reviewer has not seen.
    if profile.subagent_routes and visible_subagents:
        routed = {
            route.agent_id
            for route in profile.subagent_routes
            if not route.task_types or str(chat_mode or "chat") in route.task_types
        }
        visible_subagents = [
            agent for agent in visible_subagents if str(agent.get("agent_id") or "") in routed
        ]

    # The profile's tool allowlist, applied. It can only narrow: the candidate
    # that produced it was validated against the base grant, and intersecting
    # here means even a stored profile that has outlived a narrowed grant cannot
    # re-widen it.
    # Turbo's retrieval trio is a product contract, not a learned assembly —
    # a stored profile allowlist must not re-narrow it (it would silently drop
    # KB / web_fetch and leave quick lookup search-only).
    if profile.tool_allowlist is not None and not turbo_mode:
        allowed_tools = set(profile.tool_allowlist)
        current_mcp = (
            enabled_mcp_ids
            if isinstance(enabled_mcp_ids, list)
            else [item for item in get_enabled_ids("mcp") if isinstance(item, str)]
        )
        enabled_mcp_ids = [mcp_id for mcp_id in current_mcp if mcp_id in allowed_tools]
        # A user-selected connector must not disappear silently behind a learned
        # profile. It remains subject to the real server/ownership gate below.
        enabled_mcp_ids = list(
            dict.fromkeys(
                [
                    *enabled_mcp_ids,
                    *_sticky_plugin_mcp_ids,
                    *_sticky_direct_mcp_ids,
                    *_required_connector_ids,
                    *_required_plugin_mcp_ids,
                ]
            )
        )
        _log.info(
            "[factory] profile %s narrowed MCP servers %d → %d",
            profile.profile_id,
            len(current_mcp),
            len(enabled_mcp_ids),
        )

    # The execution manifest is assembled alongside the real prompt/tool
    # request. It hashes complete inputs but only persists hashes and public
    # references. Binding happens after the final prompt and tool schemas exist,
    # still before the Agent can execute anything.
    _manifest_builder = PromptManifestBuilder(
        context={
            "workspace_id": str(workspace_id or "default"),
            "project_id": str((project_ctx or {}).get("project_id") or ""),
            "project": dict(project_ctx or {}),
            "chat_mode": str(chat_mode or "default"),
            "model": {
                "name": str(model_name or ""),
                "provider_id": str(model_provider_id or ""),
            },
            "capabilities": {
                "skill_ids": list(skill_ids_for_bindings or []),
                "mcp_ids": list(enabled_mcp_ids or []),
                "kb_ids": list(enabled_kb_ids or []),
            },
            "orchestration_profile_id": str(profile.profile_id),
            "workflow_policy_version": str(profile.version),
            "prompt_fragment_ids": list(profile.prompt_fragments or []),
        }
    )
    asset_bundle = None

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
                enabled_only=user_agent is None and not skill_bound_mcp_ids,
            )
        except Exception:
            owned_mcp_servers = {}

    # 双端桌面本机后端：云端授权 MCP 以「指向云端能力网关的 HTTP MCP」形态
    # 作为独立配置源（bridge_servers）进入装配，最终仍受 enabled_mcp_ids
    # allowlist 收口。云端部署 / 纯本机模式下桥未激活，此处为空 dict。
    bridge_mcp_servers: dict = {}
    try:
        from core.services.desktop_cloud_bridge import cloud_gateway_mcp_configs

        bridge_mcp_servers = cloud_gateway_mcp_configs()
    except Exception:  # noqa: BLE001 - 桥故障不能影响会话装配
        bridge_mcp_servers = {}

    # Determine which MCP servers to connect
    enabled_mcp_keys = _effective_mcp_server_keys(
        cfg,
        agent_spec,
        enabled_mcp_ids=enabled_mcp_ids,
        enabled_kb_ids=enabled_kb_ids,
        owned_servers=owned_mcp_servers,
        bridge_servers=bridge_mcp_servers,
    )
    _required_connector_server_keys = _required_mcp_server_keys(
        _required_connector_ids,
        enabled_mcp_keys,
    )
    _required_plugin_server_keys = _required_mcp_server_keys(
        _required_plugin_mcp_ids,
        enabled_mcp_keys,
    )
    if _required_connector_ids and not _required_connector_server_keys:
        raise RuntimeError(
            "显式选择的连接器当前不可用或未获授权"
            f"（{', '.join(_required_connector_ids)}）；本轮已停止，未使用其他能力代替。"
        )
    if (
        _required_plugin_id
        and _required_plugin_mcp_ids
        and not _required_plugin_server_keys
        and not _required_plugin_skill_ids
    ):
        raise RuntimeError(
            f"显式调用的插件「{_required_plugin_name}」的 MCP 服务当前不可用；本轮已停止。"
        )
    enabled_servers = _filter_mcp_servers_by_keys(
        enabled_mcp_keys,
        owned_servers=owned_mcp_servers,
        bridge_servers=bridge_mcp_servers,
    )
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
    # One line per skill instead of a five-tag XML block. The old shape spent
    # ~120 characters per skill on scaffolding plus a verbatim second copy of
    # the id inside <dir> — with 20+ skills installed that was ~2.5k characters
    # (≈1k tokens) of pure redundancy re-prefilled on every request and every
    # ReAct round. The directory is stated once as a template here because the
    # basename is always the skill id (holds for DB / built-in / private /
    # market skills alike, per the note above).
    _SKILL_INSTRUCTION_TEMPLATE = (
        "# 技能（Agent Skills）\n"
        "以下是当前可用的技能列表。**技能不是工具，不能直接调用。**\n"
        "当用户请求匹配某技能的描述时，你**必须先**使用 `view_text_file` 工具读取"
        "`/workspace/skills/<技能名>/SKILL.md`（`<技能名>` 原样取下方列表的技能名），"
        "然后严格按其中指令执行。\n"
        "**禁止跳过加载步骤直接调用 MCP 工具。**\n\n"
        "# 可用技能（`技能名`：适用场景）：{% for skill in skills %}\n"
        "- `{{ skill.dir.rstrip('/').split('/')[-1] }}`"
        "{% if skill.name and skill.name != skill.dir.rstrip('/').split('/')[-1] %}"
        "（{{ skill.name }}）{% endif %}"
        "：{{ skill.description }}{% endfor %}"
    )

    # The declarative permission middleware governs built-in tools only. A
    # resident web conversation can answer prompts; trusted unattended entry
    # points (external channels and automation) bypass built-in policy checks.
    # MCP tools are temporarily a whitelist and never enter this registry.
    _is_channel_run = bool((channel_origin or {}).get("channel_id"))
    _tool_approval_available = bool(
        chat_id is not None
        and not batch_mode
        and not isolated
        and not plan_mode
        and not _is_channel_run
        and not automation_run
    )

    if not disable_tools and enabled_servers:
        from core.llm.mcp_pool import HTTP_TRANSPORTS, make_client, uses_manifest_schema

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
            if _http_cfg.get("schema_source") == "cloud_manifest" and not uses_manifest_schema(
                _http_cfg
            ):
                _log.warning(
                    "[factory] HTTP MCP '%s' ignored because its cloud manifest is invalid",
                    key,
                )
                return None
            client = make_client(key, _http_cfg, is_stateful=False)
            if uses_manifest_schema(_http_cfg):
                # The complete, authorized schema arrived in the dynamic cloud
                # manifest. Agent construction is network-free; only a model-
                # selected tool call reaches the JSON gateway.
                _HTTP_MCP_FAIL_AT.pop(key, None)
                _log.info(
                    "[factory] HTTP MCP '%s' loaded from cloud manifest (%d tools)",
                    key,
                    len(_http_cfg.get("manifest_tools") or []),
                )
                return client
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

    connected_clients = {
        str(getattr(client, "name", "") or ""): client for client in [*mcp_clients, *http_clients]
    }

    async def _mcp_tool_names_for_servers(server_keys: List[str], *, log_prefix: str) -> List[str]:
        connected_keys = [key for key in server_keys if key in connected_clients]
        if not connected_keys:
            return []
        listed_tools = await asyncio.gather(
            *(connected_clients[key].list_tools() for key in connected_keys),
            return_exceptions=True,
        )
        names: List[str] = []
        for server_key, result in zip(connected_keys, listed_tools):
            if isinstance(result, BaseException):
                _log.warning(
                    "[%s] list_tools failed for %s: %s",
                    log_prefix,
                    server_key,
                    result,
                )
                continue
            for tool in result:
                tool_name = str(getattr(tool, "name", "") or "")
                if tool_name and tool_name not in names:
                    names.append(tool_name)
        return names

    _required_connector_tool_names = await _mcp_tool_names_for_servers(
        _required_connector_server_keys,
        log_prefix="connector-required",
    )
    if _required_connector_server_keys:
        if not _required_connector_tool_names:
            raise RuntimeError(
                "显式选择的连接器未能连接或没有暴露可调用工具"
                f"（{', '.join(_required_connector_ids)}）；本轮已停止。"
            )

    _required_plugin_mcp_tool_names = await _mcp_tool_names_for_servers(
        _required_plugin_server_keys,
        log_prefix="plugin-required",
    )
    if (
        _required_plugin_id
        and not _required_plugin_skill_ids
        and not _required_plugin_mcp_tool_names
    ):
        raise RuntimeError(
            f"显式调用的插件「{_required_plugin_name}」没有可供当前会话执行的能力；" "本轮已停止。"
        )

    # ── Phase 3: Skill registration (fast — metadata already cached) ──
    # disable_tools=True is a "bare LLM" mode used by plan-generate and the
    # final-summary pass: caller wants pure text output, no tool access at
    # all. Skip skills AND sandbox/artifact/file tools — otherwise the agent
    # happily calls bash/view_text_file mid-generation and corrupts JSON.
    skill_ids_to_register = enabled_skill_ids
    if skill_ids_to_register is None:
        skill_ids_to_register = _effective_main_available_skills()
        # The fallback resolves from the static catalog, which today carries no
        # evolution-authored ids — but the exposure gate is applied here anyway
        # rather than relying on that. A gate that only covers the paths we
        # happened to think of is not a gate.
        skill_ids_to_register = _filter_skill_ids_for_user(skill_ids_to_register, current_user_id)
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
        from core.agent_skills.config import (
            get_enabled_skill_sources,
            get_sandbox_skills_dir,
            get_user_skills_dir,
        )

        for src in get_enabled_skill_sources():
            root = str(src.root_dir)
            if os.path.isdir(root) and root not in allowed_skill_dirs:
                allowed_skill_dirs.append(root)
        # Shared skills dir + this user's own dir (see the layout note in
        # agent_skills.config). Blanket-allow both so view_text_file can read any
        # materialized skill the user is entitled to, even one not in
        # skill_ids_to_register — and only those: another user's private skill
        # files stay outside the allow-list.
        _own_roots = [get_sandbox_skills_dir(), get_user_skills_dir(current_user_id)]
        for _root in _own_roots:
            if _root is None:
                continue
            if str(_root) not in allowed_skill_dirs:
                allowed_skill_dirs.append(str(_root))

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

    # Browser-backed model questions are deliberately a top-level standard-chat
    # capability. Channels, automation, batch/plan workers and subagents have no
    # resident composer to answer them; turbo mode keeps its bounded fast path.
    from core.llm.tools import ask_user_question_tool

    if ask_user_question_tool.should_register_ask_user_question(
        top_level_chat=top_level_chat,
        turbo_mode=turbo_mode,
        disable_tools=disable_tools,
        chat_id=chat_id,
    ):
        ask_user_question_tool.register_ask_user_question(
            toolkit,
            chat_id=chat_id,
            interactive=True,
        )

    if not disable_tools and turbo_mode and not _turbo_code_exec:
        # Turbo keeps only cross-turn attachment access (the file-context hook
        # references this tool for historical attachments); every other native
        # tool — sandbox/bash/file ops — is out of scope for quick lookup.
        register_read_artifact(toolkit, user_id=current_user_id)
        if skill_ids_to_register:
            # An explicitly summoned skill needs its SKILL.md readable (the
            # user-message injection tells the model to view_text_file it).
            # bash/sandbox stay off: a skill that requires code execution gets
            # explained with a mode-switch suggestion, not executed (see the
            # turbo prompt).
            register_sandboxed_view_text_file(
                toolkit,
                allowed_skill_dirs,
                loader,
                loaded_skill_ids=loaded_skill_ids,
            )
    # 收窄模式开了代码执行位（_turbo_code_exec）就走完整原生工具注册：MCP/技能面
    # 仍按模式收窄（上面已经归一），但沙箱/文件/产物工具与标准模式同一套。
    if not disable_tools and (not turbo_mode or _turbo_code_exec):
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
        if allow_bash:
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
            design_picker_tool.skill_uses_choose_design(str(sid)) for sid in skill_ids_to_register
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

        # ── Phase 3.7a: view_image (vision bridge) ──
        # Same rationale as read_artifact — images can arrive in any run (upload,
        # channel attachment, a chart the agent just rendered), and read_artifact
        # cannot parse them. Only registered when the running model can't see images
        # itself: a natively multimodal model gets the picture inline and proxying it
        # through a second model would only lose fidelity.
        if _vision_bridge_needed():
            register_view_image(
                toolkit,
                chat_id=chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=current_user_id,
                project_folder_name=_proj_folder_name,
                scope=_proj_scope,
            )

        # ── Phase 3.7b: channel_read_attachment (channel runs only) ──
        # Group listening records bystander attachments by key without downloading them;
        # this is how the agent pulls one in on demand. Gated on channel runs so the tool
        # never clutters the toolkit of web conversations, where it could never resolve.
        if _is_channel_run:
            register_channel_attachment(toolkit, user_id=current_user_id, chat_id=chat_id)

        # ── Phase 3.8: Register pin_to_workspace ──
        # Lets the agent gate which generated files reach the user-visible
        # assistant message. See core/llm/workspace.py for the per-run state.
        register_pin_to_workspace(toolkit, scope=_proj_scope)

        # ── Phase 3.85: run_job（工作流模式的作业编排面） ──
        # **用户显式触发才注册**（workflow_mode）：斜杠命令 /workflow 或 + 菜单选「工作流
        # 模式」。与计划模式/批量执行同属用户触发的模式——不触发就完全不存在，普通问答的
        # 工具面与提示词一点都不受影响。
        # 触发之后它才是这段对话的原生能力（不走 catalog 开关、关不掉）：面对 N 个同构
        # 工作项时，主循环逐项处理会让每一轮重发全部历史（成本随进度二次方增长，做不完
        # 就自行收工）。run_job 把循环体交给沙箱脚本，模型调用由后端代持凭据派出——
        # 脚本因此拿不到任何 key。isolated=True 的子作业不注册，杜绝 job 套 job。
        if workflow_mode and not isolated and _sbx_sess:
            from core.llm.tools.job_tool import register_run_job

            register_run_job(
                toolkit,
                user_id=current_user_id or "",
                chat_id=chat_id,
                sandbox_session_id=_sbx_sess,
                allowed_tools=sorted(enabled_mcp_keys or []),
                model_name=model_name,
                model_provider_id=model_provider_id,
            )

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
    _log.info("[factory] +%s tools registered", _elapsed())
    _agent_ref: Optional[Dict] = None
    # Progressive plugin runtime holder — filled in stages: registered with the
    # collector below, then completed after the real Toolkit / AgentRuntimeState
    # exist (the load_plugin closure mutates both at activation time).
    _plugin_runtime: Optional[Dict[str, Any]] = None

    _ontology_runtime = ontology_runtime if isinstance(ontology_runtime, dict) else {}
    skill_metadata = loader.load_all_metadata()
    _log.info(
        "[factory] +%s skill metadata loaded (%d)",
        _elapsed(),
        len(skill_metadata or {}),
    )
    for skill_id in skill_ids_to_register or []:
        metadata = skill_metadata.get(skill_id)
        register_runtime_asset_tags(
            _ontology_runtime,
            kind="skill",
            asset_id=skill_id,
            tags=list(getattr(metadata, "tags", []) or []),
        )
    for visible_agent in visible_subagents or []:
        extra = visible_agent.get("extra_config") or {}
        tags = list(visible_agent.get("ontology_tags") or [])
        tags.extend(extra.get("ontology_tags") or [])
        register_runtime_asset_tags(
            _ontology_runtime,
            kind="subagent",
            asset_id=str(visible_agent.get("agent_id") or ""),
            tags=tags,
        )
    _ontology_hidden_tools = {
        tool_name
        for pack in _ontology_runtime.get("packs", [])
        for workflow in pack.get("workflows", [])
        for tool_name in workflow.get("forbidden_tools", [])
    }

    def _build_toolkit() -> Toolkit:
        # ``toolkit`` here is still the ToolCollector; construct the real Toolkit from the current collection state.
        #
        # "Skill" (AgentScope's builtin SkillViewer) is hidden unconditionally —
        # distinct from the ontology forbidden_tools above. The Toolkit injects
        # its schema on every request whenever any skill is registered, but this
        # stack loads skills exclusively through view_text_file (sandbox↔backend
        # path mapping, {baseDir} substitution, runtime hint, skill_call
        # observability). A model that called Skill instead would bypass all of
        # that and receive backend-path content unusable in the sandbox — so the
        # schema is pure per-round prefill waste plus a wrong door.
        return OntologyFilteredToolkit(
            tools=toolkit.function_tools,
            mcps=[*mcp_clients, *http_clients],
            skills_or_loaders=toolkit.skill_loaders or None,
            skill_instruction_template=_SKILL_INSTRUCTION_TEMPLATE,
            hidden_tools={*_ontology_hidden_tools, "Skill"},
        )

    # Compute schemas first in the "subagent tools not yet registered" state
    # (consistent with 1.x: subagent tools are registered after
    # get_json_schemas, so they don't enter the system_prompt's tool list).
    tool_schemas = await _build_toolkit().get_tool_schemas()
    visible_tool_names = {
        str(schema.get("function", {}).get("name") or "")
        for schema in tool_schemas
        if isinstance(schema, dict)
    }
    if _required_connector_tool_names:
        _required_connector_tool_names = [
            name for name in _required_connector_tool_names if name in visible_tool_names
        ]
        if not _required_connector_tool_names:
            raise RuntimeError(
                "显式选择的连接器没有可供当前会话调用的工具；本轮已停止，" "未使用其他能力代替。"
            )
    _required_skill_registered = bool(
        _required_skill_id
        and _required_skill_id in (skill_ids_to_register or [])
        and loader.get_skill_dir(_required_skill_id)
        and "view_text_file" in visible_tool_names
    )
    if _required_skill_id and not _required_skill_registered:
        raise RuntimeError(
            f"显式调用的技能「{_required_skill_name}」没有可供当前会话读取的 SKILL.md；"
            "本轮已停止，未使用其他能力代替。"
        )
    _required_plugin_mcp_tool_names = [
        name for name in _required_plugin_mcp_tool_names if name in visible_tool_names
    ]
    _required_plugin_registered_skill_ids = [
        skill_id
        for skill_id in _required_plugin_skill_ids
        if skill_id in (skill_ids_to_register or [])
        and loader.get_skill_dir(skill_id)
        and "view_text_file" in visible_tool_names
    ]
    if _required_plugin_id and not (
        _required_plugin_registered_skill_ids or _required_plugin_mcp_tool_names
    ):
        raise RuntimeError(
            f"显式调用的插件「{_required_plugin_name}」没有可供当前会话执行的能力；"
            "本轮已停止，未使用其他能力代替。"
        )
    if _required_plugin_id and chat_id:
        # Progressive loading normally persists this during its resolution.
        # Persist here as well so explicit activation is sticky in restricted
        # modes, dedicated-agent chats and when progressive loading is disabled.
        from core.llm import plugin_loader as _plugin_activation

        await asyncio.to_thread(
            _plugin_activation.record_plugin_activation,
            chat_id,
            [_required_plugin_id],
        )
    if chat_id and (_required_skill_registered or _required_connector_tool_names):
        from core.llm.session_capabilities import record_session_capability_activation

        await asyncio.to_thread(
            record_session_capability_activation,
            chat_id,
            skill_ids=[_required_skill_id] if _required_skill_registered else None,
            mcp_ids=_required_connector_ids if _required_connector_tool_names else None,
        )
    _log.info("[factory] +%s tool schemas computed (%d)", _elapsed(), len(tool_schemas or []))
    # update_plan 只在下面的非子智能体分支里注册；先给默认值，子智能体分支走完
    # 也能安全判断「要不要挂计划催更中间件」。
    _plan_tool_enabled = False
    if user_agent is not None:
        system_prompt = build_subagent_system_prompt(
            user_agent,
            tool_schemas,
            enabled_mcp_keys,
            enabled_kb_ids=enabled_kb_ids,
        )
        _manifest_builder.add_prompt_section(
            "subagent/base",
            system_prompt,
            origin=f"user-agent:{getattr(user_agent, 'agent_id', '') or 'configured'}",
            trust="user_configured",
            priority=10,
            cache_class="agent",
            version=str(getattr(user_agent, "updated_at", "") or "1"),
            reference=f"agent:{getattr(user_agent, 'agent_id', '') or 'configured'}",
            sensitive=True,
        )
        _log.info(
            "[factory] +%s subagent system prompt built (%d chars)",
            _elapsed(),
            len(system_prompt),
        )
        _ontology_prompt = render_runtime_prompt(_ontology_runtime)
        if _ontology_prompt:
            system_prompt += "\n\n" + _ontology_prompt
            _manifest_builder.add_prompt_section(
                "runtime/ontology",
                _ontology_prompt,
                origin="ontology:runtime",
                trust="governed_runtime",
                priority=850,
                cache_class="run",
                version=str(_ontology_runtime.get("revision") or "1"),
                sensitive=True,
            )
            _log.info(
                "[factory] +%s subagent ontology contract injected (%d chars)",
                _elapsed(),
                len(_ontology_prompt),
            )

        # ── Progressive plugin directory + load_plugin tool (sub-agent) ──
        # Bound plugins deferred in the override block above; in-run activation
        # only (persist=False — see the note there).
        if _subagent_progressive is not None:
            from core.llm import plugin_loader as _plug

            _plugin_dir_section = _plug.build_plugin_directory_section(
                _subagent_progressive.directory
            )
            if _plugin_dir_section:
                system_prompt += "\n\n" + _plugin_dir_section
                _manifest_builder.add_prompt_section(
                    "runtime/plugin_directory",
                    _plugin_dir_section,
                    origin="plugin:directory",
                    trust="configured_service",
                    priority=820,
                    cache_class="capability_set",
                    version="1",
                    sensitive=True,
                )
            _plugin_runtime = {
                "activated_slugs": set(),
                "connected_keys": set(enabled_mcp_keys),
                "toolkit": None,
                "permission_context": None,
                "close_list": None,
                "persist": False,
                "loader": loader,
                "chat_id": chat_id,
                "user_id": current_user_id,
                "enabled_kb_ids": enabled_kb_ids,
                "channel_origin": channel_origin,
                "reranker_enabled": reranker_enabled,
                "approval_available": _tool_approval_available,
                "ontology_runtime": _ontology_runtime,
            }
            _plug.register_load_plugin(
                toolkit, _subagent_progressive.deferred_by_slug(), _plugin_runtime
            )
            _log.info(
                "[factory] +%s subagent progressive plugins: %d deferred (skills=%d, mcp=%d)",
                _elapsed(),
                len(_subagent_progressive.deferred),
                len(_subagent_progressive.deferred_skill_ids),
                len(_subagent_progressive.deferred_mcp_ids),
            )
    else:
        # ── 模式自带的专属提示词 ──
        # 手写正文优先于绑定的版本池分类；两者都空则该模式没配提示词。
        # 这段刻意放在 turbo 分支**之外**：收窄与否（tool_scope）和"要不要换提示词"
        # 是两件正交的事——一个不收窄工具面的模式（比如给标准模式换个口吻）同样该
        # 能配自己的提示词。之前把它写在 turbo 分支里，非收窄模式配了也不生效。
        _mode_prompt = ""
        if mode_spec is not None:
            from core.services import prompt_version_service as _pvs_mode

            _mode_prompt_text = getattr(mode_spec, "prompt_text", None)
            _mode_prompt_kind = getattr(mode_spec, "prompt_kind", None)
            if _mode_prompt_text:
                _mode_prompt = str(_mode_prompt_text)
            elif _mode_prompt_kind and _mode_prompt_kind != "turbo":
                _mode_prompt = _pvs_mode.render_system_prompt_of_kind(_mode_prompt_kind)

        if turbo_mode and not _turbo_code_exec:
            # Turbo swaps in the standalone prompt (DB "turbo" active version →
            # fs fallback): the default prompt's tool/workflow sections describe
            # capabilities this assembly deliberately does not carry.
            # （开了代码执行位的收窄模式不进这条：装配确实带沙箱/文件工具，
            # turbo 正文"秒级检索、不执行代码"的叙事反而是错的——没配专属
            # 提示词时走默认装配，工具段按实际 toolkit 动态生成。）
            from core.services import prompt_version_service as _pvs_turbo

            # 收窄模式没配专属提示词时退回历史的 turbo 正文——极速模式绑的就是它。
            system_prompt = _mode_prompt or _pvs_turbo.render_turbo_system_prompt()
            _manifest_builder.add_prompt_section(
                "mode/base",
                system_prompt,
                origin=f"chat-mode:{getattr(mode_spec, 'slug', None) or 'turbo'}",
                trust="admin",
                priority=10,
                cache_class="mode",
                version=str(getattr(mode_spec, "updated_at", "") or "1"),
            )
            _log.info(
                "[factory] +%s turbo system prompt built (%d chars)",
                _elapsed(),
                len(system_prompt),
            )
        elif _mode_prompt:
            # 不收窄的模式配了专属提示词：整段替换默认装配（和收窄模式同一语义），
            # 但工具/技能面不动——那是 tool_scope 管的事。
            system_prompt = _mode_prompt
            _manifest_builder.add_prompt_section(
                "mode/base",
                system_prompt,
                origin=f"chat-mode:{getattr(mode_spec, 'slug', 'configured')}",
                trust="admin",
                priority=10,
                cache_class="mode",
                version=str(getattr(mode_spec, "updated_at", "") or "1"),
            )
            _log.info(
                "[factory] +%s mode system prompt built (%d chars, slug=%s)",
                _elapsed(),
                len(system_prompt),
                getattr(mode_spec, "slug", "?"),
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
            system_prompt = build_system_prompt(
                cfg, ctx=_sp_ctx, manifest_builder=_manifest_builder
            )
            # Project material must travel as its own canonical ContextItem so
            # it can be independently budgeted and audited.  The prompt
            # builder retains this plaintext only in memory; persisted
            # execution manifests still contain hashes/references alone.
            for (
                _section,
                _section_content,
            ) in _manifest_builder.prompt_section_sources():
                if _section.id != "runtime/project" or not _section_content:
                    continue
                _start = system_prompt.rfind(_section_content)
                if _start < 0:
                    continue
                _before = system_prompt[:_start].rstrip()
                _after = system_prompt[_start + len(_section_content) :].lstrip()
                system_prompt = (
                    _before + ("\n\n" + _after if _before and _after else _after)
                ).strip()
            _log.info(
                "[factory] +%s system prompt built (%d chars)",
                _elapsed(),
                len(system_prompt),
            )

        _ontology_prompt = render_runtime_prompt(_ontology_runtime)
        if _ontology_prompt:
            system_prompt += "\n\n" + _ontology_prompt
            _manifest_builder.add_prompt_section(
                "runtime/ontology",
                _ontology_prompt,
                origin="ontology:runtime",
                trust="governed_runtime",
                priority=850,
                cache_class="run",
                version=str(_ontology_runtime.get("revision") or "1"),
                sensitive=True,
            )
            _log.info(
                "[factory] +%s ontology contract injected (%d chars, hidden_tools=%d)",
                _elapsed(),
                len(_ontology_prompt),
                len(_ontology_hidden_tools),
            )

        # ── Inject code-capability system prompt ──
        # Gating: CODE_CAPABILITY_ENABLED=true injects in all modes.
        # Single source of truth render_code_capability_segment (same source as the Config console preview).
        if code_capability_enabled() and (not turbo_mode or _turbo_code_exec):
            try:
                from core.services import prompt_version_service as _pvs

                _code_exec_text = _pvs.render_code_capability_segment()
            except Exception:
                _code_exec_text = ""
            if _code_exec_text:
                system_prompt += "\n\n" + _code_exec_text
                _manifest_builder.add_prompt_section(
                    "runtime/code_capability",
                    _code_exec_text,
                    origin="prompt-version:code_exec",
                    trust="admin",
                    priority=750,
                    cache_class="capability_set",
                    version="1",
                )
                _log.info(
                    "[factory] +%s code execution prompt injected (%d chars)",
                    _elapsed(),
                    len(_code_exec_text),
                )

        # ── Progressive plugin directory + load_plugin tool ──
        # The directory section is byte-stable per user (sorted by slug,
        # independent of activation state) so an activation never perturbs this
        # part of the prefix; only the tools/skills it adds do, once.
        if _progressive is not None:
            from core.llm import plugin_loader as _plug

            _plugin_dir_section = _plug.build_plugin_directory_section(_progressive.directory)
            if _plugin_dir_section:
                system_prompt += "\n\n" + _plugin_dir_section
                _manifest_builder.add_prompt_section(
                    "runtime/plugin_directory",
                    _plugin_dir_section,
                    origin="plugin:directory",
                    trust="configured_service",
                    priority=820,
                    cache_class="capability_set",
                    version="1",
                    sensitive=True,
                )
            _plugin_runtime = {
                "activated_slugs": set(_progressive.activated_slugs),
                "connected_keys": set(enabled_mcp_keys),
                "toolkit": None,  # the real Toolkit, filled after construction
                "permission_context": None,  # filled after AgentRuntimeState exists
                "close_list": None,  # filled right before return
                "loader": loader,
                "chat_id": chat_id,
                "user_id": current_user_id,
                "enabled_kb_ids": enabled_kb_ids,
                "channel_origin": channel_origin,
                "reranker_enabled": reranker_enabled,
                "approval_available": _tool_approval_available,
                "ontology_runtime": _ontology_runtime,
            }
            _plug.register_load_plugin(toolkit, _progressive.deferred_by_slug(), _plugin_runtime)
            _log.info(
                "[factory] +%s progressive plugins: %d in directory, %d deferred "
                "(skills=%d, mcp=%d)",
                _elapsed(),
                len(_progressive.directory),
                len(_progressive.deferred),
                len(_progressive.deferred_skill_ids),
                len(_progressive.deferred_mcp_ids),
            )

        # ── Inject batch execution hint (App Center batch-execution sessions only) ──
        if batch_mode:
            system_prompt += _BATCH_MODE_HINT
            _manifest_builder.add_prompt_section(
                "runtime/batch_mode",
                _BATCH_MODE_HINT,
                origin="builtin:batch_mode",
                trust="platform",
                priority=880,
                cache_class="mode",
                version="1",
            )
            _log.info("[factory] +%s batch mode hint injected", _elapsed())

        # ── Inject workflow-mode hint (user explicitly entered workflow mode) ──
        if workflow_mode:
            system_prompt += _WORKFLOW_MODE_HINT
            _manifest_builder.add_prompt_section(
                "runtime/workflow_mode",
                _WORKFLOW_MODE_HINT,
                origin="builtin:workflow_mode",
                trust="platform",
                priority=880,
                cache_class="mode",
                version="1",
            )
            _log.info("[factory] +%s workflow mode hint injected", _elapsed())

        # ── Register call_subagent tool for main agent ──
        if visible_subagents:
            from core.llm.builtin_subagents import refresh_builtin_subagents
            from core.llm.subagent_tool import build_subagent_prompt_section, register_subagent_tool

            # Refresh platform-default rows only after the parent toolset has
            # completed catalog defaults, permission filtering, skill-bound MCP
            # expansion, and runtime feature gates. The same snapshot drives the
            # routing prompt and the eventual child executor, so disabled tools
            # are neither advertised nor delegated.
            _subagent_parent_runtime = {
                "enabled_skill_ids": list(skill_ids_to_register or []),
                "enabled_mcp_ids": list(enabled_mcp_keys),
                "enabled_kb_ids": list(enabled_kb_ids or []),
                "sandbox_tools_enabled": (
                    os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() == "true"
                ),
                "code_capability_enabled": bool(code_capability_enabled()),
                "reranker_enabled": reranker_enabled,
                "model_name": model_name,
                "model_provider_id": model_provider_id,
                "chat_mode": chat_mode,
                "chat_id": chat_id,
                "sandbox_session_id": _sbx_sess,
                "project_ctx": project_ctx,
                "channel_origin": channel_origin,
                "automation_run": automation_run,
                "run_id": run_id,
                "journal_owner": journal_owner,
            }
            visible_subagents = refresh_builtin_subagents(
                visible_subagents,
                _subagent_parent_runtime,
            )
            _agent_ref = {"agent": None}  # set after creation
            register_subagent_tool(
                toolkit,
                visible_subagents,
                current_user_id or "",
                agent_ref=_agent_ref,
                chat_id=chat_id,
                parent_runtime=_subagent_parent_runtime,
            )
            # `mentioned_agent_ids` is consumed by the caller via
            # build_subagent_mention_hint() and injected into the current user
            # message — NOT into the system prompt. Keeping it out of the
            # system prompt preserves the LLM provider's prefix cache.
            subagent_section = build_subagent_prompt_section(visible_subagents)
            if subagent_section:
                system_prompt = system_prompt + "\n\n" + subagent_section
                _manifest_builder.add_prompt_section(
                    "runtime/subagent_directory",
                    subagent_section,
                    origin="subagent:directory",
                    trust="configured_service",
                    priority=830,
                    cache_class="capability_set",
                    version="1",
                    sensitive=True,
                )
            _log.info(
                "[factory] +%s subagent tool registered (%d agents)",
                _elapsed(),
                len(visible_subagents),
            )

        # ── Register update_plan tool (top-level interactive main conversations only, positive opt-in) ──
        # Codex-style lightweight plan tracker: for complex tasks the main
        # agent maintains a step checklist and keeps executing in the same
        # turn (no redirect, no approval gate); the frontend renders it as a
        # plan bar above the chat input. This replaced the old
        # enter_plan_mode redirect for model-initiated planning.
        # Recognizes ONLY the single positive signal top_level_chat (passed in
        # by astream_chat_workflow after it determines this is an interactive
        # main conversation) — not a negative exclusion list of "not batch and
        # not plan_mode and not …". A negative list leaks the tool with every
        # derived context it misses (historically, plan-execute steps,
        # plan-generation disable_tools, and channel runs all leaked this way);
        # a positive opt-in has one single source of truth, and all
        # derived/non-interactive constructions get nothing by default. The DB
        # switch auto_plan_entry_enabled (which itself returns False on
        # config-layer errors) can turn this off entirely.
        from core.services.system_config import auto_plan_entry_enabled

        if top_level_chat and auto_plan_entry_enabled():
            from core.llm.plan_update_tool import (
                build_plan_update_prompt_section,
                register_plan_update_tool,
            )

            # 计划栏的催更中间件挂在同一个正向开关上：工具没注册就绝不该有人催更新。
            _plan_tool_enabled = True
            register_plan_update_tool(toolkit)
            _pu_section = build_plan_update_prompt_section()
            if _pu_section:
                system_prompt = system_prompt + "\n\n" + _pu_section
                _manifest_builder.add_prompt_section(
                    "runtime/plan_tool",
                    _pu_section,
                    origin="builtin:update_plan",
                    trust="platform",
                    priority=840,
                    cache_class="capability_set",
                    version="1",
                )
            _log.info(
                "[factory] +%s update_plan tool registered (chat_id=%s)",
                _elapsed(),
                chat_id,
            )

    # Prompt fragments this task type carries, per the active profile.
    #
    # This is the structural answer to a measured failure: giving a rule a scope
    # *in prose* does not work — the model either ignores the exception or
    # over-applies it, and both were observed against a real model. A fragment
    # attached to a profile reaches only the task types that profile governs, so
    # the scope is enforced by what is assembled rather than by what the text
    # asks the model to infer.
    if profile.prompt_fragments:
        fragments = await asyncio.to_thread(_resolve_prompt_fragments, profile.prompt_fragments)
        if fragments:
            _dynamic_block = _render_dynamic_block(fragments)
            system_prompt = system_prompt + "\n\n" + _dynamic_block
            _manifest_builder.add_prompt_section(
                "runtime/evolved_fragments",
                _dynamic_block,
                origin="evolution:prompt_fragments",
                trust="governed_runtime",
                priority=920,
                cache_class="profile",
                version=str(profile.version),
                reference=f"profile:{profile.profile_id}",
                sensitive=True,
            )
            _log.info(
                "[factory] +%s %d profile prompt fragment(s) appended",
                _elapsed(),
                len(fragments),
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
                _disable_thinking = _mode in ("fast", "turbo")
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
    _mode_role = model_role or ("plan_agent" if plan_mode else None)
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
                "[factory] %s model resolve failed: %s, falling back to main_agent",
                _mode_role,
                exc,
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
                    _fallback_cfg.max_tokens if _fallback_cfg else None
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

    if run_id:
        from core.llm.model_usage import instrument_model_usage

        instrument_model_usage(default_model)

    _log.info("[factory] +%s model created", _elapsed())

    # ── Compaction-window logging: read the actually effective context_size directly off the model object ──
    # make_chat_model already resolves the real context_length from the Config
    # model configuration and bakes it into the model (no default fallback —
    # construction errors when unconfigured), so what we log here is exactly the
    # value the compaction decision actually uses; we no longer resolve a
    # separate "logging-only" window (the old implementation's log once
    # disagreed with the actually effective value).
    _ctx_window = int(getattr(default_model, "context_size", 0) or 0)
    # Resolve the shared ratio once instead of reading config at each ReAct step.
    from core.services.compaction_service import resolve_token_limit, resolve_trigger_ratio

    _trigger_ratio = resolve_trigger_ratio()
    _log.info(
        "[factory] compaction: model=%s, context_size=%d, trigger_threshold=%s (ratio=%s)",
        getattr(default_model, "model", None) or "(unknown)",
        _ctx_window,
        resolve_token_limit(_ctx_window, ratio=_trigger_ratio),
        _trigger_ratio,
    )

    # ContextConfig handles tool-result offloading and the AgentScope fallback.
    # AgentScope rejects a ratio of 0.9 or above — its own constraint, unrelated
    # to our compaction policy, so it is expressed against that policy's ceiling
    # rather than restated as a literal.
    context_config = ContextConfig(
        trigger_ratio=min(_trigger_ratio, AUTO_COMPACT_MAX_RATIO - 0.01),
        # 单条工具结果进上下文的上限（超出部分 offloader 落盘到 /workspace/.offload，
        # 模型按需读回）。保持 20k 不再收紧：批量场景已由 run_job 接走（逐项结果根本
        # 不进主上下文），主对话这边继续保留完整的单条可读性更划算。需要时用
        # CHAT_TOOL_RESULT_LIMIT 按部署调。
        tool_result_limit=(
            int(tool_result_limit) if tool_result_limit else _settings.compaction.tool_result_limit
        ),
        compression_prompt=SUMMARIZATION_PROMPT,
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
    #
    # **The main agent has no turn cap.** A fixed round ceiling bounds the wrong
    # axis: what a long task actually exhausts is context, not rounds, and any
    # number picked here is simultaneously too low for report-scale work and too
    # high to catch a genuine runaway. Neither of the two things a cap was
    # supposed to buy needs it:
    #   - runaway protection lives in the chat-run watchdog, which can see
    #     wall-clock and output (CHAT_RUN_INACTIVITY_TIMEOUT_SEC /
    #     CHAT_RUN_MAX_AGE_SEC / CHAT_RUN_HARD_MAX_AGE_SEC reap silent and
    #     immortal runs regardless of how many rounds they took);
    #   - context exhaustion is handled by compaction.
    # ``_UNBOUNDED_ITERS`` is a loop backstop, not a budget — AgentScope's
    # ReActConfig needs an int, and this one sits far above any real turn.
    #
    # Bounded budgets survive only where the bound is a deliberate contract:
    # sub-agents (a delegated task that must come back), turbo's quick-lookup
    # cap, a custom agent's own ``max_iters``, a published profile's turn
    # budget, and the CHAT_MAIN_MAX_ITERS opt-in for operators who do want the
    # main agent fenced.
    _UNBOUNDED_ITERS = 100_000
    _DEFAULT_SUBAGENT_ITERS = 10
    _agent_name = "hugagent_agent"
    _max_iters = _UNBOUNDED_ITERS
    if max_iters is not None:
        _max_iters = max_iters
    elif user_agent is not None:
        _agent_name = (
            f"subagent_{user_agent.agent_id}" if isolated else f"agent_{user_agent.agent_id}"
        )
        _max_iters = user_agent.max_iters or (
            _DEFAULT_SUBAGENT_ITERS if isolated else _UNBOUNDED_ITERS
        )
    elif isolated:
        _max_iters = _DEFAULT_SUBAGENT_ITERS
    else:
        # Both main-agent caps are opt-in and absent by default: the built-in
        # profile now carries 0 ("no cap"), so profile resolution only bounds the
        # loop when someone publishes a profile that deliberately sets a turn
        # budget. The env override still wins over it — the profile validation
        # range tops out at 80 turns, far below what e.g. a multi-hour
        # report-generation run needs (container restart required to change,
        # like all env config).
        if profile.max_react_turns:
            _max_iters = profile.max_react_turns
        from core.config.settings import _env, _int

        _env_iters = _int(_env("CHAT_MAIN_MAX_ITERS"), 0)
        if _env_iters > 0:
            _max_iters = max(3, _env_iters)

    if turbo_mode:
        # Hard cap for the quick-lookup contract: 1-2 retrieval rounds (each may
        # fan out parallel calls) plus the final answer. Admin-tunable via
        # 系统配置「极速模式」turbo.max_iters; wins over profile/env.
        from core.services.system_config import turbo_max_iters

        _mode_iters = getattr(mode_spec, "max_iters", None) if mode_spec else None
        if _mode_iters:
            _max_iters = min(_max_iters, int(_mode_iters))
        elif not _turbo_code_exec:
            _max_iters = min(_max_iters, turbo_max_iters())
        # else: 开了代码执行位且模式没配上限 → 不套极速的检索档硬顶（默认 4 轮
        # 会把跑代码的任务掐死），按 profile/env 的常规上限走。

    # Budget spending policy, injected only where a budget exists. Appended last
    # so it sees the final number after every narrowing above (turbo included);
    # skipped without tools, where "spend your rounds on parallel calls" has
    # nothing to describe.
    if _max_iters < _UNBOUNDED_ITERS and not disable_tools:
        _turn_budget_hint = _render_turn_budget_hint(_max_iters)
        system_prompt += _turn_budget_hint
        _manifest_builder.add_prompt_section(
            "runtime/turn_budget",
            _turn_budget_hint,
            origin="orchestration:profile",
            trust="governed_runtime",
            priority=990,
            cache_class="run_policy",
            budget=int(_max_iters),
            version=str(profile.version),
            reference=f"profile:{profile.profile_id}",
        )

    # ── Create the Agent (AgentScope 2.0) ──
    # Note: long_term_memory is not passed — mem0 is fully stripped from the SSE
    # main path (manual non-blocking pipeline).
    # hooks → middlewares (onion model, first in the list is outermost);
    # _jx_context → AgentRuntimeState. Only fields known to this function are
    # filled; per-request fields (chat_mode / user_message_text /
    # uploaded_files / historical_files) are set on agent.state by the caller
    # (streaming/workflow) after creation (replacing 1.x's
    # agent._jx_context = ModelContext(...)).
    # Compile the run-visible built-in-tool registry before constructing the
    # agent. Registry absence is intentional pass-through; MCP tools never
    # enter this gateway while the trusted-whitelist policy is active.
    from core.llm.tool_permissions import (
        PermissionRuntime,
        ToolPermissionMiddleware,
        ToolPermissionRegistry,
        ToolPermissionService,
    )

    _builtin_tool_names = {ft.name for ft in toolkit.function_tools}
    _all_tool_names = {
        str(s.get("function", {}).get("name") or "")
        for s in tool_schemas
        if s.get("function", {}).get("name")
    } | _builtin_tool_names
    _permission_registry = ToolPermissionRegistry()
    for _tool_name, _permission_spec in toolkit.permission_specs.items():
        _permission_registry.register(
            _tool_name,
            _permission_spec,
            source="native",
        )
    _permission_service = ToolPermissionService(
        _permission_registry,
        PermissionRuntime(
            chat_id=chat_id,
            user_id=current_user_id,
            interactive=_interactive,
            approval_available=_tool_approval_available,
            default_allow=_default_allow_builtin_tools(
                channel_origin=channel_origin,
                automation_run=automation_run,
            ),
            approval_mode=approval_mode,
        ),
    )

    # AgentScope's native permission engine remains a coarse first gate. Names
    # present in the registry additionally pass ToolPermissionMiddleware and
    # the final Toolkit ticket guard below.
    from agentscope.permission import PermissionContext

    _state = AgentRuntimeState(
        # The effective model name is read directly off the model object (the AS2 attribute is .model), same source as the compression window
        model_name=getattr(default_model, "model", None) or model_name or "",
        model_pinned=_subagent_model_pinned,
        user_id=current_user_id,
        chat_id=chat_id,
        run_id=run_id,
        journal_owner=journal_owner,
        ontology_enabled=bool(_ontology_runtime.get("enabled")),
        ontology_runtime=_ontology_runtime,
        permission_context=PermissionContext(),
    )

    _policy_middlewares: list = [
        DynamicModelMiddleware(),  # on_reply: switch models by chat_mode
        FileContextMiddleware(),  # on_reply: inject file context
        SteerMiddleware(),  # on_acting/on_reasoning: inject queued user steer before tool I/O
        WorkspacePinHintMiddleware(),  # on_reasoning: remind to pin
        IterBudgetReminderMiddleware(),  # on_reasoning: inject a wrap-up reminder near max_iters
        # on_acting: the active profile's intervention rules, applied to *this*
        # loop. Previously they only reached the autonomous loop, which left the
        # orchestration profile with one field that governed nothing on the axis
        # almost all traffic takes.
        StallInterventionMiddleware(profile.intervention_rules),
        OntologyGateMiddleware(_ontology_runtime),  # on_acting: zero-LLM L-a contract gate
        ToolPermissionMiddleware(_permission_service),
        CitationAnchorMiddleware(),  # on_acting: 证据锚点——工具结果回给模型前发号回注 cite_id
        ActingToolCallIdMiddleware(),  # on_acting: expose call_subagent's tool_call.id to tools (parent-child linkage)
        ToolEffectMiddleware(),  # on_acting: durable Intent before every actual tool invocation
    ]
    if _required_connector_tool_names:
        # Place the hard connector contract before all reasoning/acting policy
        # middleware. Its explicit tool_choice therefore wins, including on the
        # final iteration where IterBudget would otherwise force text.
        _policy_middlewares.insert(
            2,
            ExplicitConnectorToolChoiceMiddleware(
                connector_ids=_required_connector_ids,
                tool_names=_required_connector_tool_names,
            ),
        )
    if _required_plugin_id:
        # A plugin chip is an execution request, not a hint. Restrict the model
        # to this plugin's own skill loader/MCP tools until one completes, then
        # release the normal tool surface for the rest of the answer.
        _policy_middlewares.insert(
            2,
            ExplicitPluginToolChoiceMiddleware(
                plugin_id=_required_plugin_id,
                plugin_name=_required_plugin_name,
                skill_ids=_required_plugin_registered_skill_ids,
                mcp_tool_names=_required_plugin_mcp_tool_names,
            ),
        )
    if _required_skill_id:
        _policy_middlewares.insert(
            2,
            ExplicitSkillToolChoiceMiddleware(
                skill_id=_required_skill_id,
                skill_name=_required_skill_name,
            ),
        )
    # on_reasoning: 会话里有未收敛的批量作业时，每轮把台账数字回灌进上下文。
    # 进度是外部事实（job_items 表），不是模型的记忆——不主动回灌，隔十几轮之后就会
    # 退化成"边际收益递减，先交付吧"（568 行只补 66 行正是这么停的）。
    # 子作业内部不挂（isolated），避免嵌套噪声。
    if workflow_mode and not isolated and chat_id:
        _policy_middlewares.append(
            JobLedgerReminderMiddleware(chat_id=chat_id, user_id=current_user_id)
        )
    # on_reasoning: 计划栏停在半路时把当前清单回灌回去，催模型调 update_plan。
    # 只在真的注册了 update_plan 工具的那条路径上挂（同一个正向开关），派生/非交互
    # 的构造一律拿不到——催一个不存在的工具只会让模型编造调用。
    if _plan_tool_enabled:
        _policy_middlewares.append(PlanStaleReminderMiddleware())
    _policy_middlewares.append(FinishPinGuardMiddleware(batch_mode=batch_mode))
    # The Agent sees one framework adapter. Transitional AgentScope policies
    # execute inside its compatibility chain and can be deleted one by one as
    # their neutral HookSpec replacements reach parity.
    _middlewares: list = [AgentScopeHookAdapter(legacy_middlewares=tuple(_policy_middlewares))]

    # At this point the collector has gathered all tools (including any subagent tools) → construct the final Toolkit.
    toolkit = _build_toolkit()
    toolkit.set_tool_permission_service(_permission_service)

    # Allow all registered tools via native allow_rules (replacing BYPASS, see the explanation above).
    from agentscope.permission import PermissionBehavior, PermissionRule

    _state.permission_context.allow_rules = {
        n: [
            PermissionRule(
                tool_name=n,
                rule_content="",
                behavior=PermissionBehavior.ALLOW,
                source="jx_permission_manifest",
            )
        ]
        for n in _all_tool_names
    }

    def _manifest_for_surface(surface):  # noqa: ANN001, ANN202
        """Build one manifest generation from a toolkit-owned snapshot."""
        generation_builder = _manifest_builder.fork()
        tool_manifest_from_schemas(
            generation_builder,
            surface.tool_schemas,
            builtin_tool_names=_builtin_tool_names,
        )
        _effective_system_prompt = system_prompt
        _skill_instructions = surface.skill_instructions
        if _skill_instructions:
            generation_builder.add_prompt_section(
                "runtime/agent_skills",
                _skill_instructions,
                origin="agentscope:skill_registry",
                trust="configured_service",
                priority=1000,
                cache_class="capability_set",
                version="1",
                sensitive=True,
            )
            _effective_system_prompt = system_prompt + "\n" + _skill_instructions
        return generation_builder.build(
            final_prompt=_effective_system_prompt,
            surface_generation=surface.generation,
        )

    def _bind_manifest(manifest):  # noqa: ANN001, ANN202
        from core.evolution.runtime_binding import bind_runtime_assets

        return bind_runtime_assets(
            run_id=run_id,
            skill_ids=skill_ids_for_bindings,
            kb_ids=enabled_kb_ids,
            model_name=model_name,
            model_provider_id=model_provider_id,
            chat_mode=chat_mode,
            memory_enabled=memory_enabled,
            workspace_id=str(workspace_id or "default"),
            orchestration_profile_id=profile.profile_id,
            workflow_policy_version=profile.version,
            execution_manifest=manifest,
            manifest_required=True,
        )

    # Build the manifest from the exact same frozen surface AgentScope will use
    # for its first model request. Late tools (call_subagent/update_plan/
    # load_plugin) are already registered at this point.
    execution_manifest = None
    _compaction_tool_schemas = tool_schemas
    _compaction_system_prompt = system_prompt
    try:
        _initial_surface = await toolkit.freeze_execution_surface()
        _compaction_tool_schemas = _initial_surface.tool_schemas
        if _initial_surface.skill_instructions:
            _compaction_system_prompt = system_prompt + "\n" + _initial_surface.skill_instructions
        execution_manifest = _manifest_for_surface(_initial_surface)
        _log.info(
            "[manifest] generation=%s aggregate=%s prompt=%s tools=%s context=%s",
            execution_manifest.surface_generation,
            execution_manifest.aggregate_hash,
            execution_manifest.prompt_hash,
            execution_manifest.tool_manifest_hash,
            execution_manifest.context_hash,
        )
    except Exception as exc:  # pragma: no cover - evidence must not fail a turn
        _log.warning("[manifest] final manifest unavailable: %s", exc)

    # ── Runtime Binder (GCE ticket 03 / Harness 4.7) ──────────────────
    # Freeze the governed versions together with the final sanitized manifest.
    # This remains before Agent construction/execution, so an admin publish
    # cannot drift the run after binding.
    try:
        asset_bundle = _bind_manifest(execution_manifest)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[binder] binding skipped: %s", exc)

    # Complete the progressive-plugin runtime holder now that the real Toolkit
    # and the permission context exist (load_plugin mutates both mid-run).
    if _plugin_runtime is not None:
        _plugin_runtime["toolkit"] = toolkit
        _plugin_runtime["permission_context"] = _state.permission_context

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

    # CompactingAgent, not the bare framework Agent: its compress_context
    # override is what routes the ReAct step boundary into the one compaction
    # engine and persists the checkpoint (see core/llm/compacting_agent.py).
    agent = CompactingAgent(
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
    # Compaction trigger accounting reuses the exact already-frozen execution
    # surface instead of re-querying MCPs on the pre-turn latency path.
    agent._jx_compaction_system_prompt = _compaction_system_prompt
    agent._jx_compaction_tool_schemas = _compaction_tool_schemas

    # Stamp the console-resolved trigger ratio so the step-boundary compaction
    # check stays a pure computation (resolving it per step would put a DB read,
    # and a seed write on a cold config cache, inside the ReAct loop).
    try:
        agent._jx_trigger_ratio = _trigger_ratio
    except Exception:  # pragma: no cover - agent may reject attributes
        pass

    # Set the agent reference so the call_subagent closure can extract shared context
    if _agent_ref is not None:
        _agent_ref["agent"] = agent

    # Carry the frozen bundle on the agent so the post-response evidence
    # assembler can record what this run actually used without re-resolving it
    # (by then the versions may already have moved).
    agent.bind_execution_surface(execution_manifest, bundle=asset_bundle)

    async def _publish_context_manifest(request_manifest):  # noqa: ANN001, ANN202
        """Bind the exact post-budget context manifest used for this request."""
        try:
            from core.evolution.runtime_binding import rebind_execution_manifest

            request_bundle = rebind_execution_manifest(
                run_id=run_id,
                base_bundle=getattr(agent, "_jx_asset_bundle", None),
                execution_manifest=request_manifest,
            )
            agent.bind_request_evidence(request_manifest, bundle=request_bundle)
            _log.info(
                "[manifest] request aggregate=%s context_manifest=%s",
                request_manifest.aggregate_hash,
                request_manifest.context_manifest_hash,
            )
        except Exception as exc:  # evidence refresh must not fail the turn
            _log.warning("[manifest] request context binding unavailable: %s", exc)
            # Keep the in-memory request manifest even if durable evidence is
            # unavailable, but remove the stale base bundle. Episode assembly
            # treats a missing bundle as partial instead of falsely persisting
            # the pre-context surface as complete request evidence.
            agent.clear_request_evidence(request_manifest)
            from core.evolution.runtime_binding import clear_run_binding

            clear_run_binding(run_id)

    agent.set_context_manifest_listener(_publish_context_manifest)

    async def _publish_surface_generation(surface):  # noqa: ANN001, ANN202
        """Refresh run evidence before AgentScope consumes a changed surface."""
        # Progressive load_plugin changes the actual prompt/tool surface during
        # a run. Keep post-turn compaction on the latest generation even when
        # evidence binding itself is temporarily unavailable.
        cache_compaction_execution_surface(agent, system_prompt, surface)
        try:
            next_manifest = _manifest_for_surface(surface)
            next_bundle = _bind_manifest(next_manifest)
            agent.bind_execution_surface(next_manifest, bundle=next_bundle)
            _log.info(
                "[manifest] generation=%s aggregate=%s",
                next_manifest.surface_generation,
                next_manifest.aggregate_hash,
            )
        except Exception as exc:  # evidence refresh must not fail the turn
            _log.warning("[manifest] surface generation refresh unavailable: %s", exc)
            try:
                agent.bind_execution_surface(None, bundle=_bind_manifest(None))
            except Exception:  # pragma: no cover - last-resort availability path
                pass

    toolkit.set_execution_surface_listener(_publish_surface_generation)
    if skill_selection is not None:
        try:
            setattr(agent, "_jx_skill_selection", skill_selection)
        except Exception:  # pragma: no cover
            pass
    # Which assembly governed this run, for the turn card. Carried rather than
    # re-resolved after the response: by then a profile may have been published
    # or switched off, and the card would name one the run never used.
    try:
        setattr(
            agent,
            "_jx_profile",
            {
                "profile_id": profile.profile_id,
                "version": profile.version,
                "task_types": list(profile.task_types),
                "narrowed_tools": profile.tool_allowlist is not None,
            },
        )
    except Exception:  # pragma: no cover
        pass

    _log.info("[factory] +%s agent created, TOTAL setup done", _elapsed())

    # Only transient (per-request) stdio clients + HTTP clients get closed;
    # pooled stable clients stay open for reuse (closing them would defeat the
    # pool and hand dead clients to the next request).
    all_transient = [*transient_mcp_clients, *http_clients]
    # Clients connected by a mid-run load_plugin activation are appended to this
    # same list object, so the caller's close_clients() teardown covers them.
    if _plugin_runtime is not None:
        _plugin_runtime["close_list"] = all_transient
    return agent, all_transient
