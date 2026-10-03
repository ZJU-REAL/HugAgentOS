"""Agent assembly phase: agent construction. """

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
import os

from agentscope.agent import ReActConfig
from agentscope.agent._config import ModelConfig
from core.llm.compacting_agent import CompactingAgent
from core.llm.factory.request import AgentRequest


async def agent_construction(
    request: AgentRequest,
    *,
    _agent_name,
    _agent_ref,
    _api_scope,
    _bind_manifest,
    _compaction_system_prompt,
    _compaction_tool_schemas,
    _desktop_prepared_servers,
    _desktop_progressive,
    _log,
    _max_iters,
    _middlewares,
    _plugin_runtime,
    _prepared_capabilities,
    _sbx_sess,
    _state,
    _trigger_ratio,
    asset_bundle,
    context_config,
    default_model,
    execution_manifest,
    system_prompt,
    toolkit,
):
    try:
        asset_bundle = _bind_manifest(execution_manifest)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[binder] binding skipped: %s", exc)

    # Complete the progressive-plugin runtime holder now that the real Toolkit
    # and the permission context exist (load_plugin mutates both mid-run).
    if _plugin_runtime is not None:
        if _prepared_capabilities is not None:
            _plugin_runtime["prepared_run"] = _prepared_capabilities
            _plugin_runtime["prepared_servers"] = _desktop_prepared_servers
            _plugin_runtime["plugin_directory"] = _desktop_progressive.directory
        _plugin_runtime["toolkit"] = toolkit
        _plugin_runtime["permission_context"] = _state.permission_context

    # Offloader: when compressing/truncating overlong tool results, spill the
    # complete text to the current tool workspace's .offload/ directory. The model
    # can read it back via Read/Bash. Only mounted when sandbox tools are enabled;
    # otherwise the agent has no
    # Read/Bash and spilling is pointless. Uses the same _sbx_sess as Bash/Read.
    _offloader = None
    if not request.disable_tools and os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() == "true":
        try:
            from core.llm.offloader import SandboxOffloader
            from core.sandbox.factory import get_sandbox_provider

            if _api_scope is not None:
                from core.llm.agent_api_tools import _provider as api_sandbox_provider

                _offloader = SandboxOffloader(
                    api_sandbox_provider(),
                    _sbx_sess,
                    user_id=_api_scope.sandbox_user_id,
                )
            else:
                _offloader = SandboxOffloader(
                    get_sandbox_provider(), _sbx_sess, user_id=request.current_user_id
                )
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
    agent._jx_prepared_capabilities = _prepared_capabilities
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

    return (agent,)
