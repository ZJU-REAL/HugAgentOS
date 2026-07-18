"""Multi-turn tool-call e2e: verify that after the thinking-leak fix, the second turn and beyond still issue tool calls normally.

Background (chat_20260713_172036 incident): the history-replay path carrying tool_calls
bypassed strip_thinking, so the previous turn's reasoning monologue (30 </think> segments)
leaked verbatim into the next turn's context, and the model mimicked "narrative-style work",
hallucinating tool execution in pure text on later turns and no longer really calling tools.

This test uses the full real stack (ASGI hits app directly → workflow → agent → sandbox):
  T1  ask bash to run echo <sentinel1> → a tool_call event must appear
  T2  ask bash to run echo <sentinel2> → a tool_call event must appear again (the regression point)
  End use checkpoint-aware load_session_history to reconstruct the context the next turn will see,
      and assert there is no </think> residue in it (if T1's stored body itself has no </think>, this item is recorded as SKIP)

Run inside the container: cd /app/src/backend && python tests/llm/e2e_multiturn_toolcall.py
"""

from __future__ import annotations

import asyncio
import json
import uuid

USER_ID = "user_e2e_leakfix"
S1 = f"LEAKFIX_T1_{uuid.uuid4().hex[:8].upper()}"
S2 = f"LEAKFIX_T2_{uuid.uuid4().hex[:8].upper()}"


async def run_turn(client, chat_id: str, message: str, label: str):
    payload = {"chat_id": chat_id, "message": message, "enable_thinking": True}
    tool_calls: list[str] = []
    tool_results: list = []
    text_buf: list[str] = []
    async with client.stream("POST", "/v1/chats/stream", json=payload) as resp:
        print(f"[{label}] stream status: {resp.status_code}")
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw in ("", "[DONE]"):
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            et = ev.get("type") or ev.get("event")
            if et == "tool_call":
                nm = (ev.get("tool_name") or ev.get("name")
                      or (ev.get("data") or {}).get("tool_name"))
                if nm:
                    tool_calls.append(nm)
                    print(f"  [{label}] → tool_call: {nm}")
            elif et == "tool_result":
                tool_results.append(ev.get("data") or ev)
            elif et == "content" or ev.get("event") == "ai_message":
                text_buf.append(ev.get("delta") or "")
            elif et == "error":
                print(f"  [{label}] !! error event: {ev}")
    final_text = "".join(text_buf)
    print(f"[{label}] 回复尾部: …{final_text[-200:]!r}")
    print(f"[{label}] tool_calls: {tool_calls}")
    return tool_calls, tool_results, final_text


async def main() -> None:
    import httpx
    from httpx import ASGITransport

    from api.app import app
    from core.auth.session import create_session
    from core.config.settings import settings
    from core.db.engine import SessionLocal
    from core.db.models import ChatMessage

    # Seed users_shadow (chat_sessions.user_id has a foreign key), idempotent upsert
    from sqlalchemy import text as _sql_text

    _db_seed = SessionLocal()
    try:
        _db_seed.execute(_sql_text(
            "INSERT INTO users_shadow (user_id, username, email, created_at, updated_at) "
            "VALUES (:uid, :un, :em, now(), now()) ON CONFLICT (user_id) DO NOTHING"
        ), {"uid": USER_ID, "un": "e2e-leakfix", "em": "e2e-leakfix@test.local"})
        _db_seed.commit()
    finally:
        _db_seed.close()

    token = await create_session({
        "user_id": USER_ID, "username": "e2e-leakfix", "email": "e2e-leakfix@test.local",
    })
    cookie_name = settings.session.cookie_name

    results: list[tuple[str, bool, str]] = []

    def ck(name: str, cond: bool, detail: str = ""):
        results.append((name, bool(cond), detail))
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    transport = ASGITransport(app=app)
    chat_id = None
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t",
        cookies={cookie_name: token}, timeout=httpx.Timeout(600.0),
    ) as client:
        rc = await client.post("/v1/chats", json={"title": "e2e 多轮工具调用回归"})
        chat_id = rc.json()["data"]["chat_id"]
        print("chat_id =", chat_id)

        # ── T1: force one real tool call ──
        t1_calls, t1_results, t1_text = await run_turn(
            client, chat_id,
            f"请在沙箱里用 bash 执行命令 `echo {S1}`，把命令的原样输出告诉我。",
            "T1",
        )
        ck("T1 发起了工具调用", len(t1_calls) > 0, str(t1_calls))
        blob1 = json.dumps(t1_results, ensure_ascii=False) + t1_text
        ck("T1 真实执行成功（输出含 sentinel1）", S1 in blob1)

        # ── T2: after multiple turns it must still really call tools (the regression point of this fix) ──
        t2_calls, t2_results, t2_text = await run_turn(
            client, chat_id,
            f"很好。现在再用 bash 执行 `echo {S2}`，同样把原样输出告诉我。",
            "T2",
        )
        ck("T2（第二轮）仍发起了工具调用——不惰性、不叙事幻觉",
           len(t2_calls) > 0, str(t2_calls))
        blob2 = json.dumps(t2_results, ensure_ascii=False) + t2_text
        ck("T2 真实执行成功（输出含 sentinel2）", S2 in blob2)

        # ── Context reconstruction: the history the next turn will see must have no </think> residue ──
        from core.services.chat_service import ChatService
        from core.services.compaction_service import load_session_history

        db = SessionLocal()
        try:
            raw_t1 = (
                db.query(ChatMessage)
                .filter(ChatMessage.chat_id == chat_id, ChatMessage.role == "assistant")
                .order_by(ChatMessage.created_at)
                .first()
            )
            db_has_think = raw_t1 is not None and "</think>" in (raw_t1.content or "")
            print(f"T1 落库正文含 </think>: {db_has_think} "
                  f"(len={len(raw_t1.content) if raw_t1 else 0})")

            history = load_session_history(ChatService(db), chat_id, USER_ID)
            assert history is not None, "load_session_history 返回 None（会话/权限异常）"
            leaked = []
            for m in history:
                c = m.get("content")
                if isinstance(c, str) and "</think>" in c:
                    leaked.append(("str", c[:80]))
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and "</think>" in str(b.get("text", "")):
                            leaked.append((b.get("type"), str(b.get("text"))[:80]))
            if db_has_think:
                ck("回放历史已剥净 </think>（泄漏修复生效）",
                   not leaked, f"leaked={leaked[:3]}")
            else:
                print("[SKIP] T1 落库正文无 </think>（模型本轮未发思考闭标签），"
                      "泄漏断言不具备前置条件；strip 行为由单测钉住")
        finally:
            db.close()

        # ── Cleanup ──
        try:
            await client.delete(f"/v1/chats/{chat_id}")
            print("已清理测试会话", chat_id)
        except Exception as exc:  # noqa: BLE001
            print("清理失败（手动删除即可）:", exc)

    failed = [r for r in results if not r[1]]
    print("\n=== e2e_multiturn_toolcall:",
          "OK" if not failed else f"{len(failed)} FAILED", "===")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
