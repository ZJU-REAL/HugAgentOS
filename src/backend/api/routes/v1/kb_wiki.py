"""LLM Wiki / 概念图谱路由（v1）。

同时服务两类知识库：**自建库**（``kb_`` 前缀，Wiki 由本地管线生成）与**外接库**
（Wiki 由外部知识后端提供，我们透传）。两侧实现模块函数面同构，本文件只负责按
kb_id 选到对的模块、校验访问权限，再做参数校验与信封包装。

不具备 Wiki 能力的知识库（未勾选 Wiki 索引模式的自建库、不提供该能力的外接后端）
一律 404；前端据 ``GET /v1/catalog/kb/wiki/capability`` 与单库的
``GET /v1/catalog/kb/{kb_id}/wiki/capability`` 决定是否渲染入口。

**权限**：自建库的 Wiki 是其原文的衍生物，可读性必须与原文一致——所以这里走和
知识库检索同一套 ``resolve_local_kb_level`` 判定，绝不能让 Wiki 成为绕过知识库
授权的旁路。外接库的白名单校验在其实现模块内完成。
"""

import logging

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError, BadRequestError, ResourceNotFoundError
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/catalog/kb", tags=["Knowledge Base Wiki"])

# 单页最多返回的 Wiki 页数，避免前端一次拉爆几千页的库
MAX_PAGE_SIZE = 200
# 图谱单次最多返回节点数——前端渲染性能的闸，与实现层共用 core 侧的定义
from core.kb.wiki.config import MAX_GRAPH_NODES as MAX_GRAPH_LIMIT


def _assert_readable(db: Session, user_id: str, kb_id: str) -> None:
    """自建库要过读权限；外接库的可见性由其实现模块的白名单负责。"""
    from core.kb.wiki_router import is_local_kb

    if not is_local_kb(kb_id):
        return
    from core.auth.kb_permissions import resolve_local_kb_level

    if resolve_local_kb_level(db, user_id, kb_id) == "none":
        raise AccessDeniedError(message="Access denied", reason="无权访问该知识库")


def resolve_wiki(
    kb_id: str = Path(..., description="知识库 ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取该知识库的 wiki 实现；无权访问 403，不支持 Wiki 则 404。"""
    from core.kb.wiki_router import wiki_module_for

    _assert_readable(db, user.user_id, kb_id)
    module = wiki_module_for(kb_id)
    if module is None:
        raise ResourceNotFoundError(resource_type="kb_wiki", resource_id=kb_id)
    return module


def _guard(exc: Exception, action: str):
    """实现层调用失败 → 统一转成可读的错误，带上下文便于排查。"""
    logger.warning("知识库 Wiki %s 失败: %s", action, exc)
    raise BadRequestError(f"知识库 Wiki 服务暂不可用（{action}）")


@router.get("/wiki/capability", summary="平台是否存在可用的 Wiki 知识库")
async def wiki_capability(
    user: UserContext = Depends(get_current_user),
):
    """前端渲染 Wiki 入口前的全局探测；任何配置下都返回 200。

    ``supports_wiki`` 为真表示「平台上至少有一个 Wiki 源」——可能来自外接后端，
    也可能来自任意一个勾选了 Wiki 的自建库。具体某个库有没有，看单库探测。
    """
    from core.kb.wiki_router import supports_wiki

    provider = ""
    try:
        from core.kb.external_provider import get_provider_name

        provider = get_provider_name()
    except Exception:  # noqa: BLE001 - 社区版下外接接缝可能整体缺席
        provider = ""

    return success_response(
        data={"provider": provider, "supports_wiki": supports_wiki()},
        message="Wiki capability resolved",
    )


@router.get("/{kb_id}/wiki/capability", summary="指定知识库是否具备 Wiki 能力")
async def wiki_capability_for_kb(
    kb_id: str = Path(..., description="知识库 ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """单库探测。无权访问按「不具备」返回，不泄露该库是否存在。"""
    from core.kb.wiki_router import is_local_kb, supports_wiki

    try:
        _assert_readable(db, user.user_id, kb_id)
    except AccessDeniedError:
        return success_response(
            data={"kb_id": kb_id, "supports_wiki": False, "generating": False},
            message="Wiki capability resolved",
        )

    enabled = supports_wiki(kb_id)
    data = {"kb_id": kb_id, "supports_wiki": enabled, "generating": False}
    if enabled and is_local_kb(kb_id):
        from core.kb.wiki.jobs import kb_job_summary

        data.update(kb_job_summary(db, kb_id))
    return success_response(data=data, message="Wiki capability resolved")


@router.get("/{kb_id}/wiki/stats", summary="Wiki 规模统计")
async def wiki_stats(
    kb_id: str = Path(..., description="知识库 ID"),
    wiki=Depends(resolve_wiki),
):
    try:
        data = wiki.wiki_stats(kb_id)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "统计")
    return success_response(data=data, message="Wiki stats retrieved")


@router.get("/{kb_id}/wiki/pages", summary="分页列出 Wiki 页面")
async def wiki_pages(
    kb_id: str = Path(..., description="知识库 ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    page_type: str = Query(
        "", description="页面类型，逗号分隔多选：entity,concept,synthesis,comparison,summary"
    ),
    category_path: str = Query("", description="目录路径，把范围收窄到某个目录节点"),
    category_depth: int = Query(0, ge=0, le=10, description="目录层级，与 category_path 配对"),
    wiki=Depends(resolve_wiki),
):
    try:
        data = wiki.wiki_list_pages(
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
    kb_id: str = Path(..., description="知识库 ID"),
    parent_id: str = Query("", description="父目录 ID，留空取根层"),
    page_types: str = Query("", description="只统计这些类型的页面，逗号分隔"),
    wiki=Depends(resolve_wiki),
):
    """逐层懒加载：大库有几千页，一次性展开整棵树必然卡。"""
    try:
        data = wiki.wiki_folders(kb_id, parent_id=parent_id, page_types=page_types)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "目录")
    return success_response(data=data, message="Wiki folders retrieved")


@router.get("/{kb_id}/wiki/index", summary="Wiki 索引总览")
async def wiki_index(
    kb_id: str = Path(..., description="知识库 ID"),
    limit: int = Query(20, ge=1, le=100, description="每个分组返回多少条"),
    types: str = Query("", description="只取这些类型的分组，逗号分隔"),
    wiki=Depends(resolve_wiki),
):
    try:
        data = wiki.wiki_index(kb_id, limit=limit, types=types)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "索引")
    return success_response(data=data, message="Wiki index retrieved")


@router.get("/{kb_id}/wiki/search", summary="检索 Wiki 页面")
async def wiki_search(
    kb_id: str = Path(..., description="知识库 ID"),
    q: str = Query(..., min_length=1, description="检索词，支持交替写法如 资质|牌照"),
    limit: int = Query(10, ge=1, le=50),
    wiki=Depends(resolve_wiki),
):
    try:
        data = wiki.wiki_search(kb_id, q, limit=limit)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "检索")
    return success_response(data=data, message="Wiki search completed")


@router.get("/{kb_id}/wiki/graph", summary="概念图谱")
async def wiki_graph(
    kb_id: str = Path(..., description="知识库 ID"),
    mode: str = Query("overview", description="overview（全局概览）/ ego（以 center 展开）"),
    center: str = Query("", description="mode=ego 时的中心节点 slug"),
    depth: int = Query(1, ge=1, le=3),
    limit: int = Query(60, ge=1, le=MAX_GRAPH_LIMIT),
    types: str = Query("", description="逗号分隔的页面类型过滤"),
    wiki=Depends(resolve_wiki),
):
    if mode not in ("overview", "ego"):
        raise BadRequestError("mode 只能是 overview 或 ego")
    if mode == "ego" and not center.strip():
        raise BadRequestError("mode=ego 时必须提供 center")
    try:
        data = wiki.wiki_graph(
            kb_id, mode=mode, center=center.strip(), depth=depth, limit=limit, types=types
        )
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "图谱")
    return success_response(data=data, message="Wiki graph retrieved")


@router.get("/{kb_id}/wiki/page/{slug:path}", summary="读取单个 Wiki 页面")
async def wiki_page(
    kb_id: str = Path(..., description="知识库 ID"),
    slug: str = Path(..., description="页面 slug，形如 entity/foo-bar"),
    wiki=Depends(resolve_wiki),
):
    try:
        data = wiki.wiki_read_page(kb_id, slug)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "读页")
    if not data:
        raise ResourceNotFoundError(resource_type="wiki_page", resource_id=slug)
    return success_response(data=data, message="Wiki page retrieved")


@router.get("/{kb_id}/wiki/source/{slug:path}", summary="顺血缘取回该 Wiki 页对应的原文分块")
async def wiki_source(
    kb_id: str = Path(..., description="知识库 ID"),
    slug: str = Path(..., description="页面 slug"),
    max_chunks: int = Query(6, ge=1, le=20),
    wiki=Depends(resolve_wiki),
):
    """「地图 → 定位 → 顺坐标取回原文」这条链的最后一跳，按 ID 直取而非二次检索。"""
    try:
        data = wiki.wiki_source_chunks(kb_id, slug, max_chunks=max_chunks)
    except ResourceNotFoundError:
        raise
    except Exception as exc:
        _guard(exc, "原文回溯")
    if not data.get("page"):
        raise ResourceNotFoundError(resource_type="wiki_page", resource_id=slug)
    return success_response(data=data, message="Wiki source chunks retrieved")


@router.post("/{kb_id}/wiki/rebuild", summary="重新生成该知识库的 Wiki")
async def wiki_rebuild(
    kb_id: str = Path(..., description="知识库 ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把该库全部已完成索引的文档重新排进 Wiki 生成队列。

    用于两种场景：建库后才勾上 Wiki（此前上传的文档需要补生成），以及生成结果
    不理想时想换个粒度重来。需要写权限——这会实际消耗模型额度。
    """
    from core.auth.kb_permissions import has_kb_permission, resolve_local_kb_level
    from core.db.models import KBDocument
    from core.kb.wiki.jobs import enqueue_ingest
    from core.kb.wiki_router import is_local_kb, local_wiki_enabled

    if not is_local_kb(kb_id):
        raise BadRequestError("只有自建知识库支持重新生成 Wiki")
    if not has_kb_permission(resolve_local_kb_level(db, user.user_id, kb_id), "edit"):
        raise AccessDeniedError(message="Access denied", reason="需要知识库编辑权限")
    if not local_wiki_enabled(kb_id):
        raise BadRequestError("该知识库未开启 Wiki 索引模式")

    document_ids = [
        row.document_id
        for row in db.query(KBDocument.document_id)
        .filter(
            KBDocument.kb_id == kb_id,
            KBDocument.deleted_at.is_(None),
            KBDocument.indexing_status == "completed",
        )
        .all()
    ]
    if not document_ids:
        raise BadRequestError("该知识库还没有索引完成的文档")

    job = enqueue_ingest(db, kb_id, document_ids)
    return success_response(
        data={"job_id": job.job_id if job else "", "documents": len(document_ids)},
        message="Wiki rebuild enqueued",
    )
