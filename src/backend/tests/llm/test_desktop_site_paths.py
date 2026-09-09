
import ntpath
import pytest

@pytest.mark.parametrize("path,allowed", [
    ("C:/Users/Aaron/workspace/site", True),
    (r"C:\Users\Aaron\workspace\site", True),
    ("c:/users/aaron/WORKSPACE/site/", True),
    ("C:/Users/Aaron/workspace-other/site", False),
    ("C:/Users/Aaron/workspace/../outside", False),
    ("C:/Users/Aaron/workspace/..\\outside", False),
    ("D:/Users/Aaron/workspace/site", False),
])
def test_windows_publish_boundary(monkeypatch, path, allowed):
    from core.sandbox import _common
    from core.llm.tools._tool_helpers import _validate_workspace_path
    monkeypatch.setattr(_common, "WORKSPACE", "C:/Users/Aaron/workspace")
    assert (_validate_workspace_path(path) is None) == allowed


def test_portable_archive_enforces_contents_and_limits(tmp_path):
    from core.sandbox.directory_archive import pack_directory
    from core.services.site_packaging import safe_extract_tar
    source=tmp_path/"source"
    source.mkdir()
    (source/"index.html").write_bytes(b"<html>test</html>")
    (source/"node_modules").mkdir()
    (source/"node_modules"/"ignored").write_bytes(b"ignored")
    target=tmp_path/"bundle.tgz"
    options=dict(source=source,target=target,excludes=["node_modules"],max_files=10,
                 max_file_bytes=1024,max_total_bytes=1024,max_archive_bytes=2048)
    pack_directory(**options)
    assert safe_extract_tar(target.read_bytes()) == [("index.html", b"<html>test</html>")]
    with pytest.raises(ValueError):
        pack_directory(**{**options,"max_file_bytes":1})
    assert not target.exists()

def test_portable_archive_rejects_links(tmp_path):
    from core.sandbox.directory_archive import pack_directory
    source=tmp_path/"source"
    source.mkdir()
    (tmp_path/"outside").write_text("private")
    (source/"link").symlink_to(tmp_path/"outside")
    with pytest.raises(ValueError,match="linked"):
        pack_directory(source,tmp_path/"bundle.tgz",[],10,1024,1024,2048)

def test_native_file_alias_matches_command_session(tmp_path, monkeypatch):
    from core.llm.tools import _paths
    from core.config import local_mode
    from services.script_runner_service import server
    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    monkeypatch.setattr(_paths, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    target=_paths.to_physical_path("/workspace/site/index.html", None, session_id="chat-site")
    assert target == str(server._session_workspace("chat-site")/"site/index.html")
    assert not server._session_workspace("chat-site").exists()

def test_physical_session_identity_is_idempotent():
    from services.script_runner_service.workspace_paths import resolve_path
    root = "C:/Users/Aaron/workspace"
    path = resolve_path("/workspace/site",root,"chat")
    assert resolve_path(path,root,"chat") == path
    assert resolve_path(path,root,"other-chat") == path

def test_publish_uses_real_runner_and_same_session_files(tmp_path, monkeypatch):
    import asyncio
    import base64
    from services.script_runner_service import server
    from core.services import site_packaging
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path))
    workspace = server._session_workspace("site-chat",create=True)
    (workspace/"site").mkdir()
    (workspace/"site/index.html").write_bytes(b"<html>same session</html>")
    class Provider:
        async def execute(self, request):
            return await server.execute(server.ExecuteRequest(
                script_content=request.script_content,script_name=request.script_name,
                language=request.language,session_id=request.session_id,
                params=request.params,user_id=request.user_id,
            ))
        async def get_file(self, session, path, user_id=None):
            result=await server.get_file(server.GetFileRequest(session_id=session,path=path))
            return base64.b64decode(result.content_b64)
    monkeypatch.setattr("core.sandbox.get_sandbox_provider",lambda:Provider())
    files,error=asyncio.run(site_packaging.pack_and_fetch_dir(str(workspace/"site"),"site-chat",""))
    assert error is None
    assert files == [("index.html",b"<html>same session</html>")]
    assert not list(workspace.glob(".__site_pack_*"))

async def test_write_permission_and_bash_share_the_same_file(tmp_path, monkeypatch):
    from core.llm.tools import _paths
    from core.config import local_mode
    from services.script_runner_service import server
    from core.llm.tools.write_tool import register_write
    from core.llm.tools._state import ReadStateTracker
    from core.llm.tool_permissions import CURRENT_PERMISSION_TICKET
    from core.sandbox.local_policy import Grant, Policy
    from tests.llm.test_local_file_permission import _Toolkit, _service, _call, _payload
    monkeypatch.setattr(local_mode,"local_mode_enabled",lambda:True)
    monkeypatch.setattr(_paths,"WORKSPACE_ROOT",str(tmp_path))
    monkeypatch.setattr(server,"WORKSPACE_ROOT",str(tmp_path))
    monkeypatch.setattr("core.services.local_grant_service.grants_for_gate",lambda:[Grant(str(tmp_path),"readwrite")])
    monkeypatch.setattr("core.services.local_grant_service.policy_for_gate",lambda *args:Policy())
    monkeypatch.setattr("core.services.local_snapshot_service.maybe_snapshot_local",lambda path:None)
    from core.sandbox.script_runner_provider import ScriptRunnerProvider
    monkeypatch.setattr("core.sandbox.get_sandbox_provider",lambda:ScriptRunnerProvider())
    toolkit=_Toolkit()
    register_write(toolkit,chat_id="chat-1",user_id="user-1",state=ReadStateTracker(),interactive=False)
    outcome=await _service("Write",interactive=False).authorize(_call("Write",{"file_path":"/workspace/site/index.html","content":"same-file"}))
    assert outcome.proceed
    token=CURRENT_PERMISSION_TICKET.set(outcome.ticket)
    try:
        response=await toolkit.fn(file_path="/workspace/site/index.html",content="same-file")
    finally:
        CURRENT_PERMISSION_TICKET.reset(token)
    assert _payload(response).get("ok"),_payload(response)
    result=await server.execute(server.ExecuteRequest(session_id="chat-1",user_id="user-1",language="bash",script_name="read.sh",script_content='IFS= read -r text < /workspace/site/index.html; printf "%s" "$text"'))
    assert result.exit_code == 0,result.stderr
    assert result.stdout.strip()=="same-file"
