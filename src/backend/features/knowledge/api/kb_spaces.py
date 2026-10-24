"""kb spaces responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
router = APIRouter()
from features.knowledge.edition import CreateKBSpaceRequest, PolishKBDescriptionRequest, UpdateKBSpaceRequest
from core.auth.backend import UserContext, get_current_user
from core.config.settings import settings
from features.knowledge.wiki.config import normalize_index_modes
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError, BadRequestError, ResourceNotFoundError
from core.infra.responses import created_response, success_response
from features.knowledge.service import KBService
from fastapi import Depends, status
from sqlalchemy.orm import Session
from features.knowledge.api import kb_helpers

@router.post("", status_code=status.HTTP_201_CREATED, summary="创建知识库空间")
def create_kb_space(
    request: CreateKBSpaceRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建归属当前登录用户的知识库空间；可见性规则由当前版本决定。"""
    visibility = (request.visibility or "private").strip()
    if visibility not in ("private", "public"):
        raise BadRequestError(
            message="visibility 仅支持 private / public", data={"field": "visibility"}
        )
    if settings.edition.edition == "ce" and visibility != "private":
        raise BadRequestError(
            message="CE 仅支持私有知识库",
            data={"field": "visibility", "allowed": ["private"]},
        )

    # Permission check: require the creation permission matching the chosen visibility
    from core.auth.capabilities import resolve_user_capabilities

    caps = resolve_user_capabilities(db, user.user_id)
    required_cap = "can_create_public_kb" if visibility == "public" else "can_create_private_kb"
    if not caps.get(required_cap, False):
        label = "公有" if visibility == "public" else "私有"
        raise AccessDeniedError(message="Access denied", reason=f"缺少「创建{label}知识库」权限")

    kb_service = KBService(db)
    metadata = dict(request.metadata or {})
    if request.indexing_config:
        metadata["indexing_config"] = request.indexing_config.model_dump()

    # 索引模式：rag（向量检索）/ wiki（LLM Wiki 图谱），两者同选即 LLM-Wiki 知识库。
    # 三种组合都会分块——wiki 省掉的是向量化，不是分块（引文标注要 chunk id、
    # 回溯原文要按 chunk 直取）。
    metadata["index_modes"] = normalize_index_modes(request.index_modes)
    if request.wiki_config:
        metadata["wiki_config"] = {
            k: v for k, v in request.wiki_config.model_dump().items() if v is not None
        }

    from features.knowledge.edition import initial_visibility_grants

    initial_grants = initial_visibility_grants(db, user.user_id, visibility)

    space = kb_service.create_space(
        user_id=user.user_id,
        name=request.name,
        description=request.description,
        chunk_method=request.chunk_method or "semantic",
        metadata=metadata,
        visibility=visibility,
        initial_grants=initial_grants,
        granted_by=user.user_id,
    )
    return created_response(data=space, message="Knowledge base space created successfully")


@router.post("/polish-description", summary="AI 生成知识库简介")
async def polish_kb_description(
    request: PolishKBDescriptionRequest,
    user: UserContext = Depends(get_current_user),
):
    """根据知识库名称和现有简介，调用大模型生成一段更利于智能路由判断的知识库简介。名称为必填，模型失败时回退到本地模板生成。仅返回润色后的简介文本，不修改任何知识库记录。"""
    name = request.name.strip()
    description = (request.description or "").strip()
    if not name:
        raise BadRequestError(message="Knowledge base name is required", data={"field": "name"})

    polished = await kb_helpers.generate_kb_description(name, description)
    return success_response(
        data={"description": polished},
        message="Knowledge base description polished successfully",
    )


@router.patch("/{kb_id}", summary="更新知识库空间")
def update_kb_space(
    kb_id: str,
    request: UpdateKBSpaceRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新指定知识库空间的名称、简介与索引模式。仅空间所有者可操作；空间不存在或无权访问时返回 404。

    改索引模式不会自动补建索引：新勾选 Wiki 后要调 `POST /{kb_id}/wiki/rebuild`
    才会把已有文档排进生成队列。
    """
    kb_service = KBService(db)
    space = kb_service.update_space(
        kb_id=kb_id,
        user_id=user.user_id,
        name=request.name,
        description=request.description,
        index_modes=request.index_modes,
        wiki_config=(request.wiki_config.model_dump() if request.wiki_config else None),
    )
    if not space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    return success_response(data=space, message="Knowledge base space updated successfully")
