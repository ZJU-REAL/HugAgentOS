"""Exercise local QR login with a real subprocess and isolated cookie storage."""
import json
import os
import sys
from pathlib import Path

import pytest
from core.config.settings import settings
from integrations.yida.service import YidaService


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture; Windows uses bundled Bash")
async def test_local_yida_login_persists_and_disconnects(tmp_path, monkeypatch):
    monkeypatch.setattr(type(settings.deploy), "is_local", property(lambda self: True))
    monkeypatch.setattr(type(settings.storage), "root", property(lambda self: tmp_path / "storage with spaces"))
    binary = tmp_path / "bin" / "openyida"
    binary.parent.mkdir()
    binary.write_text("#!" + sys.executable + "\n" + """
import json, sys
from pathlib import Path
cache=Path.cwd()/".cache"
cache.mkdir(exist_ok=True)
if "--agent-qr" in sys.argv:
    print(json.dumps({"status":"need_qr_scan","qr_url":"https://auth.example/qr",
        "session_file":str(cache/"session.json")}))
elif "--agent-poll" in sys.argv:
    assert sys.argv[sys.argv.index("--agent-poll")+1] == str(cache/"session.json")
    (cache/"cookies-public.json").write_text(json.dumps({"cookies": []}))
    print(json.dumps({"status":"ok","corp_id":"test-corp","user_id":"test-user"}))
""")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    from core.infra.ephemeral import LocalEphemeralState
    memory = LocalEphemeralState()
    monkeypatch.setattr('integrations.yida.service.get_ephemeral_state', lambda: memory)
    def no_remote_provider():
        raise AssertionError("本机登录不应访问容器沙箱")
    monkeypatch.setattr("core.sandbox.factory.get_sandbox_provider", no_remote_provider)
    service = YidaService()
    assert (await service.start_login("local-owner"))["status"] == "pending"
    assert (await service.poll_login("local-owner"))["status"] == "connected"
    assert (await service.get_status("local-owner"))["status"] == "connected"
    assert (await service.get_status("other-owner"))["status"] == "disconnected"
    cookie = settings.storage.root / "yida_cache/local-owner/workspace/.cache/cookies-public.json"
    assert cookie.exists()
    assert (await service.disconnect("local-owner"))["status"] == "disconnected"
    assert not cookie.exists()


async def test_local_skill_uses_login_workspace(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.llm.tools.skill_tool import register_sandboxed_view_text_file
    monkeypatch.setattr(type(settings.deploy), "is_local", property(lambda self: True))
    monkeypatch.setattr(type(settings.storage), "root", property(lambda self: tmp_path / "data with spaces"))
    file = tmp_path / "login.md"
    file.write_text("cd /home/ubuntu/yida-workspace && openyida login --agent-qr")
    class Tools:
        def append(self, tool):
            function = tool._func
            kwargs = {"func_description": tool.description, "permission": getattr(tool, "dispatch_spec", None)}
            self.read = function
    tools = Tools()
    register_sandboxed_view_text_file(tools, [str(tmp_path)], SimpleNamespace(), user_id="owner")
    result = await tools.read(str(file))
    text = result.content[0].text
    assert "/home/ubuntu/yida-workspace" not in text
    assert "'"+(settings.storage.root / "yida_cache/owner/workspace").as_posix()+"'" in text

@pytest.mark.skipif(not os.getenv("HUGAGENT_TEST_PLUGIN_RUNTIME"), reason="Requires staged offline runtime")
async def test_bundled_real_openyida_qr_entry(tmp_path, monkeypatch):
    """Real locked OpenYida and private Node, using its offline QR fixture."""
    import integrations.yida.execution as execution
    import integrations.yida.service as service
    from core.infra.ephemeral import LocalEphemeralState
    root = Path(os.environ["HUGAGENT_TEST_PLUGIN_RUNTIME"]).resolve()
    python = root / ("python/python.exe" if os.name == "nt" else "python/bin/python")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(type(settings.deploy), "is_local", property(lambda self: True))
    monkeypatch.setattr(execution, "workspace_dir", lambda uid: tmp_path / "workspace with spaces" / uid)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("OPENYIDA_CODEX_QR_FAKE", "1")
    memory = LocalEphemeralState()
    monkeypatch.setattr(service, "get_ephemeral_state", lambda: memory)
    result = await YidaService().start_login("owner")
    assert result["status"] == "pending"
    assert result["qr_data_uri"].startswith("data:image/")
    pending = await service._get_pending("owner")
    assert service._valid_session_file("owner", pending["session_file"])
    assert (await YidaService().disconnect("owner"))["status"] == "disconnected"
    assert await memory.get(service._pending_key("owner")) is None


@pytest.mark.parametrize("corruption", ["path_escape", "changed_script"])
async def test_corrupt_bundled_runtime_never_uses_host_cli(tmp_path, monkeypatch, corruption):
    import hashlib
    root = tmp_path / "runtime"
    binary_dir = root / "python/bin"
    binary_dir.mkdir(parents=True)
    node = root / "node"
    script = root / "yida.js"
    utils = root / "utils.js"
    for file in (node, script, utils):
        file.write_text("fixture")
    manifest = {"schema": 1, "node": "node", "openyida": {
        "script": "yida.js", "utils": "utils.js",
        "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}}
    if corruption == "path_escape":
        outside = tmp_path / "outside"
        outside.write_text("fixture")
        manifest["node"] = "../outside"
    else:
        script.write_text("changed")
    (root / "plugin-clis.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(sys, "executable", str(binary_dir / "python"))
    monkeypatch.setattr(type(settings.deploy), "is_local", property(lambda self: True))
    result = await YidaService().start_login("owner")
    assert result["status"] == "error"
    assert "运行时损坏" in result["error"]
