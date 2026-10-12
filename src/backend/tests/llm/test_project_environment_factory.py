"""Real factory isolation check with a disposable database and no remote services."""

import os
import subprocess
import sys


def test_empty_environment_blocks_registration_and_keeps_one_run_frozen(tmp_path):
    script = r"""
import asyncio
from core.db.engine import Base, engine, SessionLocal
from core.db.models import Project, UserShadow, ChatSession, UserAgent, AdminMcpServer, BatchPlan, ChatRun
from core.db.model_repository import create_provider, assign_role
from core.llm.factory import create_agent_executor
from core.llm.factory.request import AgentRequest
from core.llm.project_environment import bind_project_environment
from orchestration.workflow import _extract_project_ctx

Base.metadata.create_all(engine)
with SessionLocal() as db:
    model = create_provider(db, display_name="mock", provider_type="chat", base_url="http://127.0.0.1:1/v1",
        api_key="test-only", model_name="mock", extra_config={"context_length": 32768})
    assign_role(db, "main_agent", model.provider_id)
    db.add(UserShadow(user_id="owner", username="Owner"))
    db.add(Project(project_id="scoped", name="Scoped", kind="personal", owner_user_id="owner",
                   extra_data={"environment": {"mode": "custom"}}))
    db.add(ChatSession(chat_id="scoped-chat", user_id="owner", project_id="scoped", title="Scoped"))
    db.add(UserAgent(agent_id="unbound", name="Unbound", owner_type="user", user_id="owner"))
    db.add(Project(project_id="selected", name="Selected", kind="personal", owner_user_id="owner",
                   extra_data={"environment": {"mode": "custom", "mcp_ids": ["allowed"]}}))
    db.add_all([AdminMcpServer(server_id=sid, display_name=sid, owner_user_id="owner",
                              is_enabled=False, transport="streamable_http", url="http://127.0.0.1:1/mcp")
                for sid in ("allowed", "outside")])
    db.commit()

async def run():
    request = AgentRequest(current_user_id="owner", chat_id="scoped-chat")
    frozen = await bind_project_environment(request)
    assert frozen is not None and frozen.skill_ids == frozen.mcp_ids == frozenset()
    agent, clients = await create_agent_executor(
        current_user_id="owner", chat_id="scoped-chat",
        enabled_skill_ids=["word-editing"], enabled_mcp_ids=["internet_search"],
        visible_subagents=[{"agent_id": "builtin.worker", "name": "Worker"}],
        top_level_chat=True, max_iters=1,
        project_ctx=_extract_project_ctx({"project_id": "scoped", "project_environment_config": {"mode": "custom"}}),
    )
    assert clients == [], clients
    names = {s["function"]["name"] for s in agent._jx_compaction_tool_schemas}
    assert "load_plugin" not in names and "call_subagent" not in names, names
    assert "internet_search" not in names, names
    assert "word-editing" not in str(agent._system_prompt)
    with SessionLocal() as db:
        project = db.get(Project, "scoped")
        project.extra_data = {"environment": {"mode": "all"}}
        db.commit()
    child = AgentRequest(current_user_id="owner", chat_id="scoped-chat", project_ctx=request.project_ctx)
    assert await bind_project_environment(child) == frozen
    next_turn = AgentRequest(current_user_id="owner", chat_id="scoped-chat")
    all_policy = await bind_project_environment(next_turn)
    assert all_policy.custom is False
    with SessionLocal() as db:
        db.add(AdminMcpServer(server_id="newly-added", display_name="New", owner_user_id="owner",
                              is_enabled=True, transport="streamable_http", url="http://127.0.0.1:1/mcp"))
        db.commit()
    same_run = AgentRequest(current_user_id="owner", chat_id="scoped-chat", project_ctx=next_turn.project_ctx)
    assert "newly-added" not in (await bind_project_environment(same_run)).mcp_ids
    future_turn = AgentRequest(current_user_id="owner", chat_id="scoped-chat")
    assert "newly-added" in (await bind_project_environment(future_turn)).mcp_ids
    outside = AgentRequest(current_user_id="owner", enabled_mcp_ids=["internet_search"])
    assert await bind_project_environment(outside) is None
    assert outside.enabled_mcp_ids == ["internet_search"]
    # Exercise the actual custom-child dispatch, whose cloud path deliberately
    # drops chat_id and used to drop all project restrictions as well.
    from core.llm.subagent_tool import _run_subagent_in_thread
    success, result, _, _ = await asyncio.to_thread(
        _run_subagent_in_thread, "unbound", "Unbound", "test", "", "owner",
        parent_runtime={"project_ctx": request.project_ctx},
    )
    assert success is False and "未绑定" in result, (success, result)

    # Use a real manifest client in place of a remote transport. The real
    # factory and Toolkit must expose only the explicitly bound private MCP,
    # including when its ambient switch is off.
    from unittest.mock import patch
    from core.llm.mcp_pool import make_client
    connected = []
    def manifest_client(name, config, **kwargs):
        connected.append(name)
        return make_client(name, {**config, "schema_source": "cloud_manifest",
            "gateway_invoke_url": "http://127.0.0.1:1/invoke", "schema_hash": "fixture",
            "manifest_tools": [{"name": "read_" + name, "description": "Read fixture",
                                "inputSchema": {"type": "object", "properties": {}}}]}, **kwargs)
    selected_ctx = {"project_id": "selected"}
    with patch("core.llm.mcp_pool.make_client", side_effect=manifest_client):
        selected_agent, selected_clients = await create_agent_executor(
            current_user_id="owner", project_ctx=selected_ctx, max_iters=1,
            enabled_mcp_ids=["outside"],
        )
    assert connected == ["allowed"], connected
    assert selected_ctx["project_environment_effective"]["mcp_ids"] == ["allowed"]
    # Plan steps share this original context object, not a fresh policy copy.
    following_step = AgentRequest(current_user_id="owner", project_ctx=selected_ctx, isolated=True)
    assert (await bind_project_environment(following_step)).mcp_ids == frozenset({"allowed"})
    selected_names = {s["function"]["name"] for s in selected_agent._jx_compaction_tool_schemas}
    assert "read_allowed" in selected_names and "read_outside" not in selected_names, selected_names
    from core.llm.mcp_manager import close_clients
    await close_clients(selected_clients)
    revoked_request = AgentRequest(current_user_id="owner", project_ctx=selected_ctx)
    await bind_project_environment(revoked_request)
    with SessionLocal() as db:
        connector = db.get(AdminMcpServer, "allowed")
        connector.owner_user_id = "someone-else"
        db.commit()
    from core.llm.project_environment_middleware import ProjectEnvironmentMiddleware
    executed = []
    async def io(**kwargs):
        executed.append("tool I/O")
        yield "executed"
    results = [item async for item in ProjectEnvironmentMiddleware(revoked_request).on_acting(
        None, {}, io)]
    assert executed == [], executed
    assert results[0].metadata["project_environment_denied"] is True

    # Exercise the real multi-item batch runner; model execution is the only
    # replaced boundary. Widening project settings after item 1 must not affect
    # item 2, and the snapshot is persisted for process recovery.
    with SessionLocal() as db:
        db.get(Project, "scoped").extra_data = {"environment": {"mode": "custom"}}
        db.add(BatchPlan(plan_id="environment-batch", user_id="owner", chat_id="scoped-chat",
                        status="confirmed", source_type="text_list", prompt_template="{text}",
                        items=[{"text": "one"}, {"text": "two"}], max_retries=0))
        db.commit()
    import orchestration.batch_runner as batch_runner
    item_policies = []
    async def model_boundary(prompt, user_id, sub_mcp_ids, **kwargs):
        item_request = AgentRequest(current_user_id=user_id, chat_id=kwargs["chat_id"],
            project_ctx=kwargs["project_ctx"], isolated=True, enabled_mcp_ids=sub_mcp_ids)
        item_policies.append(await bind_project_environment(item_request))
        if len(item_policies) == 1:
            with SessionLocal() as db:
                db.get(Project, "scoped").extra_data = {"environment": {"mode": "all"}}
                db.commit()
        return "completed", [], [], []
    with patch.object(batch_runner, "_run_item_via_workflow", side_effect=model_boundary):
        await batch_runner._run_until_done("environment-batch", "owner")
    assert len(item_policies) == 2 and all(p.mcp_ids == frozenset() for p in item_policies), item_policies
    with SessionLocal() as db:
        batch = db.get(BatchPlan, "environment-batch")
        assert batch.status == "done", batch.status
        assert batch.progress["project_environment"]["project_environment_config"]["mode"] == "custom"
    with SessionLocal() as db:
        snapshots = [r.recovery_snapshot for r in db.query(ChatRun).all()
                     if (r.recovery_snapshot or {}).get("worker_args")]
        assert snapshots
        assert all(s["worker_args"]["context"]["project_ctx"]["project_environment_config"]["mode"]
                   == "custom" for s in snapshots)
        # Simulate a competitor winning persistence after this runner resolves
        # the current all-mode environment but before its row lock is acquired.
        db.add(BatchPlan(plan_id="competing-batch", user_id="owner", chat_id="scoped-chat",
                        status="confirmed", source_type="text_list", prompt_template="{text}",
                        items=[{"text": "race"}], max_retries=0))
        db.commit()
    bind_count = 0
    async def competing_bind(req):
        nonlocal bind_count
        result = await bind_project_environment(req)
        bind_count += 1
        if bind_count == 1:
            with SessionLocal() as db:
                batch = db.get(BatchPlan, "competing-batch")
                batch.progress = {"project_environment": request.project_ctx}
                db.commit()
        return result
    raced_policies = []
    async def race_model(prompt, user_id, sub_mcp_ids, **kwargs):
        req = AgentRequest(current_user_id=user_id, chat_id=kwargs["chat_id"],
                           project_ctx=kwargs["project_ctx"], isolated=True)
        raced_policies.append(await bind_project_environment(req))
        return "completed", [], [], []
    with patch("core.llm.project_environment.bind_project_environment", side_effect=competing_bind), \
         patch.object(batch_runner, "_run_item_via_workflow", side_effect=race_model):
        await batch_runner._run_until_done("competing-batch", "owner")
    assert raced_policies == [frozen], raced_policies
    with SessionLocal() as db:
        db.add(BatchPlan(plan_id="rejected-batch", user_id="owner", chat_id="scoped-chat",
                        status="confirmed", source_type="text_list", prompt_template="{text}",
                        items=[{"text": "reject"}], max_retries=0))
        db.commit()
    from fastapi import HTTPException
    with patch("core.llm.project_environment.bind_project_environment",
               side_effect=HTTPException(403, "environment revoked")):
        await batch_runner._run_until_done("rejected-batch", "owner")
    with SessionLocal() as db:
        batch = db.get(BatchPlan, "rejected-batch")
        assert batch.status == "failed", batch.status
        assert batch.progress["environment_error"] == "environment revoked"
        assert not batch.progress.get("results")
        assert batch.progress.get("done", 0) == 0
asyncio.run(run())
engine.dispose()
"""
    env = dict(
        os.environ,
        DATABASE_URL=f"sqlite:///{tmp_path / 'environment.db'}",
        REDIS_URL="",
        JX_CAPABILITIES_ENABLED="false",
        LOCAL_MODE="false",
        SANDBOX_TOOLS_ENABLED="false",
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-7000:]
