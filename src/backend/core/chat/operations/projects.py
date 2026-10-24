"""Shared projects operations; callers supply authenticated identity."""

from core.auth.edition import ProjectAccessInfo

from core.infra.responses import paginated_response, success_response

from core.projects.files import ProjectFileService

from core.projects.edition import ProjectService

from fastapi import HTTPException


def list_projects(q=None, sort="-last_activity_at", page=1, page_size=30, user=None, db=None):
    """分页列出当前用户可见的项目（个人 + 团队混合可见性），支持关键词搜索与排序。"""
    svc = ProjectService(db)
    items, total = svc.list_visible(
        str(user.user_id), q=q, sort=sort, page=page, page_size=page_size
    )
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total)


def get_project(access=None, db=None):
    """获取项目详情，附带挂钩文件夹名称及容量（已用 / 上限）；项目不存在返回 404。"""
    svc = ProjectService(db)
    data = svc.get(
        access.project.project_id,
        access.user_id,
        access=ProjectAccessInfo(access.level, access.is_owner),
    )
    if data is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    pf_svc = ProjectFileService(db)
    data["capacity_used"] = pf_svc.capacity_used(access.project)
    data["capacity_limit"] = pf_svc.capacity_limit()
    return success_response(data=data)


def list_project_chats(page=1, page_size=50, scope="all", access=None, db=None):
    """Return conversations visible to the authenticated project member."""
    svc = ProjectService(db)
    items, total = svc.list_chats(
        access.project.project_id,
        access.user_id,
        page=page,
        page_size=page_size,
        scope=scope,
    )
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total)
