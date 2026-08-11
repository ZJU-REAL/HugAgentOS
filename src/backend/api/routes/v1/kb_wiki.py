"""外接知识库的 LLM Wiki / 概念图谱透传路由（v1）。

只有具备 Wiki 能力的外接后端（当前为 WeKnora）才提供这层结构化产物；
Dify / FastGPT 与社区版下 ``supports_wiki()`` 为假，本文件所有端点统一 404，
前端据 ``GET /v1/catalog/kb/wiki/capability`` 决定是否渲染 Wiki 入口。

路由是**薄透传**：鉴权、白名单与字段裁剪都在 provider 模块内完成，这里只负责
参数校验与信封包装。
"""

import logging

from core.auth.backend import UserContext, get_current_user
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, Path, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/catalog/kb", tags=["Knowledge Base Wiki"])

# 单页最多返回的 Wiki 页数，避免前端一次拉爆 2000+ 页的库
MAX_PAGE_SIZE = 200
# 图谱单次最多返回节点数；WeKnora 侧还有自己的上限，这里是前端渲染性能的闸
MAX_GRAPH_LIMIT = 300


def _wiki():
    """取当前后端的 wiki 实现；不支持时直接 404。"""
    from core.kb.external_provider import wiki_module

    module = wiki_module()
    if module is None:
        raise ResourceNotFoundError(
            resource_type="kb_wiki",
            resource_id="current_provider",
        )
    return module


def _guard(exc: Exception, action: str):
    """provider 调用失败 → 统一转成 502 语义的 BadRequestError，带上下文便于排查。"""
    logger.warning("WeKnora wiki %s failed: %s", action, exc)
    raise BadRequestError(f"知识库 Wiki 服务暂不可用（{action}）")


@router.get("/wiki/capability", summary="当前知识库后端是否支持 Wiki / 图谱")
async def wiki_capability(
    user: UserContext = Depends(get_current_user),
):
    """前端渲染 Wiki 入口前的探测端点；任何后端下都返回 200。"""
    from core.kb.external_provider import get_provider_name, supports_wiki

    return success_response(
        data={
            "provider": get_provider_name(),
            "supports_wiki": supports_wiki(),
        },
        message="Wiki capability resolved",
    )


@router.get("/{kb_id}/wiki/stats", summary="Wiki 规模统计")
async def wiki_stats(
    kb_id: str = Path(..., description="External knowledge base ID"),
    user: UserContext = Depends(get_current_user),
):
    try:
        data = _wiki().wiki_stats(kb_id)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "统计")
    return success_response(data=data, message="Wiki stats retrieved")


@router.get("/{kb_id}/wiki/pages", summary="分页列出 Wiki 页面")
async def wiki_pages(
    kb_id: str = Path(..., description="External knowledge base ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    page_type: str = Query(
        "", description="页面类型，逗号分隔多选：entity,concept,synthesis,comparison,summary"
    ),
    category_path: str = Query("", description="目录路径，把范围收窄到某个目录节点"),
    category_depth: int = Query(0, ge=0, le=10, description="目录层级，与 category_path 配对"),
    user: UserContext = Depends(get_current_user),
):
    try:
        data = _wiki().wiki_list_pages(
            kb_id,
            page=page,
            page_size=page_size,
            page_type=page_type,
            category_path=category_path,
            category_depth=category_depth,
        )
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "页面列表")
    return success_response(data=data, message="Wiki pages retrieved")


@router.get("/{kb_id}/wiki/folders", summary="Wiki 目录树的某一层")
async def wiki_folders(
    kb_id: str = Path(..., description="External knowledge base ID"),
    parent_id: str = Query("", description="父目录 ID，留空取根层"),
    page_types: str = Query("", description="只统计这些类型的页面，逗号分隔"),
    user: UserContext = Depends(get_current_user),
):
    """逐层懒加载：全库两千多页，一次性展开整棵树必然卡。"""
    try:
        data = _wiki().wiki_folders(kb_id, parent_id=parent_id, page_types=page_types)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "目录")
    return success_response(data=data, message="Wiki folders retrieved")


@router.get("/{kb_id}/wiki/index", summary="Wiki 索引总览")
async def wiki_index(
    kb_id: str = Path(..., description="External knowledge base ID"),
    limit: int = Query(20, ge=1, le=100, description="每个分组返回多少条"),
    types: str = Query("", description="只取这些类型的分组，逗号分隔"),
    user: UserContext = Depends(get_current_user),
):
    try:
        data = _wiki().wiki_index(kb_id, limit=limit, types=types)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "索引")
    return success_response(data=data, message="Wiki index retrieved")


@router.get("/{kb_id}/wiki/search", summary="检索 Wiki 页面")
async def wiki_search(
    kb_id: str = Path(..., description="External knowledge base ID"),
    q: str = Query(..., min_length=1, description="检索词，支持正则交替如 资质|牌照"),
    limit: int = Query(10, ge=1, le=50),
    user: UserContext = Depends(get_current_user),
):
    try:
        data = _wiki().wiki_search(kb_id, q, limit=limit)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "检索")
    return success_response(data=data, message="Wiki search completed")


@router.get("/{kb_id}/wiki/graph", summary="概念图谱")
async def wiki_graph(
    kb_id: str = Path(..., description="External knowledge base ID"),
    mode: str = Query("overview", description="overview（全局概览）/ ego（以 center 展开）"),
    center: str = Query("", description="mode=ego 时的中心节点 slug"),
    depth: int = Query(1, ge=1, le=3),
    limit: int = Query(60, ge=1, le=MAX_GRAPH_LIMIT),
    types: str = Query("", description="逗号分隔的页面类型过滤"),
    user: UserContext = Depends(get_current_user),
):
    if mode not in ("overview", "ego"):
        raise BadRequestError("mode 只能是 overview 或 ego")
    if mode == "ego" and not center.strip():
        raise BadRequestError("mode=ego 时必须提供 center")
    try:
        data = _wiki().wiki_graph(
            kb_id, mode=mode, center=center.strip(), depth=depth, limit=limit, types=types
        )
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "图谱")
    return success_response(data=data, message="Wiki graph retrieved")


@router.get("/{kb_id}/wiki/page/{slug:path}", summary="读取单个 Wiki 页面")
async def wiki_page(
    kb_id: str = Path(..., description="External knowledge base ID"),
    slug: str = Path(..., description="页面 slug，形如 entity/foo-bar"),
    user: UserContext = Depends(get_current_user),
):
    try:
        data = _wiki().wiki_read_page(kb_id, slug)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "读页")
    if not data:
        raise ResourceNotFoundError(resource_type="wiki_page", resource_id=slug)
    return success_response(data=data, message="Wiki page retrieved")


@router.get("/{kb_id}/wiki/source/{slug:path}", summary="顺血缘取回该 Wiki 页对应的原文分块")
async def wiki_source(
    kb_id: str = Path(..., description="External knowledge base ID"),
    slug: str = Path(..., description="页面 slug"),
    max_chunks: int = Query(6, ge=1, le=20),
    user: UserContext = Depends(get_current_user),
):
    """「地图 → 定位 → 顺坐标取回原文」这条链的最后一跳，按 ID 直取而非二次检索。"""
    try:
        data = _wiki().wiki_source_chunks(kb_id, slug, max_chunks=max_chunks)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "原文回溯")
    if not data.get("page"):
        raise ResourceNotFoundError(resource_type="wiki_page", resource_id=slug)
    return success_response(data=data, message="Wiki source chunks retrieved")
