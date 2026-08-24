"""Main streaming route must bind its real run/workspace identity."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.asyncio
async def test_main_streaming_forwards_run_and_workspace_to_agent_factory(monkeypatch):
    from core.llm import builtin_subagents
    from orchestration import workflow

    captured = {}

    class StopAtFactory(Exception):
        pass

    async def _fake_create_agent_executor(**kwargs):
        captured.update(kwargs)
        raise StopAtFactory

    async def _no_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(workflow, "create_agent_executor", _fake_create_agent_executor)
    monkeypatch.setattr(workflow, "launch_memory_retrieval", _no_memory)
    monkeypatch.setattr(workflow, "anchor_start_for_chat", lambda _chat_id: 0)
    monkeypatch.setattr(workflow, "enabled_skill_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(workflow, "enabled_mcp_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(workflow, "enabled_kb_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(workflow, "_resolve_mode_spec", lambda _ctx: None)
    monkeypatch.setattr(
        builtin_subagents, "merge_builtin_subagents", lambda *_a, **_kw: []
    )

    stream = workflow.astream_chat_workflow(
        session_messages=[{"role": "user", "content": "hello"}],
        user_message="hello",
        context={
            "run_id": "run-main-70",
            "workspace_id": "workspace-main-70",
            "chat_id": "chat-main-70",
            "user_id": "user-main-70",
            "memory_enabled": False,
        },
    )

    assert (await anext(stream))["type"] == "thinking"
    with pytest.raises(StopAtFactory):
        await anext(stream)

    assert captured["run_id"] == "run-main-70"
    assert captured["workspace_id"] == "workspace-main-70"


def test_main_streaming_executes_real_factory_and_binds_run_workspace(tmp_path):
    """Fresh process: public streaming path reaches the real factory/binder."""
    database_url = f"sqlite:///{tmp_path / 'main-streaming.db'}"
    script = r"""
import asyncio
import json

import core.db.models  # register the complete metadata before create_all
from core.db.engine import Base, engine
from core.evolution.runtime_binding import reset_for_tests, resolve_bundle_for_run
from core.llm import agent_factory, builtin_subagents
from orchestration import workflow

Base.metadata.create_all(engine)

class EmptySkillLoader:
    def load_all_metadata(self):
        return {}
    def register_skills_to_toolkit(self, *_args, **_kwargs):
        return 0
    def get_skill_dir(self, *_args, **_kwargs):
        return None

agent_factory.get_skill_loader = lambda: EmptySkillLoader()
real_factory = workflow.create_agent_executor

class BoundAfterFactory(Exception):
    pass

async def verifying_factory(**kwargs):
    await real_factory(**kwargs)
    bundle = resolve_bundle_for_run("run-main-real-70")
    assert bundle is not None
    policy = bundle.first_of_kind("memory")
    assert policy is not None
    print(json.dumps({
        "workspace_policy": policy.asset_id,
        "surface_generation": bundle.execution_manifest["surface_generation"],
        "aggregate_hash": bundle.execution_manifest["aggregate_hash"],
    }))
    raise BoundAfterFactory

async def no_memory(*_args, **_kwargs):
    return None

workflow.create_agent_executor = verifying_factory
workflow.launch_memory_retrieval = no_memory
workflow.anchor_start_for_chat = lambda _chat_id: 0
workflow.enabled_skill_ids_from_context = lambda _ctx: []
workflow.enabled_mcp_ids_from_context = lambda _ctx: []
workflow.enabled_kb_ids_from_context = lambda _ctx: []
workflow._resolve_mode_spec = lambda _ctx: None
builtin_subagents.merge_builtin_subagents = lambda *_args, **_kwargs: []

async def main():
    reset_for_tests()
    stream = workflow.astream_chat_workflow(
        session_messages=[{"role": "user", "content": "hello"}],
        user_message="hello",
        context={
            "run_id": "run-main-real-70",
            "workspace_id": "workspace-main-real-70",
            "chat_id": "chat-main-real-70",
            "user_id": "user-main-real-70",
            "memory_enabled": False,
        },
    )
    assert (await anext(stream))["type"] == "thinking"
    try:
        await anext(stream)
    except BoundAfterFactory:
        return
    raise AssertionError("real factory did not reach the runtime binder")

asyncio.run(main())
"""
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": database_url,
            "REDIS_URL": "memory://",
            "SANDBOX_TOOLS_ENABLED": "false",
        }
    )
    output = subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        env=env,
        text=True,
    )
    payload = json.loads(
        next(line for line in reversed(output.splitlines()) if line.startswith("{"))
    )

    assert payload["workspace_policy"] == "policy:workspace-main-real-70"
    assert payload["surface_generation"] == 1
    assert len(payload["aggregate_hash"]) == 64


def test_real_factory_public_reply_rebinds_or_fails_closed(tmp_path):
    """The public factory/reply/resolver seams expose only final request evidence."""
    database_url = f"sqlite:///{tmp_path / 'context-request-binding.db'}"
    script = r"""
import asyncio
import json

import core.db.models
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, ChatUsage
from core.db.engine import Base, engine, SessionLocal
from core.db.models.evolution import EvolutionEpisode
from core.evolution import runtime_binding as rb
from core.evolution import trace_assembler
from core.llm import agent_factory
from core.llm.agentscope_hook_adapter import AgentScopeHookAdapter

Base.metadata.create_all(engine)

class EmptySkillLoader:
    def load_all_metadata(self):
        return {}
    def register_skills_to_toolkit(self, *_args, **_kwargs):
        return 0
    def get_skill_dir(self, *_args, **_kwargs):
        return None

class CaptureModel:
    model = "capture-model"
    context_size = 32768
    def __init__(self):
        self.calls = []
    async def __call__(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        return ChatResponse(
            content=[TextBlock(type="text", text="done")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01),
        )
    async def count_tokens(self, messages, tools=None):
        return 1

agent_factory.get_skill_loader = lambda: EmptySkillLoader()

async def build(run_id, project_ctx=None):
    agent, clients = await agent_factory.create_agent_executor(
        disable_tools=True,
        run_id=run_id,
        workspace_id="workspace-context-71",
        max_iters=1,
        project_ctx=project_ctx,
    )
    model = CaptureModel()
    agent.model = model
    agent.state.model_pinned = True
    mounted = {
        id(middleware): middleware
        for attr in (
            "_reply_middlewares",
            "_reasoning_middlewares",
            "_acting_middlewares",
            "_model_call_middlewares",
        )
        for middleware in getattr(agent, attr, [])
    }
    assert mounted and all(
        isinstance(middleware, AgentScopeHookAdapter)
        for middleware in mounted.values()
    )
    return agent, model, clients

async def main():
    rb.reset_for_tests()
    success, capture, success_clients = await build(
        "run-context-success-71",
        project_ctx={
            "project_id": "project-context-71",
            "project_name": "Context Project",
            "project_instructions": "PROJECT-IR-MARKER-71",
            "project_files": [],
        },
    )
    await success.reply(UserMsg(name="custom-user", content="hello final context"))
    success_bundle = rb.resolve_bundle_for_run("run-context-success-71")
    assert success_bundle is not None and success_bundle.partial is False
    final_manifest = success_bundle.execution_manifest
    assert final_manifest["context_manifest_hash"]
    assert final_manifest["context_manifest"]["included"][-1]["kind"] == "user_input"
    assert capture.calls[0]["messages"][-1].get_text_content() == "hello final context"
    project_messages = [
        message
        for message in capture.calls[0]["messages"]
        if "PROJECT-IR-MARKER-71" in message.get_text_content()
    ]
    assert len(project_messages) == 1
    assert project_messages[0].metadata["harness_context_items"][0]["kind"] == "project_material"
    assert any(
        entry["kind"] == "project_material"
        for entry in final_manifest["context_manifest"]["included"]
    )
    assert "custom-user" not in str(final_manifest)
    assert "PROJECT-IR-MARKER-71" not in str(final_manifest)

    failed, _failed_capture, failed_clients = await build("run-context-failed-71")
    def fail_rebind(**_kwargs):
        raise RuntimeError("simulated evidence store failure")
    rb.rebind_execution_manifest = fail_rebind
    await failed.reply(UserMsg(name="user", content="still answer the user"))
    assert rb.resolve_bundle_for_run("run-context-failed-71") is None

    episode_id = trace_assembler.assemble_episode(
        message_id="message-context-failed-71",
        run_id="run-context-failed-71",
        bundle=rb.resolve_bundle_for_run("run-context-failed-71"),
    )
    with SessionLocal() as db:
        episode = db.query(EvolutionEpisode).filter_by(episode_id=episode_id).one()
        rejection = trace_assembler.replay_rejection_reason(episode)
        assert rejection == "asset_bundle_incomplete"

    for client in [*success_clients, *failed_clients]:
        await client.close()
    print(json.dumps({
        "success_context_hash": final_manifest["context_manifest_hash"],
        "failed_resolver": None,
        "failed_episode": rejection,
    }))

asyncio.run(main())
"""
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": database_url,
            "REDIS_URL": "memory://",
            "SANDBOX_TOOLS_ENABLED": "false",
        }
    )
    output = subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        env=env,
        text=True,
    )
    payload = json.loads(
        next(line for line in reversed(output.splitlines()) if line.startswith("{"))
    )

    assert len(payload["success_context_hash"]) == 64
    assert payload["failed_resolver"] is None
    assert payload["failed_episode"] == "asset_bundle_incomplete"
