"""Check the actual assembled tool surface in local and cloud modes."""

import os
from pathlib import Path
import subprocess
import sys


def test_factory_delivery_tools_match_execution_mode(tmp_path):
    script = r"""
import asyncio
from core.db.engine import Base, engine
import core.db.models
from core.llm import agent_factory
from core.config import local_mode
Base.metadata.create_all(engine)
class EmptyLoader:
    def load_all_metadata(self): return {}
    def get_skill_dir(self, *args): return None
    def register_skills_to_toolkit(self, *args, **kwargs): return 0
agent_factory.get_skill_loader = lambda: EmptyLoader()
async def main():
    for local in (True, False):
        local_mode.local_mode_enabled = lambda: local
        agent, clients = await agent_factory.create_agent_executor(
            enabled_skill_ids=[], enabled_mcp_ids=[], enabled_kb_ids=[],
            max_iters=1, chat_id="delivery-tools", approval_mode="full",
        )
        surface = await agent.toolkit.freeze_execution_surface()
        names = {schema["function"]["name"] for schema in surface.tool_schemas}
        assert "pin_to_workspace" in names, names
        assert ("sandbox_get_artifact" in names) is (not local), names
        for client in clients: await client.close()
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "DATABASE_URL": f"sqlite:///{tmp_path / 'factory.db'}",
            "REDIS_URL": "",
            "SANDBOX_TOOLS_ENABLED": "true",
            "HUGAGENT_CAPS_ROOT": str(tmp_path / "caps"),
        },
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
