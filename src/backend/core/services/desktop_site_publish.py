"""Transfer desktop build output to the authenticated cloud site service."""

from __future__ import annotations

import io
import json
import tarfile

from core.services.site_access_policy import SitePublishScopeFields, site_scope_ref
from pydantic import Field

MAX_ARCHIVE_BYTES = 40 * 1024 * 1024


class SiteUploadOptions(SitePublishScopeFields):
    title: str = Field("", max_length=200)
    slug: str = Field("", max_length=50)
    site_id: str = Field("", max_length=100)
    description: str = Field("", max_length=2000)


async def package_local_site(arguments, headers):
    """Read the current local sandbox, never a path on the remote backend."""
    from core.config.local_mode import local_mode_enabled
    from core.llm.tools._paths import to_physical_path
    from core.llm.tools._tool_helpers import _validate_workspace_path
    from core.services.site_packaging import pack_and_fetch_dir, resolve_project_context
    from core.services.site_service import SiteService

    headers = {k.lower(): v for k, v in headers.items()}
    user_id = headers.get("x-current-user-id", "")
    chat_id = headers.get("x-conversation-id") or headers.get("x-chat-id")
    if not user_id:
        raise ValueError("发布站点缺少本机用户身份")
    if local_mode_enabled() and not chat_id:
        raise ValueError("本机站点发布必须使用已绑定本地项目的会话")
    arguments.pop("_desktop_source", None)
    project_id, project_dir = resolve_project_context(chat_id or "", user_id)
    src = str(arguments.get("src_dir") or "").strip().rstrip("/")
    if not src or src == ".":
        if local_mode_enabled():
            raise ValueError(
                "本机发布必须显式传 src_dir；编辑前调用 list_project_sites 查询原 site_id 和发布目录"
            )
        src = project_dir or "/workspace/site"
    import os

    if not os.path.isabs(src):
        src = "/workspace/" + src
    src = to_physical_path(src, user_id, session_id=chat_id)
    # A desktop project root is an explicitly bound host directory. It is not
    # necessarily under the managed scratch workspace (e.g. Windows Desktop).
    # Keep containment checks; authorize only the current owned project's root.
    roots = (project_dir,) if local_mode_enabled() and project_dir else ()
    error = _validate_workspace_path(src + "/", additional_roots=roots)
    if error:
        raise ValueError(error)
    source = str(arguments.get("source_dir") or "").strip().rstrip("/")
    if source and source == src:
        source = "" if not arguments.get("source_dir") else source
    if source and not os.path.isabs(source):
        source = "/workspace/" + source
    if source and to_physical_path(source, user_id, session_id=chat_id) == src:
        raise ValueError("src_dir 必须指向构建产物，不能与 source_dir 相同")
    if local_mode_enabled() and chat_id:
        from core.services.local_site_sources import validate_source

        source_path = to_physical_path(source, user_id, session_id=chat_id) if source else src
        arguments["_desktop_source"] = validate_source(user_id, chat_id, source_path, src)
    files, error = await pack_and_fetch_dir(src, chat_id, user_id)
    if error:
        raise ValueError(error)
    if SiteService.looks_like_source_tree(name for name, _ in files):
        raise ValueError("请先构建站点，再发布构建产物目录")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in files:
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    data = output.getvalue()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("站点发布包超过 40 MB")
    options = SiteUploadOptions.model_validate(arguments)
    return data, json.dumps(options.model_dump(), ensure_ascii=True)


def localize_site_result(data, cloud_base, arguments=None, headers=None):
    """Turn the cloud's site-relative address into one the desktop can open."""
    for block in data.get("content", []):
        if block.get("type") != "text":
            continue
        published = json.loads(block["text"])
        context = (arguments or {}).get("_desktop_source")
        if context and published.get("ok") and published.get("site_id"):
            from core.services.local_site_sources import save_receipt

            normalized = {k.lower(): v for k, v in (headers or {}).items()}
            from fastapi import HTTPException
            from sqlalchemy.exc import SQLAlchemyError

            try:
                save_receipt(
                    normalized.get("x-current-user-id", ""),
                    normalized.get("x-conversation-id") or normalized.get("x-chat-id") or "",
                    context,
                    published,
                    cloud_base,
                )
                published["local_source"] = context
            except (HTTPException, SQLAlchemyError, OSError, ValueError):
                # The cloud write is known to have succeeded. Never turn a
                # failed local index write into an unknown/retryable publish.
                published["local_source_warning"] = (
                    "云端发布成功，但本机关联未保存。请保留 site_id，"
                    "后续发布请显式携带该 site_id，不要重新创建站点。"
                )
                published["source_dir"] = context["source_dir"]
        if str(published.get("url", "")).startswith("/site/"):
            published["url"] = cloud_base + published["url"]
        block["text"] = json.dumps(published, ensure_ascii=False)


def publish_uploaded_site(user_id, data, options):
    """The authenticated cloud user owns storage, versions and dynamic data.

    Local chat/project identifiers are intentionally not attached to cloud rows.
    Source files remain in the desktop project; only build output is uploaded.
    """
    from core.db.engine import SessionLocal
    from core.services.site_packaging import safe_extract_tar
    from core.services.site_service import SiteService

    files = safe_extract_tar(data)
    if SiteService.looks_like_source_tree(name for name, _ in files):
        raise ValueError("请上传构建产物，不能托管未构建的源码工程")
    with SessionLocal() as db:
        site = SiteService(db).publish(
            user_id=user_id,
            files=files,
            title=options.title,
            slug=options.slug,
            site_id=options.site_id,
            visibility=options.visibility,
            description=options.description,
            scope_id=site_scope_ref(options),
            build_info={"kind": "desktop", "source_location": "local"},
        )
        payload = {
            "ok": True,
            "site_id": site.site_id,
            "slug": site.slug,
            "url": f"/site/{site.slug}/",
            "title": site.title,
            "visibility": site.visibility,
            "version": site.current_version,
            "file_count": site.file_count,
            "total_size_bytes": site.total_size_bytes,
            "origin": "cloud",
            "note": "站点已托管到云端。源码保留在本机，继续修改后请带 site_id 再次发布。",
        }
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "metadata": {"origin": "cloud"},
    }


async def forward_local_publish(body):
    """Sites are cloud-hosted: a local site MCP's publish call goes to the gateway."""
    from core.infra.responses import success_response
    from core.llm.agent_factory import _inject_runtime_headers
    from core.llm.mcp_pool import make_client
    from core.services.desktop_cloud_bridge import cloud_gateway_mcp_configs
    from core.services.desktop_gateway_uploads import endpoint_plugin

    configs = cloud_gateway_mcp_configs()
    for sid, config in configs.items():
        if config.get("gateway_plugin") != endpoint_plugin("site-publish"):
            continue
        configs = _inject_runtime_headers(
            {sid: config},
            current_user_id=body.user_id,
            chat_id=body.chat_id,
            enabled_kb_ids=[],
            channel_origin=None,
            reranker_enabled=False,
        )
        client = make_client(sid, configs[sid], is_stateful=False)
        try:
            tool = await client.get_tool("publish_site")
            result = await tool(**body.model_dump(exclude={"user_id", "chat_id"}))
            for block in result.content:
                if getattr(block, "type", None) == "text":
                    return success_response(data=json.loads(block.text))
            return success_response(data={"error": "云端发布结果未知，请先在云端站点列表核对"})
        finally:
            await client.close()
    return success_response(data={"error": "云端站点发布能力不可用，请启用云端站点插件后重试"})
