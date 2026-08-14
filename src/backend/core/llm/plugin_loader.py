"""Progressive plugin loading（渐进式插件加载）.

安装后的插件默认把 N 个技能的 yaml 头 + M 个 MCP 工具的完整 JSON Schema 全量
注入每一轮请求（技能进系统提示词末尾的技能清单，工具进 tools 参数），插件越装
越多，首字延迟（TTFT）随之线性膨胀。本模块把「插件」这个聚合单位带回运行时：

- **装配期**：插件的组件（技能 / MCP server）从本轮的 enabled 集合中剔除，系统
  提示词只保留一行「插件目录」条目（插件名 + 描述），并注册一个 ``load_plugin``
  工具。
- **激活期**：模型判断需要某插件时调用 ``load_plugin``，此时才连接该插件的 MCP
  server（追加进 Toolkit basic 组的 mcps）、注册其技能（追加 LocalSkillLoader），
  下一轮 ReAct 请求里工具 schema 与技能清单即出现（AS2 的
  ``_prepare_model_input`` 每轮重算，无缓存）。
- **粘滞性**：激活写入 ``ChatSession.extra_data["activated_plugins"]``——同一会话
  后续轮次装配期直接不再延迟该插件（其组件回到常规装配位置）。显式呼唤插件
  （``/`` 斜杠、``+`` 菜单展开的 skill_ids/mcp_ids）视同激活并同样落库。

与前缀缓存（prefix caching）的关系：插件目录段内容与顺序对同一用户稳定（按
slug 排序、不随激活状态变化），激活行为本身会在「激活那一轮」与「激活后的下一
轮」（组件回到常规装配位）各击穿一次前缀缓存，之后前缀重新稳定。未被引用的
插件则永远不再为每轮 prefill 付费。

关闭开关：环境变量 ``PLUGIN_PROGRESSIVE_LOADING=false`` 回到全量 eager 装配。

范围：主链路装配（``resolve_progressive_plugins``，可见插件 ∩ enabled 集合，
会话粘滞）+ 子智能体装配（``resolve_bound_progressive_plugins``，按绑定的
install_id 解析；子智能体运行短暂且相互隔离，激活只在本次运行内生效、不落库）。
收窄模式（对话模式）圈定的插件面是管理员的显式圈定，保持 eager；组件含 stdio
transport MCP 的插件不延迟（激活期进程内起子进程的生命周期管理复杂度不值得）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

logger = logging.getLogger(__name__)


def progressive_plugin_loading_enabled() -> bool:
    """Env-driven kill switch (default on)."""
    return os.getenv("PLUGIN_PROGRESSIVE_LOADING", "true").strip().lower() == "true"


@dataclass
class DeferredPlugin:
    """One deferral-eligible plugin and its in-play components."""

    slug: str
    name: str
    description: str
    # Component ids intersected with this run's enabled sets — only what the
    # run would actually have carried is deferred / later activated.
    skill_ids: List[str] = field(default_factory=list)
    mcp_ids: List[str] = field(default_factory=list)
    # MCP servers declared by the plugin's skills via SKILL.md frontmatter
    # (mcp_server_ids). Connected at activation when not already connected —
    # NOT subtracted from the base assembly, since they may be shared with
    # non-plugin skills.
    bound_mcp_ids: List[str] = field(default_factory=list)


@dataclass
class ProgressiveResolution:
    """Outcome of the assembly-time deferral decision."""

    # Plugins actually deferred this run (not activated, not invoked).
    deferred: List[DeferredPlugin] = field(default_factory=list)
    # All deferral-eligible plugins regardless of activation state, sorted by
    # slug. The directory section renders THIS list so the prompt bytes stay
    # identical before and after an activation (prefix-cache friendly).
    directory: List[DeferredPlugin] = field(default_factory=list)
    activated_slugs: List[str] = field(default_factory=list)
    deferred_skill_ids: Set[str] = field(default_factory=set)
    deferred_mcp_ids: Set[str] = field(default_factory=set)

    def deferred_by_slug(self) -> Dict[str, DeferredPlugin]:
        return {p.slug: p for p in self.deferred}


# ── Activation persistence (ChatSession.extra_data) ──────────────────────────

_ACTIVATED_KEY = "activated_plugins"


def load_activated_plugin_slugs(chat_id: Optional[str]) -> List[str]:
    """Read the chat's sticky activation list (activation order preserved)."""
    if not chat_id:
        return []
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatSession

        with SessionLocal() as db:
            row = db.query(ChatSession.extra_data).filter(ChatSession.chat_id == chat_id).first()
            if not row:
                return []
            data = row[0] or {}
            slugs = data.get(_ACTIVATED_KEY) or []
            return [str(s) for s in slugs if isinstance(s, str) and s.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] activated list read failed: %s", exc)
        return []


def record_plugin_activation(chat_id: Optional[str], slugs: Sequence[str]) -> None:
    """Append slugs to the chat's sticky activation list (idempotent)."""
    if not chat_id or not slugs:
        return
    try:
        from sqlalchemy.orm.attributes import flag_modified

        from core.db.engine import SessionLocal
        from core.db.models import ChatSession

        with SessionLocal() as db:
            row = db.query(ChatSession).filter(ChatSession.chat_id == chat_id).first()
            if row is None:
                return
            data = dict(row.extra_data or {})
            current = [s for s in (data.get(_ACTIVATED_KEY) or []) if isinstance(s, str)]
            added = False
            for slug in slugs:
                if slug and slug not in current:
                    current.append(slug)
                    added = True
            if not added:
                return
            data[_ACTIVATED_KEY] = current
            row.extra_data = data
            flag_modified(row, "extra_data")
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] activation persist failed: %s", exc)


# ── Assembly-time resolution ─────────────────────────────────────────────────


def _http_transport(cfg: Any) -> bool:
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get("url")) or cfg.get("transport") in ("streamable_http", "sse")


def resolve_progressive_plugins(
    *,
    user_id: str,
    chat_id: Optional[str],
    enabled_skill_ids: Sequence[str],
    enabled_mcp_ids: Sequence[str],
    invoked_skill_ids: Optional[Sequence[str]] = None,
    invoked_mcp_ids: Optional[Sequence[str]] = None,
) -> ProgressiveResolution:
    """Decide which installed plugins this run defers.

    Runs in a worker thread (sync DB access). A plugin is deferral-eligible
    when it is visible to the user, has at least one component in this run's
    enabled sets, and none of its in-play MCP servers use stdio transport.
    Eligible plugins already activated for this chat — or explicitly invoked
    this turn — stay eager; explicit invocation is persisted as activation so
    the plugin stays loaded on subsequent turns.
    """
    from sqlalchemy import or_

    from core.db.engine import SessionLocal
    from core.db.models import InstalledPlugin

    enabled_skills = {s for s in enabled_skill_ids if isinstance(s, str) and s.strip()}
    enabled_mcps = {m for m in enabled_mcp_ids if isinstance(m, str) and m.strip()}
    invoked = {x for x in (invoked_skill_ids or []) if isinstance(x, str) and x.strip()}
    invoked |= {x for x in (invoked_mcp_ids or []) if isinstance(x, str) and x.strip()}

    # Server transport lookup (global + the user's private servers).
    try:
        from core.services.mcp_service import McpServerConfigService

        svc = McpServerConfigService.get_instance()
        server_cfgs = dict(svc.get_all_servers(enabled_only=True))
        try:
            server_cfgs.update(svc.get_owned_servers(str(user_id), enabled_only=False))
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] server config lookup failed: %s", exc)
        server_cfgs = {}

    # Skill → bound MCP metadata (SKILL.md frontmatter mcp_server_ids).
    skill_meta: Dict[str, Any] = {}
    try:
        from core.agent_skills.loader import get_skill_loader

        skill_meta = get_skill_loader().load_all_metadata() or {}
    except Exception:  # noqa: BLE001
        skill_meta = {}

    activated = load_activated_plugin_slugs(chat_id)
    activated_set = set(activated)

    res = ProgressiveResolution(activated_slugs=list(activated))
    eligible: List[DeferredPlugin] = []
    newly_pinned: List[str] = []
    try:
        with SessionLocal() as db:
            rows = (
                db.query(InstalledPlugin)
                .filter(
                    or_(
                        InstalledPlugin.owner_user_id == user_id,
                        InstalledPlugin.owner_user_id.is_(None),
                    )
                )
                .all()
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] installed plugin load failed: %s", exc)
        return res

    for r in rows:
        cids = r.component_ids or {}
        in_play_skills = [s for s in (cids.get("skills") or []) if s in enabled_skills]
        in_play_mcps = [m for m in (cids.get("mcp") or []) if m in enabled_mcps]
        if not in_play_skills and not in_play_mcps:
            continue
        # stdio-transport components keep the whole plugin eager: spawning
        # per-request subprocesses mid-run has lifecycle costs this v1 skips.
        if any(not _http_transport(server_cfgs.get(m)) for m in in_play_mcps):
            continue
        bound: List[str] = []
        for sid in in_play_skills:
            item = skill_meta.get(sid)
            for server_id in getattr(item, "mcp_server_ids", None) or []:
                if server_id and server_id not in bound and server_id not in in_play_mcps:
                    bound.append(server_id)
        plugin = DeferredPlugin(
            slug=str(r.slug),
            name=str(r.name or r.slug),
            description=str(r.description or ""),
            skill_ids=in_play_skills,
            mcp_ids=in_play_mcps,
            bound_mcp_ids=bound,
        )
        eligible.append(plugin)

        components = set(in_play_skills) | set(in_play_mcps)
        if plugin.slug in activated_set:
            continue
        if invoked & components:
            # Explicit invocation this turn = activation; persist below so the
            # next turn keeps the plugin eager without re-invocation.
            newly_pinned.append(plugin.slug)
            continue
        res.deferred.append(plugin)
        res.deferred_skill_ids.update(in_play_skills)
        res.deferred_mcp_ids.update(in_play_mcps)

    if newly_pinned:
        record_plugin_activation(chat_id, newly_pinned)
        res.activated_slugs.extend(newly_pinned)

    res.directory = sorted(eligible, key=lambda p: p.slug)
    return res


def resolve_bound_progressive_plugins(
    install_ids: Sequence[str],
    *,
    skill_filter: Optional[Any] = None,
) -> ProgressiveResolution:
    """Deferral resolution for a sub-agent's explicitly bound plugins.

    Differences from the main-path resolver: plugins are looked up by
    ``install_id`` (the binding is the grant — no visibility or catalog
    intersection), every eligible plugin is deferred (sub-agent runs are
    short-lived and isolated, so there is no sticky activation state to
    consult), and ``skill_filter`` lets the caller apply the same ownership /
    release-exposure narrowing the eager path would have applied to the
    expanded skill ids. Plugins with stdio-transport MCP components are
    returned via ``directory``-absence: the caller expands them eagerly as
    before (their component ids are simply not in ``deferred_*``).
    """
    from core.db.engine import SessionLocal
    from core.db.models import InstalledPlugin

    res = ProgressiveResolution()
    ids = [i for i in install_ids if isinstance(i, str) and i.strip()]
    if not ids:
        return res

    try:
        from core.services.mcp_service import McpServerConfigService

        svc = McpServerConfigService.get_instance()
        server_cfgs = dict(svc.get_all_servers(enabled_only=True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] server config lookup failed: %s", exc)
        server_cfgs = {}

    skill_meta: Dict[str, Any] = {}
    try:
        from core.agent_skills.loader import get_skill_loader

        skill_meta = get_skill_loader().load_all_metadata() or {}
    except Exception:  # noqa: BLE001
        skill_meta = {}

    try:
        with SessionLocal() as db:
            rows = db.query(InstalledPlugin).filter(InstalledPlugin.install_id.in_(ids)).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[plugin-loader] bound plugin load failed: %s", exc)
        return res

    eligible: List[DeferredPlugin] = []
    for r in rows:
        cids = r.component_ids or {}
        skills = [s for s in (cids.get("skills") or []) if isinstance(s, str) and s.strip()]
        if skill_filter is not None:
            try:
                skills = list(skill_filter(skills))
            except Exception:  # noqa: BLE001
                pass
        mcps = [m for m in (cids.get("mcp") or []) if isinstance(m, str) and m.strip()]
        if not skills and not mcps:
            continue
        if any(not _http_transport(server_cfgs.get(m)) for m in mcps):
            continue
        bound: List[str] = []
        for sid in skills:
            item = skill_meta.get(sid)
            for server_id in getattr(item, "mcp_server_ids", None) or []:
                if server_id and server_id not in bound and server_id not in mcps:
                    bound.append(server_id)
        plugin = DeferredPlugin(
            slug=str(r.slug),
            name=str(r.name or r.slug),
            description=str(r.description or ""),
            skill_ids=skills,
            mcp_ids=mcps,
            bound_mcp_ids=bound,
        )
        eligible.append(plugin)
        res.deferred.append(plugin)
        res.deferred_skill_ids.update(skills)
        res.deferred_mcp_ids.update(mcps)

    res.directory = sorted(eligible, key=lambda p: p.slug)
    return res


# ── Prompt section ───────────────────────────────────────────────────────────


def build_plugin_directory_section(directory: Sequence[DeferredPlugin]) -> str:
    """Render the stable plugin directory prompt section.

    Deliberately independent of activation state: the same user sees the same
    bytes in every chat and on every turn, so activating a plugin does not
    perturb this part of the prefix.
    """
    if not directory:
        return ""
    lines = [
        "## 插件目录（Progressive Plugins）",
        "以下插件为按需加载：**未加载时其技能与工具不在当前列表中**。"
        "当用户请求匹配某插件的描述、且所需能力不在当前工具/技能列表里时，先调用 "
        "`load_plugin` 工具（参数为插件标识）加载它，加载后的下一步即可使用其"
        "工具与技能。已加载过的插件无需重复调用。",
        "",
        "可用插件（`插件标识`：适用场景）：",
    ]
    for p in directory:
        desc = " ".join((p.description or "").split()) or p.name
        label = f"`{p.slug}`" if p.name == p.slug else f"`{p.slug}`（{p.name}）"
        lines.append(f"- {label}：{desc}")
    return "\n".join(lines)


# ── Runtime activation tool ──────────────────────────────────────────────────


def register_load_plugin(
    toolkit: Any,
    deferred_by_slug: Dict[str, DeferredPlugin],
    runtime: Dict[str, Any],
) -> None:
    """Register the ``load_plugin`` activation tool onto the collector.

    ``runtime`` is a mutable holder the factory fills in after the real
    Toolkit / AgentRuntimeState exist:

    - ``toolkit``: the live agentscope Toolkit (basic group is mutated in place;
      AS2 recomputes schemas and skill instructions every ReAct round, so the
      appended clients/loaders take effect on the next round).
    - ``permission_context``: allow_rules are appended for new tool names —
      without this every freshly activated MCP tool would fall back to ASK.
    - ``close_list``: the transient-client list returned to the caller; clients
      connected here are appended so the normal ``close_clients()`` teardown
      covers them.
    - ``connected_keys`` / ``activated_slugs``: dedup state.
    - ``persist`` (default True): False for sub-agent runs — activation stays
      in-run only (isolated short-lived contexts have no sticky state, and
      writing under the parent chat's key would leak the activation into the
      main agent's assembly).
    - ``loader`` / ``chat_id`` / ``user_id`` / ``enabled_kb_ids`` /
      ``channel_origin`` / ``reranker_enabled`` / ``confirm_gate`` /
      ``ontology_runtime``: assembly context replayed at activation.
    """
    from agentscope.message import TextBlock
    from agentscope.tool._response import ToolChunk as ToolResponse

    def _text(msg: str) -> Any:
        return ToolResponse(content=[TextBlock(type="text", text=msg)])

    async def load_plugin(plugin: str) -> Any:
        """加载插件目录中列出的插件，激活其包含的全部工具与技能。

        Args:
            plugin: 插件目录里列出的插件标识（反引号内的 slug），也接受插件名称。
        """
        wanted = (plugin or "").strip().strip("`")
        target: Optional[DeferredPlugin] = None
        for slug, item in deferred_by_slug.items():
            if wanted == slug or wanted == item.name or wanted.lower() == slug.lower():
                target = item
                break
        activated: Set[str] = runtime.setdefault("activated_slugs", set())
        if target is None:
            known = "、".join(f"`{s}`" for s in sorted(deferred_by_slug)) or "（无）"
            if wanted in activated:
                return _text(f"插件「{wanted}」本会话已加载，无需重复调用。")
            return _text(f"未找到插件「{wanted}」。可加载的插件：{known}。")
        if target.slug in activated:
            return _text(f"插件「{target.name}」本会话已加载，无需重复调用。")

        tk = runtime.get("toolkit")
        if tk is None or not getattr(tk, "tool_groups", None):
            return _text("插件加载器尚未就绪，请稍后重试。")
        basic = tk.tool_groups[0]

        connected: Set[str] = runtime.setdefault("connected_keys", set())
        new_tool_names: List[str] = []
        failed_servers: List[str] = []

        # ── MCP servers: connect stateless HTTP clients and append in place ──
        mcp_ids = [m for m in [*target.mcp_ids, *target.bound_mcp_ids] if m not in connected]
        if mcp_ids:
            from core.llm.agent_factory import _inject_runtime_headers
            from core.llm.mcp_confirm import CONFIRM_MCP_SERVERS
            from core.llm.mcp_pool import make_client
            from core.services.mcp_service import McpServerConfigService

            svc = McpServerConfigService.get_instance()
            cfgs = dict(svc.get_all_servers(enabled_only=True))
            try:
                cfgs.update(svc.get_owned_servers(str(runtime.get("user_id") or ""), enabled_only=False))
            except Exception:  # noqa: BLE001
                pass
            wanted_cfgs = {k: v for k, v in cfgs.items() if k in set(mcp_ids)}
            wanted_cfgs = _inject_runtime_headers(
                wanted_cfgs,
                current_user_id=runtime.get("user_id"),
                chat_id=runtime.get("chat_id"),
                enabled_kb_ids=runtime.get("enabled_kb_ids"),
                channel_origin=runtime.get("channel_origin"),
                reranker_enabled=bool(runtime.get("reranker_enabled")),
            )

            async def _connect(key: str, cfg: dict) -> None:
                if not _http_transport(cfg):
                    failed_servers.append(key)
                    return
                try:
                    if runtime.get("confirm_gate") and key in CONFIRM_MCP_SERVERS:
                        from core.llm.mcp_confirm import (
                            confirm_specs_for,
                            make_confirm_gated_client,
                        )

                        client = make_confirm_gated_client(
                            key,
                            cfg,
                            chat_id=runtime.get("chat_id"),
                            specs=confirm_specs_for(key),
                        )
                    else:
                        client = make_client(key, cfg, is_stateful=False)
                    tools = await client.list_tools()
                except BaseException as exc:  # noqa: BLE001 — SSE cleanup may raise CancelledError
                    if isinstance(exc, asyncio.CancelledError):
                        current = asyncio.current_task()
                        if current is not None and getattr(current, "cancelling", lambda: 0)() > 0:
                            raise
                    logger.warning("[plugin-loader] MCP '%s' connect failed: %s", key, exc)
                    failed_servers.append(key)
                    return
                basic.mcps.append(client)
                connected.add(key)
                close_list = runtime.get("close_list")
                if isinstance(close_list, list):
                    close_list.append(client)
                new_tool_names.extend(t.name for t in tools if getattr(t, "name", None))

            for key, cfg in wanted_cfgs.items():
                await _connect(key, cfg)
            for key in mcp_ids:
                if key not in wanted_cfgs and key not in failed_servers:
                    failed_servers.append(key)

        # ── Skills: materialize and append loaders in place ──
        loader = runtime.get("loader")
        skill_lines: List[str] = []
        if loader is not None and target.skill_ids:
            from agentscope.skill import LocalSkillLoader

            try:
                meta = loader.load_all_metadata() or {}
            except Exception:  # noqa: BLE001
                meta = {}
            for sid in target.skill_ids:
                try:
                    d = loader.get_skill_dir(sid)
                except Exception:  # noqa: BLE001
                    d = None
                if not d:
                    continue
                basic.skills_or_loaders.append(LocalSkillLoader(directory=d))
                item = meta.get(sid)
                desc = str(getattr(item, "description", "") or "")
                skill_lines.append(f"- `{sid}`：{desc}" if desc else f"- `{sid}`")
                # Ontology gate sees the activated skill's trusted tags too.
                try:
                    from core.ontology.validator import register_runtime_asset_tags

                    register_runtime_asset_tags(
                        runtime.get("ontology_runtime") or {},
                        kind="skill",
                        asset_id=sid,
                        tags=list(getattr(item, "tags", []) or []),
                    )
                except Exception:  # noqa: BLE001
                    pass

        # ── Permissions: newly activated MCP tools must be pre-allowed ──
        pc = runtime.get("permission_context")
        if pc is not None and new_tool_names:
            try:
                from agentscope.permission import PermissionBehavior, PermissionRule

                for n in new_tool_names:
                    pc.allow_rules.setdefault(n, []).append(
                        PermissionRule(
                            tool_name=n,
                            rule_content="",
                            behavior=PermissionBehavior.ALLOW,
                            source="jx_trusted",
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[plugin-loader] allow_rules append failed: %s", exc)

        activated.add(target.slug)
        if runtime.get("persist", True):
            await asyncio.to_thread(
                record_plugin_activation, runtime.get("chat_id"), [target.slug]
            )

        parts = [f"插件「{target.name}」已加载。"]
        if new_tool_names:
            parts.append("新增工具（下一步即可直接调用）：" + "、".join(f"`{n}`" for n in new_tool_names))
        if skill_lines:
            parts.append(
                "新增技能（使用前必须先用 `view_text_file` 读取 "
                "`/workspace/skills/<技能名>/SKILL.md`）：\n" + "\n".join(skill_lines)
            )
        if failed_servers:
            parts.append("以下 MCP 服务连接失败，其工具本轮不可用：" + "、".join(failed_servers))
        if not new_tool_names and not skill_lines:
            parts.append("该插件本轮没有可加载的组件（可能均已被禁用）。")
        return _text("\n".join(parts))

    toolkit.register_tool_function(load_plugin, namesake_strategy="override")


__all__ = [
    "DeferredPlugin",
    "ProgressiveResolution",
    "build_plugin_directory_section",
    "load_activated_plugin_slugs",
    "progressive_plugin_loading_enabled",
    "record_plugin_activation",
    "register_load_plugin",
    "resolve_bound_progressive_plugins",
    "resolve_progressive_plugins",
]
