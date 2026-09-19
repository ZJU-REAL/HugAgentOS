"""A desktop project publishes its real build directory without a cloud folder."""
import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import ChatSession, Project, UserShadow
from core.services.desktop_site_publish import package_local_site
from core.services.site_packaging import safe_extract_tar


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'local.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    monkeypatch.setenv("SCRIPT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    root = tmp_path / "Desktop" / "测试站点"
    root.mkdir(parents=True)
    (root / "index.html").write_text("<h1>desktop build</h1>")
    with factory() as db:
        db.add(UserShadow(user_id="owner", username="owner"))
        db.add(Project(project_id="project", name="Desktop", kind="local",
                       owner_user_id="owner", extra_data={"local": {"path": str(root), "slug": "desktop"}}))
        db.add(ChatSession(chat_id="chat", user_id="owner", project_id="project"))
        db.commit()
    yield root, factory
    engine.dispose()


def test_bound_desktop_project_packages_build_without_cloud_folder(local_project, monkeypatch):
    root, _ = local_project
    seen = []

    async def pack(src, session_id, user_id, **kwargs):
        seen.append((src, session_id, user_id))
        return [("index.html", (root / "index.html").read_bytes())], None

    monkeypatch.setattr("core.services.site_packaging.pack_and_fetch_dir", pack)
    data, options = asyncio.run(package_local_site(
        {"src_dir": str(root), "title": "Desktop"},
        {"X-Current-User-Id": "owner", "X-Conversation-Id": "chat"},
    ))
    assert seen == [(str(root), "chat", "owner")]
    assert safe_extract_tar(data) == [("index.html", b"<h1>desktop build</h1>")]
    assert json.loads(options)["title"] == "Desktop"


@pytest.mark.parametrize("src_dir", ["", "."])
def test_local_publish_requires_explicit_directory(local_project, monkeypatch, src_dir):
    root, _ = local_project
    async def pack(*args, **kwargs):
        pytest.fail("must reject before packing")
    monkeypatch.setattr("core.services.site_packaging.pack_and_fetch_dir", pack)
    with pytest.raises(ValueError, match="src_dir"):
        asyncio.run(package_local_site(
            {"src_dir": src_dir}, {"x-current-user-id": "owner", "x-chat-id": "chat"}))


@pytest.mark.parametrize("change,status", [("other-user", 403), ("missing-root", 409),
                                          ("relative-root", 409), ("cloud", 403),
                                          ("deleted-project", 409)])
def test_invalid_project_cannot_publish(local_project, monkeypatch, change, status):
    from fastapi import HTTPException
    from datetime import datetime
    root, factory = local_project
    user = "owner"
    with factory() as db:
        project = db.get(Project, "project")
        if change == "other-user":
            user = "stranger"
        elif change == "missing-root":
            project.extra_data = {"local": {"path": str(root / "gone")}}
        elif change == "relative-root":
            project.extra_data = {"local": {"path": "relative"}}
        elif change == "deleted-project":
            project.deleted_at = datetime.utcnow()
        elif change == "cloud":
            monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
        db.commit()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(package_local_site(
            {"src_dir": str(root)}, {"x-current-user-id": user, "x-chat-id": "chat"}))
    assert exc.value.status_code == status


@pytest.mark.parametrize("escape", ["sibling", "symlink"])
def test_publish_rejects_paths_outside_bound_project(local_project, monkeypatch, escape):
    root, _ = local_project
    outside = root.parent / (root.name + "-other")
    outside.mkdir()
    source = outside
    if escape == "symlink":
        source = root / "escape"
        source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="站点目录必须位于") as caught:
        asyncio.run(package_local_site(
            {"src_dir": str(source)}, {"x-current-user-id": "owner", "x-chat-id": "chat"}))
    # 报错要说出项目根在哪：只说「不在范围内」模型就只能一条条猜路径。
    assert str(root) in str(caught.value)


def test_unbound_chat_publishes_from_its_session_directory(local_project, tmp_path, monkeypatch):
    """没绑项目时边界是这个会话的工作目录，发布照常走通。"""
    from core.db.models import ChatSession

    _root, factory = local_project
    with factory() as db:
        db.get(ChatSession, "chat").project_id = None
        db.commit()

    from services.script_runner_service.workspace_paths import session_root

    workspace = tmp_path / "workspace"
    monkeypatch.setattr("core.sandbox._common.WORKSPACE", str(workspace))
    site = Path(session_root(str(workspace), "chat")) / "sites" / "demo"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<h1>default workspace</h1>")

    async def pack(src, session_id, user_id, **kwargs):
        return [("index.html", (site / "index.html").read_bytes())], None

    monkeypatch.setattr("core.services.site_packaging.pack_and_fetch_dir", pack)
    data, _options = asyncio.run(package_local_site(
        {"src_dir": str(site)}, {"x-current-user-id": "owner", "x-chat-id": "chat"}))
    assert safe_extract_tar(data) == [("index.html", b"<h1>default workspace</h1>")]


def test_unbound_chat_still_cannot_publish_outside_the_workspace(local_project, tmp_path, monkeypatch):
    """工作目录之外的路径发布不了。"""
    from core.db.models import ChatSession

    _root, factory = local_project
    with factory() as db:
        db.get(ChatSession, "chat").project_id = None
        db.commit()
    monkeypatch.setattr("core.sandbox._common.WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(exist_ok=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    async def pack(*args, **kwargs):
        pytest.fail("越界时不该走到打包")

    monkeypatch.setattr("core.services.site_packaging.pack_and_fetch_dir", pack)
    with pytest.raises(ValueError):
        asyncio.run(package_local_site(
            {"src_dir": str(outside)}, {"x-current-user-id": "owner", "x-chat-id": "chat"}))


def test_bound_project_archive_uses_real_runner(local_project, tmp_path, monkeypatch):
    import base64
    from services.script_runner_service import server
    root, _ = local_project
    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr("core.llm.tools._paths.WORKSPACE_ROOT", str(tmp_path / "workspace"))
    class Provider:
        async def execute(self, request):
            return await server.execute(server.ExecuteRequest(
                script_content=request.script_content, script_name=request.script_name,
                language=request.language, session_id=request.session_id,
                params=request.params, user_id=request.user_id))
        async def get_file(self, session, path, user_id=None):
            result = await server.get_file(server.GetFileRequest(session_id=session, path=path))
            return base64.b64decode(result.content_b64)
    monkeypatch.setattr("core.sandbox.get_sandbox_provider", lambda: Provider())
    data, _ = asyncio.run(package_local_site(
        {"src_dir": str(root)}, {"x-current-user-id": "owner", "x-chat-id": "chat"}))
    assert safe_extract_tar(data) == [("index.html", b"<h1>desktop build</h1>")]
    assert not list((tmp_path / "workspace").rglob(".__site_pack_*"))


@pytest.mark.parametrize("path,allowed", [
    ("D:/Desktop/站点/dist", True),
    ("d:/desktop/站点/DIST", True),
    ("D:/Desktop/站点-other/dist", False),
    ("D:/Desktop/站点/../private", False),
    ("E:/Desktop/站点/dist", False),
])
def test_windows_bound_directory_containment(monkeypatch, path, allowed):
    from core.sandbox import _common
    from core.llm.tools._tool_helpers import _validate_workspace_path
    monkeypatch.setattr(_common, "WORKSPACE", "C:/managed/workspace")
    assert (_validate_workspace_path(path, additional_roots=("D:/Desktop/站点",)) is None) == allowed


def test_boundary_is_the_session_directory_not_the_workspace_root(local_project, tmp_path, monkeypatch):
    """边界是这个会话的工作目录，工作区根下的别处不放行。"""
    from core.db.models import ChatSession
    from services.script_runner_service.workspace_paths import session_root

    _root, factory = local_project
    with factory() as db:
        db.get(ChatSession, "chat").project_id = None
        db.commit()

    workspace = tmp_path / "workspace"
    monkeypatch.setattr("core.sandbox._common.WORKSPACE", str(workspace))
    session_dir = Path(session_root(str(workspace), "chat"))

    inside = session_dir / "sites" / "demo"
    inside.mkdir(parents=True)
    (inside / "index.html").write_text("<h1>in session</h1>")
    outside = workspace / "sites" / "demo"
    outside.mkdir(parents=True)
    (outside / "index.html").write_text("<h1>workspace root</h1>")

    async def pack(src, session_id, user_id, **kwargs):
        return [("index.html", (Path(src) / "index.html").read_bytes())], None

    monkeypatch.setattr("core.services.site_packaging.pack_and_fetch_dir", pack)

    data, _ = asyncio.run(package_local_site(
        {"src_dir": str(inside)}, {"x-current-user-id": "owner", "x-chat-id": "chat"}))
    assert safe_extract_tar(data) == [("index.html", b"<h1>in session</h1>")]

    with pytest.raises(ValueError):
        asyncio.run(package_local_site(
            {"src_dir": str(outside)}, {"x-current-user-id": "owner", "x-chat-id": "chat"}))
