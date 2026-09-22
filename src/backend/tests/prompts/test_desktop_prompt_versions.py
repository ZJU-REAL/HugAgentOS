"""Desktop versions use real CRUD; previews never activate drafts."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from core.db.models import ContentBlock
from core.services import prompt_version_service as pvs
from prompts.desktop_templates import render_desktop_part, desktop_version

@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://")
    ContentBlock.__table__.create(engine)
    pvs.invalidate_cache()
    with Session(engine) as session:
        monkeypatch.setattr(pvs, "SessionLocal", lambda: Session(engine))
        yield session
    pvs.invalidate_cache()
    engine.dispose()


def test_seed_clone_activate_disable_and_preserve(db, monkeypatch):
    from prompts.prompt_runtime import build_system_prompt, invalidate_prompt_cache
    from prompts.prompt_config import PromptConfig
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    pvs.seed_from_filesystem(db=db)
    assert pvs.get_active_version("desktop", db=db)["id"] == "default"
    original = render_desktop_part("guidance")
    pvs.upsert_version("desktop", "draft", from_id="default", db=db)
    draft = pvs.get_version("desktop", "draft", db=db)
    for part in draft["parts"]:
        if part["part_id"] == "guidance":
            part["content"] = "UNIQUE_DESKTOP_VERSION"
    pvs.upsert_version("desktop", "draft", parts=draft["parts"], db=db)
    assert render_desktop_part("guidance") == original
    invalidate_prompt_cache()
    before = build_system_prompt(PromptConfig(), {"chat_id": "version-test"})
    pvs.activate_version("desktop", "draft", db=db)
    after = build_system_prompt(PromptConfig(), {"chat_id": "version-test"})
    assert "UNIQUE_DESKTOP_VERSION" not in before
    assert "UNIQUE_DESKTOP_VERSION" in after
    pvs.seed_from_filesystem(db=db)
    assert render_desktop_part("guidance") == "UNIQUE_DESKTOP_VERSION"
    with desktop_version({"parts": [{"part_id": "guidance", "content": "hidden", "is_enabled": False}]}):
        assert render_desktop_part("guidance") == ""
        assert render_desktop_part("project") == ""
    assert render_desktop_part("guidance") == "UNIQUE_DESKTOP_VERSION"


@pytest.mark.asyncio
async def test_full_preview_does_not_activate_version(db, monkeypatch):
    from api.routes.v1.prompt_management import desktop_preview, DesktopPreviewRequest
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    pvs.seed_from_filesystem(db=db)
    pvs.upsert_version("desktop", "draft", from_id="default", db=db)
    data = (await desktop_preview(DesktopPreviewRequest(version_id="draft"), db=db))["data"]
    assert data["environment_source"] == "runtime_placeholders"
    assert "{runtime.os}" in data["prompt"]
    assert "本机文件与执行" in data["prompt"]
    assert "没 pin = 用户看不到" not in data["prompt"]
    assert "{runtime.cwd}" in data["bash_tool"]
    assert "project" in data["conditional_parts"]
    assert pvs.get_active_version("desktop", db=db)["id"] == "default"


def test_database_failure_uses_filesystem(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(pvs, "get_active_version", fail)
    assert "本机文件与执行" in render_desktop_part("guidance")


@pytest.mark.asyncio
async def test_local_preview_uses_runtime_assembly_with_selected_version(db, monkeypatch):
    from api.routes.v1 import prompt_management as admin_prompts
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    pvs.seed_from_filesystem(db=db)
    pvs.upsert_version("desktop", "draft", parts=[{"part_id": "guidance", "content": "SELECTED_DRAFT"}], db=db)
    async def runtime(_, **kwargs):
        assert kwargs["approval_mode"] == "ask"
        return {"prompt": "RUNTIME_TOOLS\n" + render_desktop_part("guidance")}
    monkeypatch.setattr(admin_prompts, "_runtime_prompt_preview", runtime)
    data = (await admin_prompts.desktop_preview(admin_prompts.DesktopPreviewRequest(version_id="draft"), db=db))["data"]
    assert data["preview_mode"] == "runtime"
    assert data["prompt"] == "RUNTIME_TOOLS\nSELECTED_DRAFT"
    assert "SELECTED_DRAFT" not in render_desktop_part("guidance")


@pytest.mark.asyncio
async def test_preview_unsaved_parts_and_snapshot_roundtrip(db, monkeypatch):
    from api.routes.v1 import prompt_management as api
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    pvs.seed_from_filesystem(db=db)
    before = api.export_snapshot(db=db)["data"]
    data = (await api.desktop_preview(api.DesktopPreviewRequest(version_id="default", parts=[
        api.PromptPartPayload(part_id="guidance", content="UNSAVED_PREVIEW")]), db=db))["data"]
    assert "UNSAVED_PREVIEW" in data["prompt"]
    assert "UNSAVED_PREVIEW" not in render_desktop_part("guidance")
    api.import_snapshot(before, db=db)
    after = api.export_snapshot(db=db)["data"]["blocks"]
    assert {k: v["payload"] for k, v in after.items()} == {k: v["payload"] for k, v in before["blocks"].items()}


def test_shared_management_routes_require_system_settings():
    from api.routes.v1.prompt_management import router
    from api.deps import require_system_settings
    from fastapi.routing import APIRoute
    for route in router.routes:
        if isinstance(route, APIRoute):
            assert any(dep.call is require_system_settings for dep in route.dependant.dependencies), route.path


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id,allowed,status", [(None, False, 401), ("member", False, 403), ("owner", True, 200)])
async def test_management_permission_gate(db, monkeypatch, user_id, allowed, status):
    from types import SimpleNamespace
    from starlette.requests import Request
    from fastapi import HTTPException
    from api import deps
    monkeypatch.setattr(deps, "user_can_manage_system_settings", lambda db, user_id: allowed)
    monkeypatch.setattr(deps, "AuditLogRepository", lambda db: SimpleNamespace(log_denial=lambda **kwargs: None))
    request = Request({"type": "http", "method": "GET", "path": "/v1/admin/prompts/kinds", "headers": []})
    user = SimpleNamespace(user_id=user_id) if user_id else None
    if status == 200:
        assert await deps.require_system_settings(request, db=db, user=user) == user_id
    else:
        with pytest.raises(HTTPException) as error:
            await deps.require_system_settings(request, db=db, user=user)
        assert error.value.status_code == status


def test_desktop_seed_has_no_site_workflow_parts(db):
    pvs.seed_from_filesystem(db=db)
    version = pvs.get_active_version("desktop", db=db)
    parts = {p["part_id"]: p["content"] for p in version["parts"]}
    assert not {"site_mode", "site_records", "site_lookup_failed"} & parts.keys()
    assert "publish_site" not in parts["project"]
    assert "project_missing" in parts


def test_site_prompt_migration_is_scoped_and_idempotent(db):
    from copy import deepcopy
    from core.db.data_repair import remove_desktop_site_prompt_parts

    legacy = (
        "建站在本地项目真实目录下的 sites/<站点名>/ 编写源码。"
        "编辑已有站点先读取当前站点的 source_dir 并原地修改；"
        "构建后 publish_site 的 src_dir 指向构建产物、source_dir 指向项目源码。"
    )
    versions = [
        {"kind": "desktop", "id": "default", "parts": [
            {"part_id": "site_records", "content": "{records}"},
            {"part_id": "site_mode", "content": "old site rules"},
            {"part_id": "site_lookup_failed", "content": "old failure"},
            {"part_id": "project", "content": "before\n" + legacy + "\n越出授权目录的操作与危险命令受本机权限策略约束；文件工具修改前的快照可用于回滚。\nafter\n"},
            {"part_id": "project_missing", "content": "keep missing path"},
        ]},
        {"kind": "desktop", "id": "custom", "parts": [
            {"part_id": "project", "content": "custom publish_site instructions"},
            {"part_id": "guidance", "content": "keep guidance", "is_enabled": False},
        ]},
        {"kind": "system", "id": "default", "parts": [
            {"part_id": "site_mode", "content": "keep unrelated kind"}
        ]},
    ]
    payload = {"versions": versions, "active": {"desktop": "custom"}, "extra": "keep"}
    original = deepcopy(payload)
    db.add(ContentBlock(id="prompt_versions", payload=payload))
    db.commit()
    assert remove_desktop_site_prompt_parts(db.connection()) == 1
    assert remove_desktop_site_prompt_parts(db.connection()) == 0
    db.expire_all()
    result = db.get(ContentBlock, "prompt_versions").payload
    assert result["active"] == original["active"]
    assert result["extra"] == "keep"
    assert result["versions"][1:] == original["versions"][1:]
    assert result["versions"][0]["parts"] == [
        {"part_id": "project", "content": "before\nafter\n"},
        {"part_id": "project_missing", "content": "keep missing path"},
    ]


def test_site_prompt_migration_handles_empty_database(db):
    from core.db.data_repair import remove_desktop_site_prompt_parts
    assert remove_desktop_site_prompt_parts(db.connection()) == 0
