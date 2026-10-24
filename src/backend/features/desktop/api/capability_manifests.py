"""capability manifests responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
from features.desktop.gateway.observability import ObservedGatewayRoute
router = APIRouter(prefix="/v1/desktop/capability", tags=["Desktop Capability"], route_class=ObservedGatewayRoute)
from core.infra.responses import success_response
from fastapi import Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from features.desktop.api import capability_dependencies
from features.desktop.api import desktop_capability_state

@router.get("/manifest", summary="当前用户的云端能力清单")
async def get_manifest(
    request: Request,
    response: Response,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    from features.desktop.capability.service import build_user_capability_manifest

    use_cache = request.headers.get("cache-control") != "no-cache"
    manifest = await run_in_threadpool(
        capability_dependencies.public_content, user_id,
        lambda: build_user_capability_manifest(user_id, use_cache=use_cache),
    )
    etag = f'"{manifest["revision"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return success_response(data=manifest)


@router.get("/models", summary="桌面本机执行面的无密钥模型清单")
async def get_model_manifest(
    request: Request,
    response: Response,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    from features.desktop.capability.service import build_user_model_manifest

    manifest = await run_in_threadpool(
        capability_dependencies.public_content, user_id, lambda: build_user_model_manifest(user_id)
    )
    etag = f'"{manifest["revision"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return success_response(data=manifest)


@router.get("/skills/manifest", summary="当前用户的云端技能清单")
async def get_skill_manifest(
    request: Request,
    response: Response,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    from features.desktop.capability.service import build_user_skill_manifest

    use_cache = request.headers.get("cache-control") != "no-cache"
    manifest = await run_in_threadpool(
        capability_dependencies.public_content, user_id,
        lambda: build_user_skill_manifest(user_id, use_cache=use_cache),
    )
    etag = f'"{manifest["revision"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return success_response(data=manifest)


@router.get("/skills/{skill_id}/bundle", summary="下载一个当前授权技能的完整 zip 包")
async def get_skill_bundle(
    skill_id: str,
    request: Request,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    from features.desktop.capability.service import resolve_skill_bundle

    resolved = capability_dependencies.public_content(
        user_id, lambda: resolve_skill_bundle(user_id, skill_id), bundle=True
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="skill not available")
    data, content_hash = resolved
    etag = f'"{content_hash}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="application/zip", headers=headers)


@router.get("/agents/manifest", summary="当前用户可见的智能体定义清单")
async def get_agent_manifest(
    request: Request, response: Response, user_id: str = Depends(capability_dependencies.require_capability_user)
):
    from features.desktop.capability.service import build_user_agent_manifest

    use_cache = request.headers.get("cache-control") != "no-cache"
    manifest = await run_in_threadpool(
        capability_dependencies.public_content, user_id,
        lambda: build_user_agent_manifest(user_id, use_cache=use_cache),
    )
    return capability_dependencies.entity_manifest_response(request, response, manifest)


@router.get(
    "/agents/{agent_id}/bundle", summary="下载一个智能体定义包（agent.json + instructions.md）"
)
async def get_agent_bundle(
    agent_id: str, request: Request, user_id: str = Depends(capability_dependencies.require_capability_user)
):
    from features.desktop.capability.service import resolve_agent_bundle

    return capability_dependencies.bundle_response(
        request,
        capability_dependencies.public_content(user_id, lambda: resolve_agent_bundle(user_id, agent_id), bundle=True),
    )


@router.get("/plugins/manifest", summary="当前用户已安装插件清单")
async def get_plugin_manifest(
    request: Request, response: Response, user_id: str = Depends(capability_dependencies.require_capability_user)
):
    from features.desktop.capability.service import build_user_plugin_manifest

    use_cache = request.headers.get("cache-control") != "no-cache"
    manifest = await run_in_threadpool(
        capability_dependencies.public_content, user_id,
        lambda: build_user_plugin_manifest(user_id, use_cache=use_cache),
    )
    return capability_dependencies.entity_manifest_response(request, response, manifest)


@router.get("/plugins/{install_id}/bundle", summary="下载一个插件定义包（plugin.json）")
async def get_plugin_bundle(
    install_id: str, request: Request, user_id: str = Depends(capability_dependencies.require_capability_user)
):
    from features.desktop.capability.service import resolve_plugin_bundle

    return capability_dependencies.bundle_response(
        request,
        capability_dependencies.public_content(user_id, lambda: resolve_plugin_bundle(user_id, install_id), bundle=True),
    )
