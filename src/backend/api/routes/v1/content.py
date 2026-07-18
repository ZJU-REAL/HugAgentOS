"""Content block read API routes (v1) — CE 子集（split：§5.2）.

社区版只保留前台读取端点：内容块读取、版本轮询、操作手册元信息。
内容写入 / 快照导入导出 / 资产上传属商业版内容台（ADMIN/CONFIG 令牌），
不在社区版提供——对应实现物理不进 CE 树。
"""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.content.content_blocks import (
    DEFAULT_APP_CONFIG,
    DOCS_BLOCK_MAP,
    normalize_app_config,
    normalize_homepage_shortcuts,
)
from core.db.engine import get_db
from core.db.models import ContentBlock
from core.infra.responses import success_response

router = APIRouter(prefix="/v1/content", tags=["Content"])


@router.get("/docs", summary="获取文档内容块（前台读取）")
async def get_docs_content(db: Session = Depends(get_db)):
    """前台读取全部可编辑内容块：功能更新时间轴、能力中心、提示词广场、页面配置、
    应用配置与首页快捷卡，并附各块最近更新时间。无需鉴权，DB 缺失时对应字段返回空值。"""
    updates_row = db.query(ContentBlock).filter(ContentBlock.id == "docs_updates").first()
    caps_row = db.query(ContentBlock).filter(ContentBlock.id == "docs_capabilities").first()
    prompt_hub_row = db.query(ContentBlock).filter(ContentBlock.id == "prompt_hub").first()
    page_config_row = db.query(ContentBlock).filter(ContentBlock.id == "page_config").first()
    app_config_row = db.query(ContentBlock).filter(ContentBlock.id == "app_config").first()
    hs_row = db.query(ContentBlock).filter(ContentBlock.id == "homepage_shortcuts").first()

    return success_response(data={
        "updates": updates_row.payload if updates_row else [],
        "capabilities": caps_row.payload if caps_row else [],
        "prompt_hub": prompt_hub_row.payload if prompt_hub_row else [],
        "page_config": page_config_row.payload if page_config_row else {},
        "app_config": normalize_app_config(app_config_row.payload if app_config_row else DEFAULT_APP_CONFIG),
        "homepage_shortcuts": normalize_homepage_shortcuts(hs_row.payload if hs_row else None),
        "updates_updated_at": updates_row.updated_at.isoformat() if updates_row and updates_row.updated_at else None,
        "capabilities_updated_at": caps_row.updated_at.isoformat() if caps_row and caps_row.updated_at else None,
        "page_config_updated_at": page_config_row.updated_at.isoformat() if page_config_row and page_config_row.updated_at else None,
        "app_config_updated_at": app_config_row.updated_at.isoformat() if app_config_row and app_config_row.updated_at else None,
        "homepage_shortcuts_updated_at": hs_row.updated_at.isoformat() if hs_row and hs_row.updated_at else None,
    })


@router.get("/docs/version", summary="获取各内容块最新更新时间（轻量轮询）")
async def get_docs_versions(db: Session = Depends(get_db)):
    """轻量轮询接口：仅返回各内容块的最新更新时间，供前端判断是否需要重新拉取正文。"""
    rows = (
        db.query(ContentBlock)
        .filter(ContentBlock.id.in_(list(DOCS_BLOCK_MAP.values())))
        .all()
    )
    row_map = {row.id: row for row in rows}
    versions = {}
    for alias, db_id in DOCS_BLOCK_MAP.items():
        row = row_map.get(db_id)
        versions[alias] = row.updated_at.isoformat() if row and row.updated_at else None
    return success_response(data=versions)


# ── Manual (操作手册) ─────────────────────────────────────────────────────────

MANUAL_DIR = Path("/app/storage/manual")
# ASCII 文件名 — 中文名经过 docker save → tar → docker load 易被破坏，nginx 静态托管会 404。
MANUAL_FILENAME = "manual.pdf"


@router.get("/manual", summary="获取操作手册信息")
async def get_manual_info():
    """获取已上传操作手册 PDF 的元信息（是否存在、文件名、大小、上传时间、访问 URL）。无需鉴权。"""
    filepath = MANUAL_DIR / MANUAL_FILENAME
    if not filepath.exists():
        return success_response(data={"exists": False})

    stat = filepath.stat()
    return success_response(data={
        "exists": True,
        "filename": MANUAL_FILENAME,
        "size": stat.st_size,
        "uploaded_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "url": f"/docs/manual/{MANUAL_FILENAME}",
    })
