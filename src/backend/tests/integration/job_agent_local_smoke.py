"""Native desktop workflow -> real agent forks -> child tools -> real files.

Run with packaged private Python and a new absolute --output directory.
Only model decisions are scripted; APIs, factory, permissions and processes are real.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from job_local_smoke import configure, listener

SCRIPT = """from hugagent_job import agent, job, ledger
items = [{"key": key, "payload": {"key": key}} for key in ("A", "B")]
ledger.seed(items)
def handle(item):
    return agent("FORK_" + item["key"], schema={"type":"object","required":["key","ok"]}, item_key=item["key"])
job.map(items, handle, concurrency=2)
"""
requests = []


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tools = {t["function"]["name"] for t in body.get("tools", [])}
        messages = body["messages"]
        user = next((str(m.get("content")) for m in reversed(messages) if m["role"] == "user"), "")
        results = [m for m in messages if m["role"] == "tool"]
        names = [m.get("name") for m in results]
        main = "run_job" in tools
        child = "call_subagent" not in tools and not main
        match = re.search(r"(?:FORK_|CHILD_)([AB])", user)
        key = match.group(1) if match else "A"
        if main and not results:
            tool, args = "Write", {"file_path": "workflow-e2e.py", "content": SCRIPT}
        elif main and len(results) == 1:
            tool, args = "run_job", {
                "script_path": "workflow-e2e.py",
                "wait": True,
                "max_seconds": 120,
                "max_calls": 4,
                "concurrency": 2,
                "progress_wake_sec": 0,
            }
        elif not main and not child and not results:
            tool, args = "call_subagent", {"agent_id": "builtin.worker", "task": "CHILD_" + key}
        elif child and not results:
            tool, args = "Bash", {"command": "printf 'CHILD_" + key + "' > child-" + key + ".txt"}
        else:
            tool = None
            content = (
                "MAIN_OK"
                if main
                else ("CHILD_" + key if child else json.dumps({"key": key, "ok": True}))
            )
        requests.append(
            {
                "role": "main" if main else "child" if child else "derived",
                "key": key,
                "tool": tool,
                "tools": sorted(tools),
                "result_names": names,
            }
        )
        if tool:
            assert tool in tools, (tool, sorted(tools))
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "tc-" + str(len(requests)),
                        "type": "function",
                        "function": {"name": tool, "arguments": json.dumps(args)},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": content}, "stop"
        chunk = {
            "id": "test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": body["model"],
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        end = dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": finish}])
        data = (
            "data: "
            + json.dumps(chunk)
            + "\n\n"
            + "data: "
            + json.dumps(end)
            + "\n\n"
            + "data: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def acceptance(root, backend_socket, runner_socket, model):
    import core.jobs.owner as owner
    import core.jobs.supervisor as supervisor
    import core.llm.factory as factory
    import uvicorn
    from agentscope.message import UserMsg
    from api.app import app as production_app
    from api.routes.v1.internal_jobs import router
    from core.db.engine import Base, SessionLocal, engine
    from core.models.repository import assign_role, create_provider
    from core.db.models import ChatSession, Job, UserShadow
    from core.capabilities.agents.builtin import get_builtin_subagent, list_builtin_subagents
    from core.sandbox import get_sandbox_provider
    from core.sandbox.runner import server as runner
    from core.jobs.store import JobService
    from core.runs.journal import durable_run_binding
    from fastapi import FastAPI

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(UserShadow(user_id="acceptance-user", username="synthetic"))
        db.add(
            ChatSession(
                chat_id="acceptance-chat", user_id="acceptance-user", title="Native workflow test"
            )
        )
        db.commit()
        provider = create_provider(
            db,
            display_name="Local scripted model",
            provider_type="chat",
            base_url=f"http://127.0.0.1:{model.server_port}/v1",
            api_key="test-only",
            model_name="workflow-test",
            extra_config={"context_length": 32768, "api_protocol": "chat_completions"},
        )
        pid = provider.provider_id
        assign_role(db, "main_agent", pid)
    # Production router/dependencies, no installation lifecycle or unrelated sidecars.
    app = FastAPI()
    app.include_router(router)
    callbacks = []

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    @app.middleware("http")
    async def capture(request, following):
        response = await following(request)
        callbacks.append({"path": request.url.path, "status": response.status_code})
        return response

    servers = [
        uvicorn.Server(uvicorn.Config(a, log_level="error", access_log=False))
        for a in (app, runner.app)
    ]
    tasks = [
        asyncio.create_task(s.serve(sockets=[sock]))
        for s, sock in zip(servers, (backend_socket, runner_socket))
    ]
    observed = []
    original = factory.create_agent_executor

    async def inspect_factory(**kwargs):
        agent, clients = await original(**kwargs)
        role = (
            "child"
            if kwargs.get("user_agent")
            else "derived" if kwargs.get("derived_main") else "main"
        )
        observed.append(
            {
                "role": role,
                "chat_id": kwargs.get("chat_id"),
                "sandbox": kwargs.get("sandbox_session_id"),
                "approval": agent.toolkit._tool_permission_service.runtime.approval_mode,
                "max_iters": agent.react_config.max_iters,
            }
        )
        return agent, clients

    factory.create_agent_executor = inspect_factory
    try:
        for _ in range(200):
            if all(s.started for s in servers):
                break
            await asyncio.sleep(0.025)
        assert all(s.started for s in servers), "native HTTP service startup failed"
        owner.bind()
        async with durable_run_binding(
            user_id="acceptance-user",
            chat_id="acceptance-chat",
            kind="batch_item",
            external_id="native-workflow-e2e",
            request_payload={},
        ) as binding:
            agent, clients = await factory.create_agent_executor(
                current_user_id="acceptance-user",
                chat_id=binding.chat_id,
                run_id=binding.run_id,
                journal_owner=binding.owner,
                sandbox_session_id="acceptance-chat",
                enabled_skill_ids=[],
                enabled_mcp_ids=[],
                enabled_kb_ids=[],
                visible_subagents=list_builtin_subagents(),
                workflow_mode=True,
                approval_mode="full",
                model_name="workflow-test",
                model_provider_id=pid,
            )
            from core.llm.mcp.manager import close_clients

            try:
                answer = await asyncio.wait_for(
                    agent.reply(UserMsg(name="user", content="MAIN_NATIVE_WORKFLOW")), 160
                )
                assert answer.get_text_content() == "MAIN_OK", answer
            finally:
                await close_clients(clients)
        with SessionLocal() as db:
            rows = db.query(Job).all()
            assert len(rows) == 1, len(rows)
            row = rows[0]
            status = JobService(db).stats(row.job_id)
            assert row.status == "completed" and status["done"] == 2, (row.status, status)
            parent = row.extra_data["start_params"]["parent_runtime"]
            assert "builtin.worker" in parent["subagent_ids"]
            job_id = row.job_id
        provider = get_sandbox_provider()
        results = {}
        for key in ("A", "B"):
            text = (
                await provider.get_file(
                    "acceptance-chat", "child-" + key + ".txt", user_id="acceptance-user"
                )
            ).decode()
            assert text == "CHILD_" + key, text
            results[key] = text
        forks = [o for o in observed if o["role"] == "derived"]
        children = [o for o in observed if o["role"] == "child"]
        assert len(forks) == len(children) == 2, observed
        assert len({o["chat_id"] for o in forks}) == 2
        assert all(o["approval"] == "full" for o in forks + children)
        assert all(o["max_iters"] > 10 for o in forks)
        assert all(
            o["max_iters"] == get_builtin_subagent("builtin.worker").max_iters for o in children
        )
        assert all(o["sandbox"] == "acceptance-chat" for o in forks + children)
        assert len([r for r in requests if r["tool"] == "call_subagent"]) == 2
        assert len([r for r in requests if r["tool"] == "Bash"]) == 2
        assert all(c["status"] == 200 for c in callbacks), callbacks
        report = {
            "python": sys.executable,
            "platform": sys.platform,
            "production_app_imported": production_app is not None,
            "job_id": job_id,
            "status": "completed",
            "done": 2,
            "files": results,
            "agents": observed,
            "callbacks": callbacks,
            "model_calls": requests,
        }
        (root / "acceptance.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            "PASS native desktop workflow: 2 independent main forks -> 2 child agents -> real Bash/files",
            flush=True,
        )
    finally:
        factory.create_agent_executor = original
        await supervisor.shutdown()
        await runner.process_sessions.close_all()
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    if not args.output.is_absolute() or root.exists():
        parser.error("output must be a new absolute directory")
    root.mkdir(parents=True)
    backend, runner = listener(), listener()
    configure(root, backend.getsockname()[1], runner.getsockname()[1])
    os.environ.update(
        PLUGIN_PROGRESSIVE_LOADING="false", MEM0_ENABLED="false", ENABLE_SUMMARY="false"
    )
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    try:
        asyncio.run(acceptance(root, backend, runner, model))
    finally:
        model.shutdown()
        backend.close()
        runner.close()
