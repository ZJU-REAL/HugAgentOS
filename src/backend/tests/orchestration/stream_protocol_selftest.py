"""Selftest: /chat/stream protocol contract.

Run:
  python -m selftests.stream_protocol_selftest
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
    import api.routes.chat as chat_routes
    from api.schemas import ChatRequest

    original_astream_workflow = chat_routes.astream_chat_workflow
    original_chat_service = chat_routes.ChatService
    original_require_auth_user = chat_routes._require_authenticated_user_id
    original_ensure_chat_session = chat_routes._ensure_chat_session
    original_load_session_messages = chat_routes._load_session_messages_from_db
    original_resolve_db_user_id = chat_routes._resolve_db_user_id

    class _FakeChatService:
        def __init__(self, db):
            _ = db

        def add_message(self, **kwargs):
            _ = kwargs
            return None

    async def _fake_astream_workflow(*, session_messages, user_message, context):
        _ = session_messages, user_message, context
        yield {"type": "content", "delta": "# 报告正文"}
        yield {
            "type": "meta",
            "route": "main",
            "is_markdown": True,
            "sources": [{"source_type": "database", "name": "test", "detail": "ok"}],
            "artifacts": [{"type": "docx", "name": "报告.docx", "url": "/files/demo"}],
            "warnings": ["DOCX export unavailable: test"],
        }

    try:
        chat_routes.ChatService = _FakeChatService
        chat_routes._require_authenticated_user_id = lambda user: "selftest_user"
        chat_routes._resolve_db_user_id = lambda db, user, request_user_id=None: "selftest_user"
        chat_routes._ensure_chat_session = lambda *args, **kwargs: {}
        chat_routes._load_session_messages_from_db = lambda *args, **kwargs: []
        chat_routes.astream_chat_workflow = _fake_astream_workflow
        req = ChatRequest(chat_id="stream_selftest", message="生成报告", model_name="qwen")
        response = await chat_routes.chat_stream(req)
        pieces: list[str] = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                pieces.append(chunk.decode("utf-8"))
            else:
                pieces.append(str(chunk))
        return "".join(pieces)
    finally:
        chat_routes.astream_chat_workflow = original_astream_workflow
        chat_routes.ChatService = original_chat_service
        chat_routes._require_authenticated_user_id = original_require_auth_user
        chat_routes._resolve_db_user_id = original_resolve_db_user_id
        chat_routes._ensure_chat_session = original_ensure_chat_session
        chat_routes._load_session_messages_from_db = original_load_session_messages


def main() -> int:
    try:
        import anyio
    except ModuleNotFoundError as e:
        print(f"stream_protocol_selftest: SKIP (missing dependency: {e})")
        return 0

    try:
        payload = anyio.run(_collect_stream)
    except ModuleNotFoundError as e:
        print(f"stream_protocol_selftest: SKIP (missing dependency: {e})")
        return 0

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
