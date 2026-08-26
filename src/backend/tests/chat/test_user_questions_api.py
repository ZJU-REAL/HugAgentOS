"""HTTP seam tests for pending user-question recovery and answers."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from api.routes.v1 import chats
from core.llm.tools import user_questions
from fastapi import FastAPI

_QUESTION = {
    "id": "scope",
    "question": "修改范围？",
    "options": [
        {"label": "仅当前页面"},
        {"label": "全部页面"},
    ],
}


@pytest.fixture
def api_app(monkeypatch):
    class FakeChatService:
        def __init__(self, _db):
            pass

        def get_session(self, chat_id, user_id):
            if chat_id == "owned-chat" and user_id == "owner":
                return object()
            return None

    monkeypatch.setattr(chats, "ChatService", FakeChatService)
    app = FastAPI()
    app.include_router(chats.router)
    app.dependency_overrides[chats.get_current_user] = lambda: SimpleNamespace(
        user_id="owner",
    )
    app.dependency_overrides[chats.get_db] = lambda: object()
    return app


@pytest.mark.asyncio
async def test_pending_recovery_answer_validation_and_first_claimant(api_app):
    waiting = asyncio.create_task(
        user_questions.ask(
            chat_id="owned-chat",
            questions=[_QUESTION],
            interactive=True,
            timeout=2,
        ),
    )
    await asyncio.sleep(0)
    pending = user_questions.get_all_pending("owned-chat")[0]
    request_id = pending["request_id"]

    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        recovered = await client.get("/v1/chats/owned-chat/pending-user-questions")
        assert recovered.status_code == 200
        assert recovered.json()["data"]["requests"][0]["request_id"] == request_id

        listed = await client.get("/v1/chats/pending-user-questions")
        assert listed.status_code == 200
        assert listed.json()["data"]["items"][0]["chat_id"] == "owned-chat"

        invalid = await client.post(
            f"/v1/chats/owned-chat/user-questions/{request_id}/answer",
            json={"answers": [{"id": "scope", "selected": ["unknown"]}]},
        )
        assert invalid.status_code == 400
        assert user_questions.has_pending("owned-chat") is True

        answered = await client.post(
            f"/v1/chats/owned-chat/user-questions/{request_id}/answer",
            json={"answers": [{"id": "scope", "selected": ["option_1"]}]},
        )
        assert answered.status_code == 200
        assert answered.json()["data"] == {"ok": True, "outcome": "answered"}

        duplicate = await client.post(
            f"/v1/chats/owned-chat/user-questions/{request_id}/answer",
            json={"answers": [{"id": "scope", "selected": ["option_2"]}]},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["data"]["stale"] is True

        unauthorized = await client.post(
            f"/v1/chats/not-owned/user-questions/{request_id}/answer",
            json={"answers": [{"id": "scope", "selected": ["option_1"]}]},
        )
        assert unauthorized.status_code == 404

        empty = await client.get("/v1/chats/owned-chat/pending-user-questions")
        assert empty.json()["data"]["requests"] == []

    result = await asyncio.wait_for(waiting, timeout=1)
    assert result["status"] == "answered"
    assert result["answers"][0]["selected_labels"] == ["仅当前页面"]


@pytest.mark.asyncio
async def test_cancel_endpoint_resolves_wait_authoritatively(api_app):
    waiting = asyncio.create_task(
        user_questions.ask(
            chat_id="owned-chat",
            questions=[_QUESTION],
            interactive=True,
            timeout=2,
        ),
    )
    await asyncio.sleep(0)
    request_id = user_questions.get_all_pending("owned-chat")[0]["request_id"]

    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/v1/chats/owned-chat/user-questions/{request_id}/cancel",
        )
        assert response.status_code == 200
        assert response.json()["data"] == {"ok": True, "outcome": "cancelled"}

    assert (await waiting) == {"status": "cancelled", "answers": []}

@pytest.mark.asyncio
async def test_four_question_round_is_answerable(api_app):
    # The answer body used to carry its own ``max_length=3`` while the tool
    # accepted any number of questions, so a fourth question could be asked
    # but never answered (422 "at most 3 items"). The body cap now comes from
    # the same domain constant the request itself is validated against.
    questions = [
        {"id": f"q{index}", "question": f"问题 {index}？", "options": [{"label": "是"}]}
        for index in range(4)
    ]
    waiting = asyncio.create_task(
        user_questions.ask(
            chat_id="owned-chat",
            questions=questions,
            interactive=True,
            timeout=2,
        ),
    )
    await asyncio.sleep(0)
    request_id = user_questions.get_all_pending("owned-chat")[0]["request_id"]

    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        answered = await client.post(
            f"/v1/chats/owned-chat/user-questions/{request_id}/answer",
            json={
                "answers": [
                    {"id": item["id"], "selected": ["option_1"]} for item in questions
                ],
            },
        )
        assert answered.status_code == 200

    result = await asyncio.wait_for(waiting, timeout=1)
    assert [answer["id"] for answer in result["answers"]] == [
        item["id"] for item in questions
    ]


@pytest.mark.asyncio
async def test_answer_body_rejects_more_answers_than_the_question_cap(api_app):
    # Past the domain cap the body is refused before any pending lookup.
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chats/owned-chat/user-questions/missing/answer",
            json={
                "answers": [
                    {"id": f"q{index}", "selected": []}
                    for index in range(user_questions.MAX_QUESTIONS + 1)
                ],
            },
        )
        assert response.status_code == 422
