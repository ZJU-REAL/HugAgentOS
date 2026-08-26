"""Public state-machine tests for suspended user questions."""

import asyncio

import pytest
from core.llm.tools import user_questions as uq

_QUESTIONS = [
    {
        "id": "style",
        "header": "页面风格",
        "question": "你希望采用哪种视觉方向？",
        "options": [
            {
                "label": "政务简洁 (Recommended)",
                "description": "强调可信与易读。",
            },
            {
                "label": "现代视觉",
                "description": "强调图片和留白。",
            },
        ],
        "multi_select": False,
    },
]


def _drain(chat_id: str) -> list[dict]:
    queue = uq.get_ui_queue(chat_id)
    items: list[dict] = []
    if queue is None:
        return items
    while True:
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


@pytest.mark.asyncio
async def test_answer_resumes_same_wait_and_emits_authoritative_resolution():
    chat_id = "chat_user_question_answered"
    waiting = asyncio.create_task(
        uq.ask(chat_id=chat_id, questions=_QUESTIONS, interactive=True, timeout=2),
    )
    await asyncio.sleep(0)

    requested = _drain(chat_id)
    assert len(requested) == 1
    request = requested[0]
    assert request["event"] == "requested"
    assert request["questions"][0]["options"][0]["recommended"] is True
    assert request["questions"][0]["options"][0]["id"] == "option_1"
    assert uq.has_pending(chat_id) is True

    resolved = uq.answer(
        chat_id,
        request["request_id"],
        [{"id": "style", "selected": ["option_1"], "custom": "整体偏蓝色"}],
    )
    assert resolved == {"ok": True, "outcome": "answered"}

    result = await asyncio.wait_for(waiting, timeout=1)
    assert result == {
        "status": "answered",
        "answers": [
            {
                "id": "style",
                "selected": [],
                "selected_labels": [],
                "custom": "整体偏蓝色",
                "skipped": False,
            },
        ],
    }
    assert uq.has_pending(chat_id) is False
    assert _drain(chat_id) == [
        {
            "event": "resolved",
            "request_id": request["request_id"],
            "outcome": "answered",
        },
    ]


@pytest.mark.asyncio
async def test_invalid_answer_is_rejected_without_claiming_pending_request():
    chat_id = "chat_user_question_invalid"
    waiting = asyncio.create_task(
        uq.ask(chat_id=chat_id, questions=_QUESTIONS, interactive=True, timeout=2),
    )
    await asyncio.sleep(0)
    request = _drain(chat_id)[0]

    invalid = uq.answer(
        chat_id,
        request["request_id"],
        [{"id": "style", "selected": ["not-an-option"]}],
    )
    assert invalid["ok"] is False
    assert invalid["reason"] == "bad_answer"
    assert uq.has_pending(chat_id) is True
    assert waiting.done() is False

    assert uq.cancel(chat_id, request["request_id"])["ok"] is True
    assert (await waiting)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_skip_and_timeout_have_distinct_results_and_clean_pending_state():
    skip_chat = "chat_user_question_skip"
    skipped = asyncio.create_task(
        uq.ask(chat_id=skip_chat, questions=_QUESTIONS, interactive=True, timeout=2),
    )
    await asyncio.sleep(0)
    request = _drain(skip_chat)[0]
    assert (
        uq.answer(
            skip_chat,
            request["request_id"],
            [{"id": "style", "selected": [], "skipped": True}],
        )["ok"]
        is True
    )
    skip_result = await skipped
    assert skip_result["status"] == "answered"
    assert skip_result["answers"][0]["skipped"] is True

    timeout_chat = "chat_user_question_timeout"
    timeout_result = await uq.ask(
        chat_id=timeout_chat,
        questions=_QUESTIONS,
        interactive=True,
        timeout=0.01,
    )
    assert timeout_result == {"status": "timeout", "answers": []}
    assert uq.has_pending(timeout_chat) is False
    assert _drain(timeout_chat)[-1]["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_non_interactive_question_does_not_register_a_wait():
    result = await uq.ask(
        chat_id="chat_user_question_noninteractive",
        questions=_QUESTIONS,
        interactive=False,
    )
    assert result == {"status": "blocked_non_interactive", "answers": []}
    assert uq.get_all_pending("chat_user_question_noninteractive") == []


@pytest.mark.asyncio
async def test_answer_claim_at_timeout_boundary_remains_authoritative(monkeypatch):
    """wait_for may report timeout just after the HTTP answer won the lock."""

    chat_id = "chat_user_question_timeout_race"

    async def answer_then_timeout(waitable, *, timeout):
        del timeout
        waitable.close()
        request_id = uq.get_all_pending(chat_id)[0]["request_id"]
        claimed = uq.answer(
            chat_id,
            request_id,
            [{"id": "style", "selected": ["option_1"]}],
        )
        assert claimed["ok"] is True
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", answer_then_timeout)
    result = await uq.ask(
        chat_id=chat_id,
        questions=_QUESTIONS,
        interactive=True,
        timeout=0.01,
    )
    assert result["status"] == "answered"
    assert result["answers"][0]["selected"] == ["option_1"]


def test_question_contract_rejects_duplicate_ids_and_empty_batches():
    duplicate = [dict(_QUESTIONS[0]), dict(_QUESTIONS[0])]
    with pytest.raises(uq.UserQuestionValidationError, match="问题 id 重复"):
        uq.normalize_questions(duplicate)
    with pytest.raises(uq.UserQuestionValidationError, match="至少"):
        uq.normalize_questions([])


def test_question_contract_accepts_dsh_label_only_options_and_unbounded_batches():
    normalized = uq.normalize_questions(
        [
            {
                "id": f"question-{index}",
                "question": "请选择",
                "options": [{"label": f"选项 {choice}"} for choice in range(5)],
            }
            for index in range(4)
        ],
    )
    assert len(normalized) == 4
    assert normalized[0]["options"][0]["id"] == "option_1"
    assert normalized[0]["options"][0]["label"] == "选项 0"
