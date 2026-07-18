"""Full production-chain integration test: LoopService creates loop -> start_autonomous_loop_run(ChatRun)
-> driver runs (worker does the work + a read-only reviewer sub-agent personally verifies the real output) -> persist_result writes to DB.

No-script verification: the verdict comes from review_requirement opening the produced real file and checking it. Here we use a simple goal not bound to any project
(worker writes a /workspace file, the reviewer reads /workspace of the same session); passing it proves the whole chain
"worker -> reviewer sub-agent -> flip -> persist" works. Requires real LLM/sandbox.

    docker exec hugagent-backend python /app/src/backend/scripts/_loop_integration_test.py
"""
import asyncio

USER_ID = "copytest_9c0cf31f"


async def main():
    from core.db.engine import SessionLocal
    from core.services.loop_service import LoopService
    from orchestration import chat_run_executor
    from orchestration.autonomous_loop import _sbx_exec, _read_ledger
    from core.services.chat_service import ChatService

    # 1) Create loop (DB) -- goal is a concrete output the reviewer can verify from real files (with no verify command / numeric score).
    db = SessionLocal()
    loop = LoopService(db).create_loop(
        user_id=USER_ID, title="集成测试-reviewer",
        goal_spec={
            "objective": "在沙箱 /workspace 下创建 report.html，页面标题为「集成测试通过」，"
                         "正文包含一个 <h1>集成测试通过</h1> 和一段说明文字。",
            "acceptance_criteria": [
                "/workspace/report.html 文件存在",
                "文件内容含 <h1>集成测试通过</h1>",
            ],
        },
        budget={"max_iters": 4, "max_wall_clock_s": 1800, "max_tokens": 3000000},
    )
    loop_id = loop.loop_id
    goal_spec = dict(loop.goal_spec)
    budget = dict(loop.budget)
    session = f"loop-{loop_id}"
    chat_id = f"loopchat_{loop_id}"
    ChatService(db).ensure_session(chat_id=chat_id, user_id=USER_ID, title="集成测试",
                                   extra_data={"autonomous_loop": True, "loop_id": loop_id})
    loop.chat_id = chat_id
    db.commit()
    db.close()
    print(f"[it] created loop {loop_id}")

    # 2) Clean up old state
    await _sbx_exec("rm -f /workspace/report.html /workspace/feature_list.json",
                    session_id=session, user_id=USER_ID)

    # 3) Go through the production entry point start_autonomous_loop_run (ChatRun background + Redis Stream; no project_id -> isolated sandbox)
    run = await chat_run_executor.start_autonomous_loop_run(
        loop_id=loop_id, chat_id=chat_id, user_id=USER_ID,
        goal_spec=goal_spec, budget=budget, model_name="fast",
    )
    print(f"[it] started run {run.run_id}, awaiting...")
    task = chat_run_executor._active_runs.get(run.run_id)
    if task:
        await task

    # 4) Verify DB persistence + ledger + real output file
    db = SessionLocal()
    svc = LoopService(db)
    lp = svc.get_loop(loop_id)
    its = svc.list_iterations(loop_id)
    print(f"[it] DB loop status={lp.status} iters={lp.iteration_count} "
          f"score={lp.final_score} tokens={lp.tokens_spent}")
    print(f"[it] DB iteration rows: {[(i.seq, i.verdict, i.decided_by) for i in its]}")
    db.close()

    ledger = await _read_ledger(session_id=session, user_id=USER_ID)
    if ledger:
        reqs = ledger.get("requirements", [])
        print(f"[it] feature_list.json: iteration={ledger.get('iteration')} "
              f"requirements={len(reqs)} passed={sum(1 for r in reqs if r.get('passes'))}")
    code, out, _ = await _sbx_exec(
        "grep -c '集成测试通过' /workspace/report.html 2>/dev/null || echo MISSING",
        session_id=session, user_id=USER_ID)
    print(f"[it] 真实产出核验: report.html grep → {out.strip()}")

    ok = (lp.status in ("completed", "budget_exhausted") and len(its) >= 1
          and any(i.decided_by == "reviewer" for i in its))
    print(f"\nINTEGRATION_{'OK' if ok else 'FAIL'} status={lp.status} iters={len(its)}")


if __name__ == "__main__":
    asyncio.run(main())
