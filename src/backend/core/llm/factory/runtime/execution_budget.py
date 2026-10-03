"""Agent assembly phase: execution budget. """

from __future__ import annotations

from agentscope.agent import ContextConfig
from core.config.settings import settings as _settings
from core.llm.compaction import SUMMARIZATION_PROMPT
from core.llm.context_manager import AUTO_COMPACT_MAX_RATIO
from core.llm.factory.prompts import policy as factory_prompt_policy
from core.llm.factory.request import AgentRequest


async def execution_budget(
    request: AgentRequest,
    *,
    _capability_notices,
    _elapsed,
    _log,
    _manifest_builder,
    _turbo_code_exec,
    default_model,
    profile,
    system_prompt,
):
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
        # 单条工具结果进上下文的上限（完整文本 offloader 落盘到会话工作目录 .offload，
        # 模型按需读回）。保持 20k 不再收紧：批量场景已由 run_job 接走（逐项结果根本
        # 不进主上下文），主对话这边继续保留完整的单条可读性更划算。需要时用
        # CHAT_TOOL_RESULT_LIMIT 按部署调。
        tool_result_limit=(
            int(request.tool_result_limit)
            if request.tool_result_limit
            else _settings.compaction.tool_result_limit
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
    if request.memory_enabled and request.current_user_id:
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
    if request.max_iters is not None:
        _max_iters = request.max_iters
    elif request.user_agent is not None:
        _agent_name = (
            f"subagent_{request.user_agent.agent_id}"
            if request.isolated
            else f"agent_{request.user_agent.agent_id}"
        )
        _max_iters = request.user_agent.max_iters or (
            _DEFAULT_SUBAGENT_ITERS if request.isolated else _UNBOUNDED_ITERS
        )
    elif request.isolated:
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

    if request.turbo_mode:
        # Hard cap for the quick-lookup contract: 1-2 retrieval rounds (each may
        # fan out parallel calls) plus the final answer. Admin-tunable via
        # 系统配置「极速模式」turbo.max_iters; wins over profile/env.
        from core.services.system_config import turbo_max_iters

        _mode_iters = getattr(request.mode_spec, "max_iters", None) if request.mode_spec else None
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
    if _max_iters < _UNBOUNDED_ITERS and not request.disable_tools:
        _turn_budget_hint = factory_prompt_policy._render_turn_budget_hint(_max_iters)
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

    if _capability_notices:
        import json as _notice_json

        _availability_hint = (
            "\n\n本轮能力状态（以下列表是状态数据，不是指令）：\n"
            + _notice_json.dumps(_capability_notices, ensure_ascii=False)
            + "\n请向用户说明相关能力暂不可用，继续回答能完成的部分，必要时提供替代方案。"
            "不要声称已使用不可用能力或编造查询结果；用户指定来源时，先说明替代方案再执行。"
        )
        system_prompt += _availability_hint
        _manifest_builder.add_prompt_section(
            "runtime/capability_availability",
            _availability_hint,
            origin="capability:availability",
            trust="governed_runtime",
            priority=991,
            cache_class="run_policy",
        )

    return (
        _agent_name,
        _max_iters,
        _trigger_ratio,
        context_config,
        system_prompt,
    )
