"""Suspended, model-initiated questions answered by the chat user.

``ask`` registers a process-local pending request and awaits its ``Event``.
The streaming layer drains the per-chat UI queue, while the out-of-band chat
API validates an answer and wakes the exact same tool coroutine. No synthetic
continuation run and no model-visible tool result exist until the human answers.

The registry is intentionally process-local, matching the application's
current single-worker confirmation implementation. A process restart cancels
the owning ChatRun; pending requests are not advertised as resumable work.
"""

from __future__ import annotations

import asyncio
import copy
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from core.llm.human_interaction import MAX_WAIT_SECONDS

KIND_USER_QUESTION = "user_question"
STATUS_BLOCKED = "blocked_non_interactive"

_MAX_ID_LEN = 64
_MAX_CUSTOM_LEN = 2000
# One request is a single round of "a few short questions" (see the tool
# guidance in the system prompt). The cap lives here, next to the only
# validator both the model request and the browser answer pass through, so
# the two sides can never disagree on how many questions a request holds.
MAX_QUESTIONS = 8
_STATE_IDLE_TTL_SECONDS = 1800
_RECOMMENDED_SUFFIX = re.compile(
    r"\s*(?:\((?:recommended|推荐)\)|（(?:recommended|推荐)）)\s*$",
    re.IGNORECASE,
)


class UserQuestionValidationError(ValueError):
    """The model request or browser answer violates the public contract."""


@dataclass
class _PendingQuestion:
    request_id: str
    questions: List[Dict[str, Any]]
    created_at: float
    expires_at: float
    event: asyncio.Event
    outcome: Optional[str] = None
    answers: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class _ChatQuestions:
    pending: Dict[str, _PendingQuestion] = field(default_factory=dict)
    ui_signals: Optional[asyncio.Queue] = None
    last_ts: float = field(default_factory=time.monotonic)


_LOCK = threading.RLock()
_CHATS: Dict[str, _ChatQuestions] = {}


def _text(
    value: Any,
    *,
    field_name: str,
    limit: Optional[int] = None,
    required: bool = False,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise UserQuestionValidationError(f"{field_name} 不能为空")
    if limit is not None and len(text) > limit:
        raise UserQuestionValidationError(f"{field_name} 不能超过 {limit} 个字符")
    return text


def _as_dict(value: Any, *, field_name: str) -> Dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    if not isinstance(value, dict):
        raise UserQuestionValidationError(f"{field_name} 必须是对象")
    return value


def normalize_questions(raw_questions: Iterable[Any]) -> List[Dict[str, Any]]:
    """Validate and copy the model-facing question request."""

    questions = list(raw_questions or [])
    if not questions:
        raise UserQuestionValidationError("questions 必须至少包含一个问题")
    if len(questions) > MAX_QUESTIONS:
        raise UserQuestionValidationError(f"questions 一次最多 {MAX_QUESTIONS} 个问题")

    normalized: List[Dict[str, Any]] = []
    seen_question_ids: set[str] = set()
    for index, raw_value in enumerate(questions):
        raw = _as_dict(raw_value, field_name=f"questions[{index}]")
        question_id = _text(
            raw.get("id"),
            field_name=f"questions[{index}].id",
            required=True,
        )
        if question_id in seen_question_ids:
            raise UserQuestionValidationError(f"问题 id 重复: {question_id}")
        seen_question_ids.add(question_id)

        question = _text(
            raw.get("question"),
            field_name=f"questions[{index}].question",
            required=True,
        )
        header = _text(
            raw.get("header"),
            field_name=f"questions[{index}].header",
        )

        raw_options = raw.get("options")
        options: List[Dict[str, Any]] = []
        if raw_options is not None:
            if not isinstance(raw_options, list):
                raise UserQuestionValidationError(f"questions[{index}].options 必须是数组")
            for option_index, raw_option_value in enumerate(raw_options):
                raw_option = _as_dict(
                    raw_option_value,
                    field_name=f"questions[{index}].options[{option_index}]",
                )
                label = _text(
                    raw_option.get("label"),
                    field_name=f"questions[{index}].options[{option_index}].label",
                    required=True,
                )
                options.append(
                    {
                        # DSH exposes label-only options to the model. The UI still
                        # receives a private stable id so duplicate clicks and
                        # out-of-band answers never depend on display text.
                        "id": f"option_{option_index + 1}",
                        "label": label,
                        "description": _text(
                            raw_option.get("description"),
                            field_name=(f"questions[{index}].options[{option_index}].description"),
                        ),
                        "recommended": bool(_RECOMMENDED_SUFFIX.search(label)),
                    },
                )

        normalized.append(
            {
                "id": question_id,
                "header": header,
                "question": question,
                "options": options,
                "multi_select": bool(raw.get("multi_select", False)) and bool(options),
            },
        )
    return normalized


def _normalize_answers(
    raw_answers: Iterable[Any],
    questions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    answers = list(raw_answers or [])
    if len(answers) != len(questions):
        raise UserQuestionValidationError("答案数量必须与问题数量一致")

    normalized: List[Dict[str, Any]] = []
    for index, (raw_value, question) in enumerate(zip(answers, questions)):
        raw = _as_dict(raw_value, field_name=f"answers[{index}]")
        answer_id = _text(
            raw.get("id"),
            field_name=f"answers[{index}].id",
            required=True,
        )
        if answer_id != question["id"]:
            raise UserQuestionValidationError("答案 id 或顺序与原问题不一致")

        selected_raw = raw.get("selected", [])
        if not isinstance(selected_raw, list):
            raise UserQuestionValidationError(f"answers[{index}].selected 必须是数组")
        selected = [
            _text(
                value,
                field_name=f"answers[{index}].selected",
                limit=_MAX_ID_LEN,
                required=True,
            )
            for value in selected_raw
        ]
        if len(selected) != len(set(selected)):
            raise UserQuestionValidationError(f"answers[{index}].selected 不能重复")

        option_labels = {option["id"]: option["label"] for option in question["options"]}
        if any(option_id not in option_labels for option_id in selected):
            raise UserQuestionValidationError(f"answers[{index}] 包含未知选项")
        if not question["multi_select"] and len(selected) > 1:
            raise UserQuestionValidationError(f"answers[{index}] 是单选题")

        custom = _text(
            raw.get("custom"),
            field_name=f"answers[{index}].custom",
            limit=_MAX_CUSTOM_LEN,
        )
        # DSH semantics: custom text supplements checked labels for a
        # multi-select question, but replaces the option for single-select.
        if custom and not question["multi_select"]:
            selected = []
        skipped = bool(raw.get("skipped", False))
        if skipped and (selected or custom):
            raise UserQuestionValidationError("跳过的问题不能同时包含选项或补充文本")
        if not skipped and not selected and not custom:
            raise UserQuestionValidationError("每个问题必须作答或明确跳过")

        answer = {
            "id": answer_id,
            "selected": selected,
            "selected_labels": [option_labels[option_id] for option_id in selected],
            "skipped": skipped,
        }
        if custom:
            answer["custom"] = custom
        normalized.append(answer)
    return normalized


def _gc_locked(now: float) -> None:
    dead = [
        chat_id
        for chat_id, state in _CHATS.items()
        if not state.pending and now - state.last_ts > _STATE_IDLE_TTL_SECONDS
    ]
    for chat_id in dead:
        _CHATS.pop(chat_id, None)


def _state_locked(chat_id: str) -> _ChatQuestions:
    now = time.monotonic()
    _gc_locked(now)
    state = _CHATS.get(chat_id)
    if state is None:
        state = _ChatQuestions()
        _CHATS[chat_id] = state
    state.last_ts = now
    return state


def _requested_signal(pending: _PendingQuestion) -> Dict[str, Any]:
    return {
        "event": "requested",
        "request_id": pending.request_id,
        "questions": copy.deepcopy(pending.questions),
        "created_at": pending.created_at,
        "expires_at": pending.expires_at,
    }


def _resolved_signal(pending: _PendingQuestion) -> Dict[str, Any]:
    return {
        "event": "resolved",
        "request_id": pending.request_id,
        "outcome": pending.outcome,
    }


def get_ui_queue(chat_id: Optional[str]) -> Optional[asyncio.Queue]:
    if not chat_id:
        return None
    with _LOCK:
        state = _CHATS.get(chat_id)
        return state.ui_signals if state is not None else None


def get_all_pending(chat_id: Optional[str]) -> List[Dict[str, Any]]:
    if not chat_id:
        return []
    with _LOCK:
        state = _CHATS.get(chat_id)
        if state is None:
            return []
        return [_requested_signal(item) for item in state.pending.values()]


def list_pending_chat_ids() -> List[str]:
    with _LOCK:
        _gc_locked(time.monotonic())
        return [chat_id for chat_id, state in _CHATS.items() if state.pending]


def has_pending(chat_id: Optional[str]) -> bool:
    if not chat_id:
        return False
    with _LOCK:
        state = _CHATS.get(chat_id)
        return bool(state and state.pending)


def _claim_locked(
    state: _ChatQuestions,
    pending: _PendingQuestion,
    *,
    outcome: str,
    answers: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    if state.pending.pop(pending.request_id, None) is None:
        return False
    pending.outcome = outcome
    pending.answers = answers or []
    state.last_ts = time.monotonic()
    if state.ui_signals is not None:
        state.ui_signals.put_nowait(_resolved_signal(pending))
    pending.event.set()
    return True


def answer(
    chat_id: str,
    request_id: str,
    answers: Iterable[Any],
) -> Dict[str, Any]:
    """Validate and claim a pending request; first valid claimant wins."""

    with _LOCK:
        state = _CHATS.get(chat_id)
        pending = state.pending.get(request_id) if state is not None else None
        if state is None or pending is None:
            return {"ok": False, "reason": "stale", "error": "该问题已失效"}
        try:
            normalized = _normalize_answers(answers, pending.questions)
        except UserQuestionValidationError as exc:
            return {"ok": False, "reason": "bad_answer", "error": str(exc)}
        _claim_locked(state, pending, outcome="answered", answers=normalized)
        return {"ok": True, "outcome": "answered"}


def cancel(chat_id: str, request_id: str) -> Dict[str, Any]:
    """Cancel a pending question from the user interface."""

    with _LOCK:
        state = _CHATS.get(chat_id)
        pending = state.pending.get(request_id) if state is not None else None
        if state is None or pending is None:
            return {"ok": False, "reason": "stale", "error": "该问题已失效"}
        _claim_locked(state, pending, outcome="cancelled")
        return {"ok": True, "outcome": "cancelled"}


async def ask(
    *,
    chat_id: Optional[str],
    questions: Iterable[Any],
    interactive: bool = True,
    timeout: float = MAX_WAIT_SECONDS,
) -> Dict[str, Any]:
    """Suspend the caller until the browser answers, cancels, or times out."""

    normalized = normalize_questions(questions)
    if not interactive or not chat_id:
        return {"status": STATUS_BLOCKED, "answers": []}

    created_at = time.time()
    pending = _PendingQuestion(
        request_id=uuid.uuid4().hex,
        questions=normalized,
        created_at=created_at,
        expires_at=created_at + timeout,
        event=asyncio.Event(),
    )
    with _LOCK:
        state = _state_locked(chat_id)
        if state.ui_signals is None:
            state.ui_signals = asyncio.Queue()
        state.pending[pending.request_id] = pending
        state.ui_signals.put_nowait(_requested_signal(pending))

    try:
        await asyncio.wait_for(pending.event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        with _LOCK:
            state = _CHATS.get(chat_id)
            if state is not None:
                _claim_locked(state, pending, outcome="timeout")
        # A valid HTTP answer/cancel may have claimed the request at the same
        # event-loop boundary where wait_for delivered TimeoutError. The
        # claimant owns the terminal outcome; never overwrite it with timeout.
    except asyncio.CancelledError:
        with _LOCK:
            state = _CHATS.get(chat_id)
            if state is not None:
                _claim_locked(state, pending, outcome="cancelled")
        raise

    if pending.outcome == "answered":
        return {"status": "answered", "answers": copy.deepcopy(pending.answers)}
    return {"status": pending.outcome or "cancelled", "answers": []}


__all__ = [
    "KIND_USER_QUESTION",
    "MAX_QUESTIONS",
    "MAX_WAIT_SECONDS",
    "STATUS_BLOCKED",
    "UserQuestionValidationError",
    "answer",
    "ask",
    "cancel",
    "get_all_pending",
    "get_ui_queue",
    "has_pending",
    "list_pending_chat_ids",
    "normalize_questions",
]
