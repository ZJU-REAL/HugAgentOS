"""Exercise the real assembly without reading the developer's database."""
import os
import subprocess
import sys


def test_evaluation_factory_and_child_share_one_native_surface(tmp_path):
    script = r'''
import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import core.db.models
import core.sandbox
import core.sandbox.factory
from core.db.engine import Base, engine, SessionLocal
from core.db.models import UserShadow
from core.db.model_repository import create_provider, assign_role
from core.llm.agent_factory import create_agent_executor
from core.llm.builtin_subagents import build_builtin_runtime_profile, get_builtin_subagent

Base.metadata.create_all(engine)
with SessionLocal() as db:
    model = create_provider(db, display_name="mock", provider_type="chat", base_url="http://127.0.0.1:1/v1",
        api_key="test-only", model_name="mock", extra_config={"context_length": 32768})
    assign_role(db, "main_agent", model.provider_id)
    db.add(UserShadow(user_id="owner", username="Owner"))
    db.commit()
session = "eval_" + "a" * 32
class Sandbox:
    name = "opensandbox"
    runs_on_host = False
    def __init__(self):
        self.writes = []
    async def put_file(self, session_id, path, content, user_id):
        self.writes.append((session_id, path, content, user_id))
sandbox = Sandbox()

async def run():
    with patch.object(core.sandbox, "get_sandbox_provider", return_value=sandbox), patch.object(
        core.sandbox.factory, "get_sandbox_provider", return_value=sandbox
    ):
        for chat_id, sandbox_session, isolated in ((session, None, False), (None, session, True)):
            profile = build_builtin_runtime_profile(get_builtin_subagent("builtin.worker"), {}) if isolated else None
            agent, clients = await create_agent_executor(
                current_user_id="owner", chat_id=chat_id, sandbox_session_id=sandbox_session,
                user_agent=profile,
                memory_enabled=True, enabled_mcp_ids=["private"], enabled_skill_ids=["private"],
                project_ctx={"project_name": "OWNER_PRIVATE_PROJECT_SENTINEL"},
                visible_subagents=[{"agent_id": "private", "name": "OWNER_OTHER_AGENT_SENTINEL"},
                                   {"agent_id": "builtin.worker", "name": "Worker"}],
                top_level_chat=True, isolated=isolated, max_iters=1,
            )
            assert clients == []
            schemas = {item["function"]["name"]: item["function"] for item in agent._jx_compaction_tool_schemas}
            assert {"bash", "Read", "Write", "Edit", "Glob", "Grep"} <= schemas.keys(), schemas.keys()
            assert not {"read_chat", "list_related_chats", "load_plugin", "read_artifact",
                        "pin_to_workspace", "sandbox_put_artifact", "ask_user_question"} & schemas.keys()
            assert not any("myspace" in name.lower() for name in schemas)
            assert "evaluation" in schemas["bash"]["description"]
            assert "OWNER_PRIVATE_PROJECT_SENTINEL" not in str(agent._system_prompt)
            assert "OWNER_OTHER_AGENT_SENTINEL" not in str(agent._system_prompt)
            policies = {type(policy).__name__ for adapter in agent._reply_middlewares
                        for policy in getattr(adapter, "legacy_middlewares", ())}
            assert "FinishPinGuardMiddleware" not in policies
            assert agent.offloader is not None
            await agent.offloader.offload_tool_result("framework", SimpleNamespace(output="large " * 15000))
            assert sandbox.writes[-1][0] == session
            assert sandbox.writes[-1][3] == "owner"
asyncio.run(run())
engine.dispose()
'''
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp_path / 'eval.db'}", REDIS_URL="",
               SANDBOX_TOOLS_ENABLED="true", CODE_CAPABILITY_ENABLED="true",
               JX_CAPABILITIES_ENABLED="false", LOCAL_MODE="false")
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, (result.stdout + result.stderr)[-6000:]
