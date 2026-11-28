"""评价判定（evaluation_gate）的领域规则：判定输入、输出解析、结果存储与读取。

每轮回答完成后，后台问一次轻量模型「这条回答要不要请用户评价」（调用本身在
:mod:`core.agent_runtime.evaluation_gating`）。结果写在助手消息的
``extra_data.evaluation_gate``::

    {"need_evaluation": bool, "reason": str, "source": "model" | "fallback",
     "cause": str | None, "latency_ms": int, "decided_at": iso}

读取侧全部按需推导，判定出结果之前什么都不写：

- ``not_needed``：可评价、且存着 ``need_evaluation == false`` 的回答——不显示评价卡、不提醒、
  不进抽检池（用户仍可主动评价）。
- ``gating``：完成本轮时打上了「判定中」标记（:data:`PENDING_KEY`，只在真的派发判定时写）、
  还没有判定结果、交付时间在 ``timeout_ms + 10s`` 之内的回答——前端先不显示评价卡，等结果。
  超出这个时间还没有结果（进程重启、任务丢失）就按需要评价处理。

总开关关闭时，两份列表都是空的，存着的判定结果一律忽略，行为与没有这个功能时完全一致。
原则是「宁可多问一次」：超时、出错、输出解析不了、模型想调工具，都按需要评价处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.chat import evaluation
from core.db.models import ChatMessage, ChatRun, MessageFeedback
from core.infra.model_output import parse_strict_json
from core.infra.time import as_utc, utc_now
from core.llm.runtime.one_shot import (  # noqa: F401 — the gate's causes and outcome are the shared ones
    CAUSE_MODEL_ERROR,
    CAUSE_TIMEOUT,
    CAUSE_TOOL_CALL,
    ModelOutcome,
)

STORAGE_KEY = "evaluation_gate"
#: 完成本轮时写在消息上的「判定中」标记：只有带它的回答才会出现在 ``gating`` 里。
PENDING_KEY = "evaluation_gate_pending"
PROMPT_KIND = "evaluation_gate"
ROLE_KEY = "evaluation_gate"
USAGE_SOURCE = "evaluation_gate"

SOURCE_MODEL = "model"
SOURCE_FALLBACK = "fallback"

CAUSE_PARSE_ERROR = "parse_error"

REASON_MAX = 20
#: 判定结果在交付后这么久之内还可能落库（超时之外再留的余量，覆盖写库与调度延迟）。
GATING_GRACE_SECONDS = 10

# 判定输入的长度上限（字符）。判定只需要看清这一轮在做什么、之前问过什么，
# 不需要完整原文；最新这条回答多留一些。
_HISTORY_MESSAGES = 30
_MESSAGE_CHARS = 800
_TARGET_CHARS = 6000
_FALLBACK_REASONS = {
    CAUSE_TIMEOUT: "判定超时",
    CAUSE_MODEL_ERROR: "判定出错",
    CAUSE_PARSE_ERROR: "判定结果无法解析",
    CAUSE_TOOL_CALL: "判定结果无效",
}


# ── 结果 ──────────────────────────────────────────────────────────────────


def _clip_reason(text: Any) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:REASON_MAX]


def result(need_evaluation: bool, reason: str, *, source: str, cause: Optional[str],
           latency_ms: int, decided_at: Optional[datetime] = None) -> Dict[str, Any]:
    """存进 ``extra_data.evaluation_gate`` 的结果。"""
    return {
        "need_evaluation": bool(need_evaluation),
        "reason": _clip_reason(reason),
        "source": source,
        "cause": cause,
        "latency_ms": max(0, int(latency_ms or 0)),
        "decided_at": as_utc(decided_at or utc_now()).isoformat(),
    }


def fallback(cause: str, latency_ms: int = 0) -> Dict[str, Any]:
    """拿不到可信结论时的结果：需要评价。"""
    return result(True, _FALLBACK_REASONS.get(cause, "判定失败"), source=SOURCE_FALLBACK,
                  cause=cause, latency_ms=latency_ms)


class DecisionParseError(ValueError):
    pass


_TRUE = {"true", "yes", "y", "1", "是", "需要", "需要评价"}
_FALSE = {"false", "no", "n", "0", "否", "不需要", "无需评价", "不需要评价"}


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def parse_decision(text: str) -> Dict[str, Any]:
    """宽松解析模型输出：去掉思考块与代码围栏，取第一个 JSON 对象里的 ``need_evaluation``。"""
    payload = parse_strict_json(str(text or ""))
    if not isinstance(payload, dict):
        raise DecisionParseError("model output is not a JSON object")
    need = _as_bool(payload.get("need_evaluation", payload.get("need")))
    if need is None:
        raise DecisionParseError("need_evaluation missing or not a boolean")
    return {"need_evaluation": need, "reason": _clip_reason(payload.get("reason"))}


def resolve_outcome(outcome: ModelOutcome) -> Dict[str, Any]:
    """模型调用结果 → 存储结果；任何失败都回退成需要评价。"""
    if outcome.error:
        return fallback(outcome.error, outcome.latency_ms)
    try:
        decided = parse_decision(outcome.text)
    except DecisionParseError:
        return fallback(CAUSE_PARSE_ERROR, outcome.latency_ms)
    return result(decided["need_evaluation"], decided["reason"], source=SOURCE_MODEL, cause=None,
                  latency_ms=outcome.latency_ms)


def stored_result(gate: Any) -> Optional[Dict[str, Any]]:
    """取出来的 ``extra_data.evaluation_gate``：形状对就是判定结果，否则 ``None``。"""
    if not isinstance(gate, dict) or not isinstance(gate.get("need_evaluation"), bool):
        return None
    return gate


# ── 判定输入 ──────────────────────────────────────────────────────────────


@dataclass
class TranscriptEntry:
    role: str
    content: str
    message_id: str
    #: 助手回答的已有结论：用户评价（满意 / 需修改 / 不满意）或判定结果，供识别重复问题。
    note: str = ""
    is_target: bool = False


def _clip(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…（已截断）"


def _note(verdict: Optional[str], gate: Optional[Dict[str, Any]]) -> str:
    if verdict:
        return f"用户已评价：{evaluation.VERDICT_LABELS.get(verdict, verdict)}"
    if gate is not None and gate.get("source") == SOURCE_MODEL:
        return "已判定：需要评价" if gate["need_evaluation"] else "已判定：无需评价"
    return ""


def load_transcript(db: Session, chat_id: str, message_id: str) -> List[TranscriptEntry]:
    """本会话截至目标回答的最近对话（不含工具过程），旧回答带上已评价 / 已判定的结论。

    只读每条消息的开头（正文可达 10 万字）；目标回答本身多读一些。目标回答还不存在时返回空。
    """
    target = db.query(ChatMessage.chat_seq).filter(
        ChatMessage.message_id == message_id, ChatMessage.chat_id == chat_id
    ).first()
    if target is None:
        return []
    head = func.substr(ChatMessage.content, 1, _TARGET_CHARS + 1)
    rows = (
        db.query(ChatMessage.message_id, ChatMessage.role, head,
                 ChatMessage.extra_data[STORAGE_KEY])
        .filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.role.in_(("user", "assistant")),
            ChatMessage.chat_seq <= target.chat_seq,
        )
        .order_by(ChatMessage.chat_seq.desc())
        .limit(_HISTORY_MESSAGES)
        .all()
    )
    ids = [mid for mid, role, *_ in rows if role == "assistant"]
    verdicts = {
        mid: evaluation.effective_verdict(rating, verdict)
        for mid, rating, verdict in db.query(
            MessageFeedback.message_id, MessageFeedback.rating, MessageFeedback.verdict
        ).filter(MessageFeedback.message_id.in_(ids))
    } if ids else {}
    entries: List[TranscriptEntry] = []
    for mid, role, content, gate in reversed(rows):
        is_target = mid == message_id
        entries.append(TranscriptEntry(
            role=role,
            content=_clip(content, _TARGET_CHARS if is_target else _MESSAGE_CHARS),
            message_id=mid,
            note="" if is_target or role != "assistant" else _note(verdicts.get(mid),
                                                                   stored_result(gate)),
            is_target=is_target,
        ))
    return entries


def render_entry(index: int, entry: TranscriptEntry) -> str:
    if entry.role == "user":
        return f"[第 {index} 轮 · 用户]\n{entry.content}"
    label = "助手 · 最新这条回答（待判定）" if entry.is_target else "助手"
    note = f"（{entry.note}）" if entry.note else ""
    return f"[第 {index} 轮 · {label}]{note}\n{entry.content}"


def render_transcript(entries: Iterable[TranscriptEntry]) -> List[str]:
    """每条消息一段；同一轮的问答共用轮次号。"""
    blocks: List[str] = []
    index = 0
    for entry in entries:
        if entry.role == "user" or index == 0:
            index += 1
        blocks.append(render_entry(index, entry))
    return blocks


def build_instruction(principles: str) -> str:
    """系统提示：管理员维护的判断原则 + 运行时追加的输出格式。"""
    lines = []
    if principles.strip():
        lines += [principles.strip(), ""]
    lines += [
        "## 输出",
        "对话记录里标注了「用户已评价」「已判定」的旧回答：最新回答与它们是同一个问题、"
        "结论没有实质变化时，判定为不需要评价。",
        "不要回答用户的问题，不要调用任何工具。只输出一个 JSON 对象："
        '{"need_evaluation": true 或 false, "reason": "20 字以内"}',
    ]
    return "\n".join(lines)


def build_prompt(blocks: List[str]) -> str:
    return (
        "<conversation>\n" + "\n\n".join(blocks) + "\n</conversation>\n\n"
        "判断用户是否需要评价上面标注为「最新这条回答（待判定）」的回答，只输出 JSON。"
    )


# ── 派发与读取侧：哪些回答会判定、哪些无需评价、哪些还在判定 ────────────────


def gate_expected(db: Session, run_id: str) -> bool:
    """这次运行完成时是否派发判定：开关开着且运行可评价。

    在完成本轮的同一次写入里调用（``completion.complete_turn``），结论同时决定是否写
    :data:`PENDING_KEY` 标记与是否派发，读取侧的 ``gating`` 只认这个标记——计划执行、
    计划生成、快照恢复、人工压缩这些不走这条路径的完成，天然不会出现在 ``gating`` 里。
    这里出错一律按不判定处理，绝不能让完成本轮的写入失败。
    """
    from core.config.store import evaluation_gate_options as gate_options

    try:
        if not gate_options.evaluation_gate_options().enabled:
            return False
        return db.query(ChatRun.run_id).filter(
            ChatRun.run_id == run_id, evaluation.evaluable_run_filter()
        ).first() is not None
    except Exception:  # noqa: BLE001
        return False


@dataclass
class GateState:
    """一条回答的判定状态：存着的结果，或「判定中」标记。"""

    result: Optional[Dict[str, Any]]
    pending: bool

    @property
    def not_needed(self) -> bool:
        return self.result is not None and self.result["need_evaluation"] is False

    def gating(self, status: Optional[str], delivered_at: Optional[datetime],
               window_start: datetime) -> bool:
        """带标记、还没结果、已完成且交付时间在等待窗口内。"""
        return (
            self.result is None and self.pending and status == "completed"
            and delivered_at is not None and as_utc(delivered_at) >= window_start
        )


def gate_state(gate: Any, pending: Any) -> GateState:
    # SQLite's JSON extraction hands a JSON ``true`` back as ``1``; PostgreSQL as ``True``.
    flag = pending is True or (type(pending) is int and pending == 1)
    return GateState(result=stored_result(gate), pending=flag)


def gating_window_start(timeout_ms: int, now: Optional[datetime] = None) -> datetime:
    return (now or utc_now()) - timedelta(milliseconds=timeout_ms, seconds=GATING_GRACE_SECONDS)


def _gates(db: Session, message_ids: Iterable[str]) -> Dict[str, GateState]:
    ids = list({mid for mid in message_ids if mid})
    if not ids:
        return {}
    return {
        mid: gate_state(gate, pending)
        for mid, gate, pending in db.query(
            ChatMessage.message_id, ChatMessage.extra_data[STORAGE_KEY],
            ChatMessage.extra_data[PENDING_KEY],
        ).filter(ChatMessage.message_id.in_(ids))
    }


def not_needed_ids(db: Session, message_ids: Iterable[str]) -> set[str]:
    """``message_ids`` 里判定为无需评价的回答（不看开关，调用方负责）。"""
    return {mid for mid, state in _gates(db, message_ids).items() if state.not_needed}


def chat_gate_lists(db: Session, chat_id: str, *, enabled: bool, timeout_ms: int,
                    excluded: Iterable[str] = (),
                    now: Optional[datetime] = None) -> Dict[str, List[str]]:
    """一个会话的 ``not_needed`` 与 ``gating`` 列表；开关关闭时都是空的。

    ``excluded``（定时 / 循环任务的回答，不参与评价）里的回答不会出现在两份列表里，
    三份列表互不重叠，前端按各自含义处理即可。
    """
    if not enabled:
        return {"not_needed": [], "gating": []}
    window_start = gating_window_start(timeout_ms, now)
    delivered = func.coalesce(ChatRun.completed_at, ChatRun.updated_at)
    runs = db.query(ChatRun.message_id, ChatRun.status, delivered).filter(
        ChatRun.chat_id == chat_id, ChatRun.message_id.is_not(None),
        evaluation.evaluable_run_filter(),
    ).all()
    skip = set(excluded)
    latest: Dict[str, tuple] = {}
    for mid, status, at in runs:
        if mid in skip:
            continue
        # 重跑会复用同一条消息；以最近交付的那次运行为准。
        prev = latest.get(mid)
        if prev is None or (at is not None and (prev[1] is None or as_utc(at) > as_utc(prev[1]))):
            latest[mid] = (status, at)
    states = _gates(db, latest)
    not_needed_list, gating = [], []
    for mid, (status, at) in latest.items():
        state = states.get(mid)
        if state is None:  # 回答已不存在（编辑后重发会删掉旧回答）
            continue
        if state.not_needed:
            not_needed_list.append(mid)
        elif state.gating(status, at, window_start):
            gating.append(mid)
    return {"not_needed": sorted(not_needed_list), "gating": sorted(gating)}


def latest_run_is(db: Session, message_id: str, run_id: str) -> bool:
    """``run_id`` 是否仍是这条消息最近的一次运行（重跑开始后旧判定不能写到新回答上）。"""
    row = (
        db.query(ChatRun.run_id)
        .filter(ChatRun.message_id == message_id)
        .order_by(ChatRun.created_at.desc(), ChatRun.run_id.desc())
        .first()
    )
    return row is not None and row[0] == run_id
