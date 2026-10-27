"""mcp submissions responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
import json
import uuid
from typing import Any, Dict, List, Optional
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketInstallation, McpMarketItem, McpMarketSubmission
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from features.marketplace import edition as ml
from core.capabilities.mcp.management import decrypt_mcp_headers, mcp_oauth_bundle_storage_key, probe_mcp_connectivity, tool_snapshot_hash, validate_remote_mcp_url
from sqlalchemy.orm import Session
from core.db.threads import run_in_session
from features.marketplace import mcp_oauth_clients
from features.marketplace import mcp_validation
from features.marketplace import mcp_versions

def _store_tools(db: Session, server_id: str, tools) -> None:
    """Keep the probed tool list on the source server, as a live probe of it would."""
    source = db.query(AdminMcpServer).filter(AdminMcpServer.server_id == server_id).first()
    if source is not None:
        source.tools_json = tools


def _submittable(db: Session, source_server_id: str, owner_user_id: str) -> AdminMcpServer:
    """The owner's remote MCP, detached, once it may be submitted for listing."""
    row = (
        db.query(AdminMcpServer)
        .filter(
            AdminMcpServer.server_id == source_server_id,
            AdminMcpServer.owner_user_id == owner_user_id,
        )
        .first()
    )
    if not row:
        raise ResourceNotFoundError("private_mcp_server", source_server_id)
    if row.transport not in ("streamable_http", "sse") or not row.url:
        raise BadRequestError(message="只有远程 HTTP/SSE MCP 可以申请上架")
    installed_from_market = (
        db.query(McpMarketInstallation.install_id)
        .filter(McpMarketInstallation.server_id == row.server_id)
        .first()
    )
    if (row.extra_config or {}).get("market_slug") or installed_from_market:
        raise BadRequestError(message="从 MCP 市场安装的实例不能再次申请上架")
    pending = (
        db.query(McpMarketSubmission)
        .filter(
            McpMarketSubmission.source_server_id == source_server_id,
            McpMarketSubmission.owner_user_id == owner_user_id,
            McpMarketSubmission.status == "pending",
            McpMarketSubmission.deleted_at.is_(None),
        )
        .first()
    )
    if pending:
        raise BadRequestError(message="该 MCP 已有待审核的上架申请")
    db.expunge(row)
    return row


async def submit_to_marketplace(
    source_server_id: str,
    *,
    owner_user_id: str,
    submitter_name: str,
    category: str,
    version: str,
    summary: str = "",
    note: str = "",
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    row = await run_in_session(_submittable, source_server_id, owner_user_id)
    await validate_remote_mcp_url(
        row.url,
        allow_private_network=mcp_validation.self_service_private_network_allowed(),
        require_https=False,
    )
    ok, error = await probe_mcp_connectivity(row, validate=True)
    if not ok:
        raise BadRequestError(message=f"MCP 上架检测失败：{error}")
    tools = list(row.tools_json or [])
    if not tools:
        raise BadRequestError(message="MCP 未发现任何工具，不能申请上架")

    def record(db: Session) -> Dict[str, Any]:
        return _record_submission(
            db, row, tools, owner_user_id, submitter_name, category, version, summary, note, tags
        )

    return await run_in_session(record)


def _record_submission(
    db: Session, row, tools, owner_user_id, submitter_name, category, version, summary, note, tags
) -> Dict[str, Any]:
    _store_tools(db, row.server_id, tools)
    template_url, auth_schema = mcp_validation.credential_free_connection(row)
    oauth_raw = decrypt_mcp_headers(row.headers).get(mcp_oauth_bundle_storage_key())
    auth_config = (
        mcp_oauth_clients.submission_auth_config(json.loads(oauth_raw))
        if oauth_raw
        else None
    )
    now = utc_now()
    submission = McpMarketSubmission(
        submission_id=f"mcpsub_{uuid.uuid4().hex}",
        slug=mcp_validation.source_slug(db, row, owner_user_id),
        source_server_id=row.server_id,
        owner_user_id=owner_user_id,
        submitter_name=submitter_name or "",
        display_name=row.display_name,
        description=(summary or row.description or "").strip(),
        user_intro=row.user_intro,
        category=mcp_validation.validate_category(category),
        tags=list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip())),
        icon=row.icon,
        version=mcp_validation.normalize_version(version),
        transport=row.transport,
        url=template_url,
        auth_schema=auth_schema,
        auth_config=mcp_validation.normalize_auth_config(auth_config, auth_schema),
        tools_json=tools,
        tool_hash=tool_snapshot_hash(tools),
        note=(note or "").strip(),
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return submission_dict(submission)


def submission_dict(row: McpMarketSubmission, *, detail: bool = False) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "submission_id": row.submission_id,
        "slug": row.slug,
        "source_server_id": row.source_server_id,
        "owner_user_id": row.owner_user_id,
        "submitter_name": row.submitter_name or "",
        "display_name": row.display_name,
        "description": row.description or "",
        "category": row.category,
        "tags": list(row.tags or []),
        "icon": row.icon or "",
        "version": row.version,
        "transport": row.transport,
        "url_origin": mcp_validation.safe_origin(row.url),
        "auth_schema": list(row.auth_schema or []),
        "auth_config": mcp_validation.normalize_auth_config(row.auth_config, list(row.auth_schema or [])),
        "tool_count": len(row.tools_json or []),
        "tool_hash": row.tool_hash,
        "listing_notice": dict(row.listing_notice or {}),
        "note": row.note or "",
        "status": row.status,
        "review_note": row.review_note or "",
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if detail:
        data["user_intro"] = row.user_intro or ""
        data["url"] = row.url
        data["tools"] = list(row.tools_json or [])
    return data


def list_my_submissions(db: Session, owner_user_id: str) -> List[Dict[str, Any]]:
    rows = (
        db.query(McpMarketSubmission)
        .filter(
            McpMarketSubmission.owner_user_id == owner_user_id,
            McpMarketSubmission.deleted_at.is_(None),
        )
        .order_by(McpMarketSubmission.created_at.desc())
        .all()
    )
    return [submission_dict(row) for row in rows]


def withdraw_submission(db: Session, submission_id: str, owner_user_id: str) -> None:
    row = (
        db.query(McpMarketSubmission)
        .filter(
            McpMarketSubmission.submission_id == submission_id,
            McpMarketSubmission.owner_user_id == owner_user_id,
            McpMarketSubmission.deleted_at.is_(None),
        )
        .first()
    )
    if not row:
        raise ResourceNotFoundError("mcp_market_submission", submission_id)
    if row.status != "pending":
        raise BadRequestError(message="只有待审核申请可以撤回")
    row.status = "withdrawn"
    row.deleted_at = utc_now()
    row.updated_at = utc_now()
    db.commit()


def list_submissions(db: Session, status: Optional[str] = None) -> List[Dict[str, Any]]:
    query = db.query(McpMarketSubmission).filter(McpMarketSubmission.deleted_at.is_(None))
    if status:
        query = query.filter(McpMarketSubmission.status == status)
    return [
        submission_dict(row) for row in query.order_by(McpMarketSubmission.created_at.desc()).all()
    ]


def get_submission(db: Session, submission_id: str) -> Dict[str, Any]:
    row = (
        db.query(McpMarketSubmission)
        .filter(
            McpMarketSubmission.submission_id == submission_id,
            McpMarketSubmission.deleted_at.is_(None),
        )
        .first()
    )
    if not row:
        raise ResourceNotFoundError("mcp_market_submission", submission_id)
    return submission_dict(row, detail=True)


def _reviewable(db: Session, submission_id: str, approve: bool):
    row = (
        db.query(McpMarketSubmission)
        .filter(
            McpMarketSubmission.submission_id == submission_id,
            McpMarketSubmission.deleted_at.is_(None),
        )
        .first()
    )
    if not row:
        raise ResourceNotFoundError("mcp_market_submission", submission_id)
    if (approve and row.status != "pending") or (
        not approve and row.status not in ("pending", "approved")
    ):
        raise BadRequestError(message="该申请当前状态不可审核")
    return row


def _review_source(db: Session, submission_id: str):
    """The applicant's MCP, detached, once it still matches the submission."""
    row = _reviewable(db, submission_id, True)
    source = (
        db.query(AdminMcpServer)
        .filter(
            AdminMcpServer.server_id == row.source_server_id,
            AdminMcpServer.owner_user_id == row.owner_user_id,
        )
        .first()
    )
    if not source:
        raise BadRequestError(message="申请人的原始 MCP 已删除，不能通过审核")
    source_template_url, source_auth_schema = mcp_validation.credential_free_connection(source)
    if source.transport != row.transport or source_template_url.strip() != row.url.strip():
        raise BadRequestError(message="MCP 连接地址或传输方式已变化，请申请人重新提交")
    if source_auth_schema != list(row.auth_schema or []):
        raise BadRequestError(message="MCP 认证参数已变化，请申请人重新提交")
    db.expunge(source)
    return source


async def review_submission(
    submission_id: str,
    *,
    approve: bool,
    review_note: str = "",
    category: str = "",
    reviewed_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Approve (after a live re-check) or reject a listing request."""
    tools = None
    if approve:
        source = await run_in_session(_review_source, submission_id)
        await validate_remote_mcp_url(
            source.url or "",
            allow_private_network=mcp_validation.self_service_private_network_allowed(),
            require_https=False,
        )
        ok, error = await probe_mcp_connectivity(source, validate=True)
        if not ok:
            raise BadRequestError(message=f"MCP 审核复检失败：{error}")
        tools = list(source.tools_json or [])
    return await run_in_session(
        _record_review, submission_id, approve, tools, review_note, category, reviewed_by
    )


def _record_review(db: Session, submission_id, approve, tools, review_note, category, reviewed_by):
    row = _reviewable(db, submission_id, approve)
    now = utc_now()
    if approve:
        if tool_snapshot_hash(tools or []) != row.tool_hash:
            raise BadRequestError(message="MCP 工具或参数结构已变化，请申请人重新提交")
        _store_tools(db, row.source_server_id, tools)
        mcp_versions.publish_snapshot(
            db,
            slug=row.slug,
            display_name=row.display_name,
            description=row.description,
            user_intro=row.user_intro,
            category=category or row.category,
            tags=list(row.tags or []),
            icon=row.icon,
            publisher_id=row.owner_user_id,
            publisher_name=row.submitter_name,
            source="community",
            version=row.version,
            transport=row.transport,
            url=row.url,
            auth_schema=list(row.auth_schema or []),
            auth_config=mcp_validation.normalize_auth_config(row.auth_config, list(row.auth_schema or [])),
            tools=list(row.tools_json or []),
            source_server_id=row.source_server_id,
            approved_by=reviewed_by,
        )
        row.status = "approved"
    else:
        row.status = "rejected"
        item = db.query(McpMarketItem).filter(McpMarketItem.slug == row.slug).first()
        if item:
            item.deleted_at = now
            item.updated_at = now
            ml.set_listing_enabled(db, ml.KIND_MCP, row.slug, False, updated_by=reviewed_by)
    row.review_note = (review_note or "").strip()
    row.reviewed_by = reviewed_by
    row.reviewed_at = now
    row.updated_at = now
    db.commit()
    if approve:
        ml.set_listing_enabled(db, ml.KIND_MCP, row.slug, True, updated_by=reviewed_by)
    return submission_dict(row, detail=True)
