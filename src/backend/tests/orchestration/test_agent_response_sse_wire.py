"""SSE wire contract of ``POST /v1/agents/responses`` with ``stream=true``.

Runs the real route and the real ``follow_run_as_sse`` encoder; only run
admission and the durable event source are replaced.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import core.runs.events.subscription as run_subscription
from api.routes.v1 import agent_responses
from core.auth.backend import UserContext
from core.chat.schemas import ChatRequest


async def _stream_frames(monkeypatch) -> list[str]:
    async def fake_start(request, user):
        assert request.stream is True
        return SimpleNamespace(
            run_id="run-sse", message_id="reply-1", user_message_id="user-1", chat_id=request.chat_id
        )

    async def fake_follow(run_id, *, from_offset=0):
        assert run_id == "run-sse"
        yield {"type": "content", "delta": "# 报告正文", "_internal": "must not leak"}
        yield {
            "type": "meta",
            "route": "main",
            "is_markdown": True,
            "sources": [{"source_type": "database", "name": "test", "detail": "ok"}],
            "artifacts": [{"type": "docx", "name": "报告.docx", "url": "/files/demo"}],
            "warnings": ["DOCX export unavailable: test"],
        }

    monkeypatch.setattr(agent_responses, "_start_response_run", fake_start)
    monkeypatch.setattr(run_subscription, "follow_run", fake_follow)

    response = await agent_responses.agent_response(
        ChatRequest(chat_id="chat-sse", message="生成报告", stream=True),
        UserContext(user_id="u1", user_center_id="test", username="test"),
    )
    assert response.media_type == "text/event-stream"
    assert response.headers["x-hugagent-user-message-id"] == "user-1"

    pieces = []
    async for chunk in response.body_iterator:
        pieces.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
    return [block.strip() for block in "".join(pieces).split("\n\n") if block.strip()]


async def test_stream_frames_strip_internal_fields_and_end_with_done(monkeypatch):
    frames = await _stream_frames(monkeypatch)

    assert frames[-1] == "data: [DONE]"
    assert all(frame.startswith("data: ") for frame in frames)
    assert not any("_internal" in frame for frame in frames)

    first = json.loads(frames[0][len("data: "):])
    assert first == {"type": "content", "delta": "# 报告正文"}


async def test_meta_frame_carries_artifacts_but_no_text(monkeypatch):
    frames = await _stream_frames(monkeypatch)
    payloads = [json.loads(f[len("data: "):]) for f in frames[:-1]]
    meta = next(p for p in payloads if p.get("type") == "meta")

    assert not {"delta", "content", "text"} & meta.keys()
    assert meta["artifacts"] == [{"type": "docx", "name": "报告.docx", "url": "/files/demo"}]
