"""load_plugin for a task: progressive plugin loading on the agent's own workspace.

The task prompt lists the synced plugins the user still grants (the 插件目录). load_plugin
connects one plugin's MCP servers into the running task through core's activation
(``core.llm.tools.plugin_activation``) and returns the paths of its skills under
/workspace/plugins/<slug>/, which the task reads with pa_read_file or Bash. The activation is
recorded on the task's own chat session, so later runs of the same task connect the plugin
again (see worker/activation.run_context); another task starts without it.

The main conversation has no load_plugin: like its lookup-only MCP servers (front_mcp_ids),
work that needs a plugin is delegated to a task.
"""
from core.db.threads import run_db

from personal_agent.tasks.guards import owned_task
from personal_agent.tools.responses import response


def loaded_plugins(ident, config, chat_id):
    """The plugins of task_plugins that this task's chat session already loaded."""
    from core.capabilities.plugins.runtime.activation_history import load_activated_plugin_slugs
    from personal_agent.identity.capabilities import task_plugins
    loaded = set(load_activated_plugin_slugs(chat_id))
    return [p for p in task_plugins(ident, config) if p.install_id in loaded]


def register_load_plugin(toolkit, request, agent_id, config, task_id, uid):
    """Add load_plugin to a Worker run. config is runtime_config's: only granted plugins load."""
    from core.llm.tools.plugin_activation import register_load_plugin as core_register
    from personal_agent.identity.capabilities import plugin_skill_paths, task_plugins
    plugins = task_plugins(agent_id, config)
    if not plugins or not getattr(request, "run_id", None):
        return
    loaded = loaded_plugins(agent_id, config, request.chat_id)
    # The holder core's activation works on. The factory completes it with the live toolkit,
    # its permission context and the run's client list once the agent exists.
    runtime = {"activated_slugs": {p.slug for p in loaded}, "connected_keys": set(request.enabled_mcp_ids or []),
               "toolkit": None, "permission_context": None, "close_list": None,
               # Skills are workspace files, not catalog loaders: core only connects the MCP servers.
               "loader": None, "persist": True, "chat_id": request.chat_id, "user_id": uid,
               "enabled_kb_ids": [], "channel_origin": None, "reranker_enabled": False,
               # A task is unattended; its own approvals cover what reaches people (tasks/approvals.py).
               "approval_available": False}
    collected = []
    core_register(collected, {p.slug: p for p in plugins}, runtime)
    activate = collected[0]._func
    toolkit.plugin_runtime = runtime

    def tool_names():
        live = runtime.get("toolkit")
        return [t.name for t in live.tool_groups[0].tools] if live is not None and getattr(live, "tool_groups", None) else []

    async def load_plugin(plugin: str):
        """Load one plugin of the 插件目录 for this task: connect its tools and list its skills.
        Read a listed skill's SKILL.md with pa_read_file before you use it.

        Args:
            plugin: the plugin's id in the directory (the slug in backticks), or its name.
        """
        await run_db(owned_task, task_id, uid)
        wanted = (plugin or "").strip().strip("`")
        target = next((p for p in plugins if wanted == p.slug), None) or next(
            (p for p in plugins if wanted.lower() in (p.slug.lower(), p.name.lower())), None)
        before = tool_names()
        try:
            answer = await activate(plugin)
        except RuntimeError as exc:  # an MCP server of the plugin is unreachable: nothing was connected
            return response({"error": str(exc)})
        if target is None or target.slug not in runtime["activated_slugs"]:
            return response({"error": "".join(getattr(b, "text", "") for b in answer.content)})
        skills = await run_db(plugin_skill_paths, agent_id, target)
        return response({"plugin": target.slug, "name": target.name,
                         "tools": [n for n in tool_names() if n not in before],
                         "skills": [{"id": sid, "path": path, "description": text} for sid, path, text in skills],
                         "next": "Its tools are available from your next step. Read a skill's SKILL.md "
                                 "with pa_read_file before following it." if skills else
                                 "Its tools are available from your next step."})

    from core.llm.factory.tools.definitions import function_tool
    toolkit.append(function_tool(load_plugin))
