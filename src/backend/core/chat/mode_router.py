"""智能路由（``mode_slug=auto``）：每轮先识别用户意图，再落到一个真实模式上。

这里是领域规则，不碰模型调用：

* 闸门——系统配置总开关 + 用户能力位 ``can_use_intent_router``，每轮在服务端重验；
* 候选集——用户可见的启用模式 ∩ ``intent_router.candidate_slugs``，去掉 ``auto`` 与私有模式；
  本轮有显式呼唤（斜杠技能 / 插件 / 连接器 / @子智能体）时再去掉禁止手动呼唤的模式；
* 当前模式——这段对话上一条助手消息 ``intent_route.mode_slug``；
* 三种策略里调模型之前就能定的结论（识别指令、JSON 容错与结论见 :mod:`core.chat.mode_route_decision`）；
* 识别结果落进运行快照，恢复 / steer / handoff 沿用而不重识别。

真正把识别请求发给模型的是 :mod:`core.agent_runtime.intent_routing`（它要复用主请求的前缀）。
识别失败一律回退，绝不让对话中断。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.chat import mode_types
from core.config.store import intent_router_options as router_options
from core.db.models.chat_mode import TOOL_SCOPE_RESTRICTED

logger = logging.getLogger(__name__)

TOOL_NAME = "intent_recognition"
TOOL_DISPLAY_NAME = "用户意图识别"
CAPABILITY_FLAG = "can_use_intent_router"
ROLE_KEY = "intent_router"
PROMPT_KIND = "intent_router"
#: 识别卡片里展示的问题长度上限。
QUESTION_PREVIEW_CHARS = 200

# result.source
SOURCE_LLM = "llm"
SOURCE_KEPT = "kept"
SOURCE_SINGLE = "single_candidate"
SOURCE_FIRST_TURN_STICKY = "first_turn_sticky"
SOURCE_FALLBACK = "fallback"

# result.fallback_cause
CAUSE_DISABLED = "disabled"
CAUSE_NO_PERMISSION = "no_permission"
CAUSE_NO_CANDIDATES = "no_candidates"
CAUSE_TIMEOUT = "timeout"
CAUSE_PARSE_ERROR = "parse_error"
CAUSE_INVALID_SLUG = "invalid_slug"
CAUSE_TOOL_CALL = "tool_call"
CAUSE_MODEL_ERROR = "model_error"
CAUSE_ROUTER_ERROR = "router_error"

#: 恢复 / steer / handoff 透传的上下文字段。
ROUTED_SLUG_KEY = "routed_mode_slug"
ROUTE_KEY = "intent_route"

#: 显式呼唤的上下文字段：任何一个有值，本轮就不能落到禁止手动呼唤的模式上。
_EXPLICIT_INVOKE_KEYS = (
    "skill_id",
    "skill_ids",
    "plugin_id",
    "plugin_skill_ids",
    "plugin_mcp_ids",
    "connector_id",
    "mcp_ids",
    "mention_agent_id",
    "explicit_subagent_command",
)


@dataclass(frozen=True)
class Candidate:
    slug: str
    name: str
    description: str = ""
    restricted: bool = False
    manual_invoke_enabled: bool = True

    @property
    def rank(self) -> int:
        """能力档：收窄工具面的模式低一档。智能保持只禁止往低档走。"""
        return 0 if self.restricted else 1


@dataclass
class RoutePlan:
    """一轮路由在调模型之前就能定下来的全部事实。"""

    tool_id: str
    question: str
    strategy: str
    candidates: List[Candidate] = field(default_factory=list)
    fallback_slug: str = mode_types.SLUG_STANDARD
    current_slug: Optional[str] = None
    timeout_ms: int = router_options.DEFAULT_TIMEOUT_MS
    #: 不需要问模型时的现成结论（来源 + 回退原因）。
    decided_slug: Optional[str] = None
    decided_source: Optional[str] = None
    decided_cause: Optional[str] = None
    decided_reason: str = ""

    @property
    def needs_model(self) -> bool:
        return self.decided_slug is None

    @property
    def provisional_slug(self) -> str:
        """先按哪个模式装配 agent：已定的结论，否则当前模式，第一轮用回退模式。"""
        return self.decided_slug or self.current_slug or self.fallback_slug

    def candidate(self, slug: Optional[str]) -> Optional[Candidate]:
        return next((c for c in self.candidates if c.slug == slug), None)

    def allowed(self) -> List[Candidate]:
        """模型可以选的模式：智能保持时不低于当前模式那一档。"""
        current = self.candidate(self.current_slug)
        if self.strategy != router_options.STRATEGY_SMART_KEEP or current is None:
            return list(self.candidates)
        return [c for c in self.candidates if c.rank >= current.rank]


# ── 判定 ──────────────────────────────────────────────────────────────────


def is_auto(context: Dict[str, Any]) -> bool:
    return str(context.get("mode_slug") or "").strip() == mode_types.SLUG_AUTO


def has_explicit_invocation(context: Dict[str, Any]) -> bool:
    return any(context.get(key) for key in _EXPLICIT_INVOKE_KEYS)


def question_preview(text: str) -> str:
    return str(text or "")[:QUESTION_PREVIEW_CHARS]


def user_allowed(db: Session, user_id: str) -> bool:
    from core.auth.capabilities import resolve_user_capabilities

    return bool(resolve_user_capabilities(db, str(user_id)).get(CAPABILITY_FLAG))


def load_candidates(
    db: Session,
    user_id: Optional[str],
    options: router_options.IntentRouterOptions,
    *,
    explicit_invocation: bool,
) -> List[Candidate]:
    from core.chat.modes import ChatModeService

    wanted = set(options.candidate_slugs)
    out: List[Candidate] = []
    for row in ChatModeService(db).list_for_user(user_id):
        if row.owner_user_id is not None or row.slug == mode_types.SLUG_AUTO:
            continue
        if wanted and row.slug not in wanted:
            continue
        if explicit_invocation and not bool(row.manual_invoke_enabled):
            # mode_bindings 会拒绝在这种模式里执行显式呼唤；别把用户路由进死胡同。
            continue
        if any(c.slug == row.slug for c in out):
            continue
        out.append(
            Candidate(
                slug=row.slug,
                name=row.name,
                description=row.description or "",
                restricted=row.tool_scope == TOOL_SCOPE_RESTRICTED,
                manual_invoke_enabled=bool(row.manual_invoke_enabled),
            )
        )
    return out


def current_mode_slug(db: Session, chat_id: Optional[str], context: Dict[str, Any]) -> Optional[str]:
    """这段对话上一条助手消息记下的路由结果；第一轮（或上一条不是路由出来的）返回 None。"""
    if not chat_id:
        return None
    from core.db.models import ChatMessage

    current_message = str(context.get("message_id") or "")
    current_run = str(context.get("run_id") or "")
    rows = (
        db.query(ChatMessage.message_id, ChatMessage.extra_data)
        .filter(ChatMessage.chat_id == chat_id, ChatMessage.role == "assistant")
        .order_by(ChatMessage.chat_seq.desc())
        .limit(8)
        .all()
    )
    for message_id, extra in rows:
        extra = extra if isinstance(extra, dict) else {}
        if message_id == current_message or (current_run and extra.get("run_id") == current_run):
            continue
        route = extra.get(ROUTE_KEY)
        slug = route.get("mode_slug") if isinstance(route, dict) else None
        return str(slug) if slug else None
    return None


def _effective_fallback(options, candidates: List[Candidate]) -> str:
    slugs = [c.slug for c in candidates]
    if not slugs or options.fallback_slug in slugs:
        return options.fallback_slug
    if mode_types.SLUG_STANDARD in slugs:
        return mode_types.SLUG_STANDARD
    return slugs[0]


def plan_route(db: Session, context: Dict[str, Any], question: str) -> RoutePlan:
    """一轮路由的同步部分（读配置、校权限、取候选、定当前模式）。

    这里定得下来的结论（关闭 / 无权限 / 单候选 / 第一轮沿用 / 智能保持无处可升）直接写进
    ``decided_*``，调用方据此决定还要不要问模型。
    """
    options = router_options.intent_router_options()
    user_id = str(context.get("user_id") or "") or None
    plan = RoutePlan(
        tool_id=f"intent_{uuid.uuid4().hex}",
        question=question_preview(question),
        strategy=options.strategy,
        fallback_slug=options.fallback_slug,
        timeout_ms=options.timeout_ms,
    )
    explicit = has_explicit_invocation(context)
    try:
        plan.candidates = load_candidates(db, user_id, options, explicit_invocation=explicit)
    except Exception:  # noqa: BLE001 — 候选读不到就按回退走
        logger.warning("[intent_router] 读取候选模式失败", exc_info=True)
        plan.candidates = []
    plan.fallback_slug = _effective_fallback(options, plan.candidates)

    def decide(slug: str, source: str, cause: Optional[str] = None, reason: str = "") -> RoutePlan:
        plan.decided_slug, plan.decided_source, plan.decided_cause = slug, source, cause
        plan.decided_reason = reason
        return plan

    if not options.enabled:
        return decide(plan.fallback_slug, SOURCE_FALLBACK, CAUSE_DISABLED, "智能路由未开启")
    if not user_id or not user_allowed(db, user_id):
        return decide(plan.fallback_slug, SOURCE_FALLBACK, CAUSE_NO_PERMISSION, "没有智能路由权限")
    if not plan.candidates:
        return decide(plan.fallback_slug, SOURCE_FALLBACK, CAUSE_NO_CANDIDATES, "没有可选的模式")

    current = current_mode_slug(db, context.get("chat_id"), context)
    plan.current_slug = current if plan.candidate(current) is not None else None
    if len(plan.candidates) == 1:
        return decide(plan.candidates[0].slug, SOURCE_SINGLE, reason="只有一个可选模式")
    if plan.strategy == router_options.STRATEGY_FIRST_TURN and plan.current_slug:
        return decide(plan.current_slug, SOURCE_FIRST_TURN_STICKY, reason="沿用首轮识别的模式")
    if plan.current_slug and len(plan.allowed()) == 1:
        return decide(plan.current_slug, SOURCE_KEPT, reason="当前模式已是能力最完整的模式")
    return plan


# ── 流事件 ────────────────────────────────────────────────────────────────


def tool_call_event(tool_id: str, question: str) -> Dict[str, Any]:
    args = {"question": question_preview(question)}
    return {
        "type": "tool_call",
        "tool_name": TOOL_NAME,
        "tool_display_name": TOOL_DISPLAY_NAME,
        "tool_args": args,
        "input": dict(args),
        "tool_id": tool_id,
    }


def tool_result_event(tool_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_name": TOOL_NAME,
        "tool_id": tool_id,
        "status": "success",
        "result": dict(result),
    }


def apply_to_context(context: Dict[str, Any], result: Dict[str, Any], tool_id: str) -> None:
    context[ROUTED_SLUG_KEY] = result["mode_slug"]
    context[ROUTE_KEY] = {**result, "tool_id": tool_id}


def stored_route(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """恢复 / steer / handoff 带进来的既有路由结论。"""
    slug = str(context.get(ROUTED_SLUG_KEY) or "").strip()
    if not slug:
        return None
    route = context.get(ROUTE_KEY)
    route = dict(route) if isinstance(route, dict) else {}
    route.setdefault("mode_slug", slug)
    return route


def public_result(route: Dict[str, Any]) -> Dict[str, Any]:
    """工具卡 / 消息元数据里的 result（去掉内部 tool_id）。"""
    return {k: v for k, v in route.items() if k != "tool_id"}


def route_metadata(context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """助手消息元数据 ``intent_route``（与识别卡片 result 相同）；非智能路由轮次返回 None。"""
    route = (context or {}).get(ROUTE_KEY)
    return public_result(route) if isinstance(route, dict) else None


# ── 持久化 ────────────────────────────────────────────────────────────────


def persist_route(run_id: str, owner: str, result: Dict[str, Any], tool_id: str) -> None:
    """把路由结论写进运行快照与请求载荷，让恢复沿用、任务监控能读到。

    走 journal 的租约校验：被别的 worker 接管时写不进去，也不该写。
    """
    if not run_id or not owner:
        return
    from core.db.models import ChatRun
    from core.runs import ownership

    route = {**result, "tool_id": tool_id}

    def _commit(db: Session) -> None:
        run = db.get(ChatRun, run_id)
        if run is None:
            return
        snapshot = dict(run.recovery_snapshot or {})
        worker_args = dict(snapshot.get("worker_args") or {})
        ctx = dict(worker_args.get("context") or {})
        ctx[ROUTED_SLUG_KEY] = result["mode_slug"]
        ctx[ROUTE_KEY] = route
        worker_args["context"] = ctx
        snapshot["worker_args"] = worker_args
        run.recovery_snapshot = snapshot
        payload = dict(run.request_payload or {}) if isinstance(run.request_payload, dict) else {}
        payload[ROUTED_SLUG_KEY] = result["mode_slug"]
        run.request_payload = payload

    ownership.journal().append_operation(
        run_id,
        owner=owner,
        operation_type="intent_routed",
        phase="pre_model",
        safety="replayable",
        payload={"mode_slug": result["mode_slug"], "source": result.get("source")},
        commit_effect=_commit,
    )


__all__ = [
    "Candidate",
    "RoutePlan",
    "apply_to_context",
    "is_auto",
    "persist_route",
    "plan_route",
    "public_result",
    "route_metadata",
    "stored_route",
    "tool_call_event",
    "tool_result_event",
]
