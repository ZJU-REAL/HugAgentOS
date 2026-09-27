"""Selftest: POST /v1/agents/responses with stream=true wire contract.

Run:
  PYTHONPATH=src/backend .venv/bin/python src/backend/tests/orchestration/stream_protocol_selftest.py
"""

from __future__ import annotations

import json


def _parse_sse_chunks(raw: str) -> list[str]:
    chunks: list[str] = []
    for block in raw.split("\n\n"):
        line = block.strip()
        if not line:
            continue
        chunks.append(line)
    return chunks


async def _collect_stream() -> str:
    from types import SimpleNamespace
    from unittest.mock import patch

    from api.routes.v1 import agent_responses
    from api.schemas import ChatRequest
    from core.auth.backend import UserContext
    from orchestration import chat_run_executor

    async def fake_start(request, user, db):
        assert request.stream is True
        return SimpleNamespace(run_id="stream-selftest", message_id="reply-1", chat_id=request.chat_id)

    async def fake_follow(run_id, *, from_offset=0):
        assert run_id == "stream-selftest"
        yield {"type": "content", "delta": "# 报告正文", "_internal": "must not leak"}
        yield {
            "type": "meta",
            "route": "main",
            "is_markdown": True,
            "sources": [{"source_type": "database", "name": "test", "detail": "ok"}],
            "artifacts": [{"type": "docx", "name": "报告.docx", "url": "/files/demo"}],
            "warnings": ["DOCX export unavailable: test"],
        }

    # Exercise the real response route and SSE wire formatter; replace only
    # admission and the durable event source so no model/database is needed.
    with patch.object(agent_responses, "_start_response_run", fake_start), patch.object(
        chat_run_executor, "follow_run", fake_follow
    ):
        response = await agent_responses.agent_response(
            ChatRequest(chat_id="stream_selftest", message="生成报告", stream=True),
            UserContext(user_id="selftest_user", user_center_id="test", username="test"),
            None,
        )
        assert response.media_type == "text/event-stream"
        pieces = []
        async for chunk in response.body_iterator:
            pieces.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
        return "".join(pieces)


def main() -> int:
    import asyncio

    payload = asyncio.run(_collect_stream())
    assert "_internal" not in payload, "transport-internal fields leaked"

    events = _parse_sse_chunks(payload)
    assert events, "expected at least one SSE event"
    assert events[-1] == "data: [DONE]", f"expected final [DONE], got {events[-1]!r}"

    first = events[0]
    assert first.startswith("data: "), f"unexpected first event: {first!r}"
    data_1 = json.loads(first[len("data: ") :])
    assert any(k in data_1 for k in ("delta", "content", "text")), "text event missing delta/content/text"

    meta_evt = None
    for evt in events[1:-1]:
        if not evt.startswith("data: "):
            continue
        raw = evt[len("data: ") :]
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("type") == "meta":
            meta_evt = obj
            break

    assert isinstance(meta_evt, dict), "missing meta event"
    assert "delta" not in meta_evt and "content" not in meta_evt and "text" not in meta_evt
    assert isinstance(meta_evt.get("artifacts"), list)

    print("stream_protocol_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
