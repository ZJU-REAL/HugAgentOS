"""CE process registration must match the launcher's deployed port contract."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("profile", ["local", "cloud"])
@pytest.mark.parametrize("offset", [0, 1000])
def test_ce_registry_launcher_and_ports_agree(profile, offset, tmp_path):
    backend = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "PYTHONPATH": str(backend),
        "JX_EDITION": "ce",
        "DEPLOY_PROFILE": profile,
        "HUGAGENT_LOCAL_MCP_PORT_OFFSET": str(offset),
        "HUGAGENT_HOME": str(tmp_path),
        "DATABASE_URL": "sqlite:///:memory:",
        "REDIS_URL": "",
        "STORAGE_TYPE": "local",
    }
    code = """
import os
from extensions.mcp_servers.registry import SERVERS, process_servers
from extensions.mcp_servers._ports import PORTS
from extensions.mcp_servers._launcher import PORTS as launcher_ports

excluded = {"query_database", "ai_chain_information_mcp", "security_ops"}
assert excluded.isdisjoint(SERVERS)
assert excluded.isdisjoint(PORTS)
assert {"query_database_mcp", "ai_chain_information_mcp", "security_ops_mcp"}.isdisjoint(launcher_ports)
expected = {
    "retrieve_dataset_content": 9100, "internet_search": 9102,
    "web_fetch": 9106, "automation_task": 9108, "skill_manager": 9112,
    "site_publish": 9113, "agent_manager": 9115, "plugin_manager": 9116,
    "browser_runtime": 9117,
}
offset = int(os.environ["HUGAGENT_LOCAL_MCP_PORT_OFFSET"])
assert PORTS == {key: port + offset for key, port in expected.items()}
servers = process_servers(os.environ["DEPLOY_PROFILE"])
assert {server.server_id for server in servers} == set(expected)
assert launcher_ports == {server.package: expected[server.server_id] + offset for server in servers}
assert "space_files_mcp" not in launcher_ports
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=backend, env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
