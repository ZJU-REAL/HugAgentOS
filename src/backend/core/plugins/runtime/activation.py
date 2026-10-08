"""Progressive plugin runtime: activation."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)


from core.plugins.runtime import activation_history as plugin_activation_history
from core.plugins.runtime import models as plugin_runtime_models
from core.plugins.runtime import resolution as plugin_resolution


class _ActivationStage:
    """Collect one activation's clients and loaders, then commit or drop them together.

    A device capability run must never expose half a plugin: if the authorized
    closure stops holding — or an MCP turns out to be unreachable — after some
    clients already connected, the run has to end up exactly as it started, so
    everything lands in a buffer that is published only on success. The legacy
    cloud path has no such checkpoint and keeps appending straight to the live
    group.
    """

    def __init__(self, live_group: Any, runtime: Dict[str, Any], prepared: Any) -> None:
        from types import SimpleNamespace

        self._live = live_group
        self._runtime = runtime
        self._prepared = prepared
        self._staged_clients: List[Any] = []
        self.staged = prepared is not None
        self.group = SimpleNamespace(mcps=[], skills_or_loaders=[]) if self.staged else live_group
        current = runtime.setdefault("connected_keys", set())
        self.connected: Set[str] = set(current) if self.staged else current

    async def checkpoint(self) -> None:
        """Re-assert the authorized closure between steps of the activation."""
        if self._prepared is None:
            return
        from core.capabilities.runtime import validate

        await asyncio.to_thread(validate, self._prepared)

    def add_client(self, key: str, client: Any) -> None:
        self.group.mcps.append(client)
        self.connected.add(key)
        if self.staged:
            self._staged_clients.append(client)
            return
        close_list = self._runtime.get("close_list")
        if isinstance(close_list, list):
            close_list.append(client)

    async def discard(self) -> None:
        for client in self._staged_clients:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001 — one bad close must not leak the rest
                logger.warning("[plugin-loader] staged MCP close failed: %s", exc)
        self._staged_clients.clear()

    def commit(self) -> None:
        if not self.staged:
            return
        self._live.mcps.extend(self.group.mcps)
        self._live.skills_or_loaders.extend(self.group.skills_or_loaders)
        self._runtime["connected_keys"] = self.connected
        close_list = self._runtime.get("close_list")
        if isinstance(close_list, list):
            close_list.extend(self._staged_clients)
        self._staged_clients.clear()


@asynccontextmanager
async def _activation_stage(live_group: Any, runtime: Dict[str, Any], prepared: Any):
    stage = _ActivationStage(live_group, runtime, prepared)
    try:
        yield stage
    except BaseException:
        await stage.discard()
        raise
    stage.commit()


def register_load_plugin(
    toolkit: Any,
    deferred_by_slug: Dict[str, plugin_runtime_models.DeferredPlugin],
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
      ``channel_origin`` / ``reranker_enabled`` / ``approval_available`` /
      ``ontology_runtime``: assembly context replayed at activation.
    MCP tools are trusted and therefore never added to the built-in-tool
    permission registry during progressive activation.
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
        matches = [
            item
            for slug, item in deferred_by_slug.items()
            if wanted == slug or wanted == item.name or wanted.lower() == slug.lower()
        ]
        exact = [item for slug, item in deferred_by_slug.items() if wanted == slug]
        if runtime.get("prepared_run") is not None and not exact:
            directory_matches = [
                item
                for item in runtime.get("plugin_directory", deferred_by_slug.values())
                if wanted == item.name or wanted.lower() == item.slug.lower()
            ]
            if len(directory_matches) > 1:
                return _text(
                    "插件名称有多个来源，请使用目录中的完整标识："
                    + "、".join(item.slug for item in directory_matches)
                )
        if runtime.get("prepared_run") is not None and len(matches) > 1 and len(exact) != 1:
            return _text(
                "插件名称有多个来源，请使用目录中的完整标识："
                + "、".join(item.slug for item in matches)
            )
        target = exact[0] if exact else (matches[0] if matches else None)
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

        prepared = runtime.get("prepared_run")
        if prepared is not None:
            from core.capabilities.runtime import validate, validate_activation

            await asyncio.to_thread(
                validate, prepared, user_id=runtime.get("user_id", prepared.user_id)
            )
            # 展开这个插件的技能之前，按推迟时记下的身份复查它自己。
            await asyncio.to_thread(
                validate_activation,
                prepared,
                target.capability_nodes,
                available_mcp=set(runtime.get("prepared_servers") or {}),
            )

        new_tool_names: List[str] = []
        failed_servers: List[str] = []
        skill_lines: List[str] = []

        async with _activation_stage(tk.tool_groups[0], runtime, prepared) as stage:
            # ── MCP servers: connect stateless HTTP clients and append in place ──
            mcp_ids = [
                m for m in [*target.mcp_ids, *target.bound_mcp_ids] if m not in stage.connected
            ]
            if mcp_ids:
                from core.llm.factory.tools.mcp_config import _inject_runtime_headers
                from core.llm.mcp_pool import make_client
                from core.services.mcp_service import McpServerConfigService

                if prepared is not None:
                    cfgs = runtime["prepared_servers"]
                else:
                    svc = McpServerConfigService.get_instance()
                    cfgs = dict(svc.get_all_servers(enabled_only=True))
                    try:
                        cfgs.update(
                            svc.get_owned_servers(
                                str(runtime.get("user_id") or ""), enabled_only=False
                            )
                        )
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
                    if not stage.staged and not plugin_resolution._http_transport(cfg):
                        failed_servers.append(key)
                        return
                    client = None
                    try:
                        from orchestration.local_plugin_sidecars import ensure_native_servers

                        await stage.checkpoint()
                        await ensure_native_servers({key: cfg})
                        client = make_client(key, cfg, is_stateful=False)
                        if stage.staged and not plugin_resolution._http_transport(cfg):
                            await client.connect()
                        tools = await client.list_tools()
                        await stage.checkpoint()
                    except (
                        BaseException
                    ) as exc:  # noqa: BLE001 — SSE cleanup may raise CancelledError
                        if client is not None:
                            await client.close()
                        if isinstance(exc, asyncio.CancelledError):
                            current = asyncio.current_task()
                            if (
                                current is not None
                                and getattr(current, "cancelling", lambda: 0)() > 0
                            ):
                                raise
                        logger.warning("[plugin-loader] MCP '%s' connect failed: %s", key, exc)
                        failed_servers.append(key)
                        return
                    stage.add_client(key, client)
                    new_tool_names.extend(t.name for t in tools if getattr(t, "name", None))

                for key, cfg in wanted_cfgs.items():
                    await _connect(key, cfg)
                for key in mcp_ids:
                    if key not in wanted_cfgs and key not in failed_servers:
                        failed_servers.append(key)

            await stage.checkpoint()
            if stage.staged and failed_servers:
                raise RuntimeError("插件 MCP 服务不可用：" + "、".join(failed_servers))

            # ── Skills: materialize and append loaders in place ──
            loader = runtime.get("loader")
            if loader is not None and target.skill_ids:

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
                    if prepared is not None:
                        from core.llm.tool_collector import RuntimeNamedSkillLoader

                        stage.group.skills_or_loaders.append(
                            RuntimeNamedSkillLoader(d, sid, prepared)
                        )
                    else:
                        from core.llm.tool_collector import RuntimeNamedSkillLoader

                        stage.group.skills_or_loaders.append(RuntimeNamedSkillLoader(d, sid))
                    item = meta.get(sid)
                    desc = str(getattr(item, "description", "") or "")
                    from core.agent_skills.config import model_facing_skill_dir

                    skill_path = model_facing_skill_dir(sid, d)
                    skill_lines.append(f"- `{sid}`：`{skill_path}/SKILL.md` — {desc}")
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

            await stage.checkpoint()

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
                plugin_activation_history.record_plugin_activation,
                runtime.get("chat_id"),
                [target.install_id],
            )

        # Tool/skill enumeration is shared with the execution manifest. The
        # next ReAct request must publish a new explicit surface generation,
        # rather than silently mixing the old manifest with newly loaded tools.
        invalidate_surface = getattr(tk, "invalidate_execution_surface", None)
        if invalidate_surface is not None:
            invalidate_surface()

        parts = [f"插件「{target.name}」已加载。"]
        if new_tool_names:
            parts.append(
                "新增工具（下一步即可直接调用）：" + "、".join(f"`{n}`" for n in new_tool_names)
            )
        if skill_lines:
            parts.append(
                "新增技能（使用前必须先用 `view_text_file` 读取所列说明文件）：\n"
                + "\n".join(skill_lines)
            )
        if failed_servers:
            parts.append("以下 MCP 服务连接失败，其工具本轮不可用：" + "、".join(failed_servers))
        if not new_tool_names and not skill_lines:
            parts.append("该插件本轮没有可加载的组件（可能均已被禁用）。")
        return _text("\n".join(parts))

    toolkit.register_tool_function(load_plugin, namesake_strategy="override")
