"""MCP marketplace lifecycle: submit, review, publish, install, and suspend.

Marketplace rows are credential-free reviewed snapshots.  Installation creates
or updates an ``AdminMcpServer`` owned by the installing user (or global for an
admin install), encrypting install-time header values before persistence.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from core.config.mcp_marketplace_catalog import CURATED_MCP_MARKET_ITEMS
from core.db.models import (
    AdminMcpServer,
    McpMarketInstallation,
    McpMarketItem,
    McpMarketSubmission,
    McpMarketVersion,
)
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.services import marketplace_listing as ml
from core.services.mcp_management_service import (
    assess_mcp_risk,
    auth_schema_from_headers,
    decrypt_mcp_headers,
    encrypt_mcp_headers,
    materialize_mcp_http_connection,
    mcp_oauth_bundle_storage_key,
    mcp_query_secret_storage_key,
    mcp_url_override_storage_key,
    probe_mcp_connectivity,
    refresh_mcp_caches,
    tool_snapshot_hash,
    validate_remote_mcp_url,
)
from sqlalchemy.orm import Session

MCP_MARKET_CATEGORIES = [
    "信息检索",
    "数据分析",
    "内容创作",
    "办公协作",
    "研发工具",
    "业务系统",
    "自动化",
    "通用工具",
]
_SECRET_QUERY_TERMS = ("key", "token", "secret", "password", "credential")


def ensure_curated_market_items(db: Session) -> List[str]:
    """Seed credential-free official templates once, without resurrecting deletions."""
    seeded: List[str] = []
    updated = False
    now = datetime.utcnow()
    for definition in CURATED_MCP_MARKET_ITEMS:
        slug = str(definition["slug"])
        # Soft-deleted rows deliberately count as existing: an administrator's
        # removal choice must survive application restarts.
        existing_item = db.query(McpMarketItem).filter(McpMarketItem.slug == slug).first()
        if existing_item:
            if existing_item.deleted_at is None:
                existing_version = (
                    db.query(McpMarketVersion)
                    .filter(McpMarketVersion.version_id == existing_item.latest_version_id)
                    .first()
                )
                if existing_version and existing_version.approved_by == "system-curated":
                    schema = list(definition.get("auth_schema") or [])
                    auth_config = _normalize_auth_config(definition.get("auth_config"), schema)
                    report = dict(existing_version.risk_report or {})
                    report.update(
                        {
                            "discovery_mode": "per_install",
                            "install_notice": str(definition.get("install_notice") or ""),
                            "docs_url": str(definition.get("docs_url") or ""),
                        }
                    )
                    description = str(definition.get("description") or "")
                    user_intro = str(definition.get("user_intro") or "")
                    icon = str(definition.get("icon") or "")
                    changed = (
                        list(existing_version.auth_schema or []) != schema
                        or dict(existing_version.auth_config or {}) != auth_config
                        or dict(existing_version.risk_report or {}) != report
                        or (existing_item.description or "") != description
                        or (existing_item.user_intro or "") != user_intro
                        or (existing_item.icon or "") != icon
                    )
                    if changed:
                        existing_version.auth_schema = schema
                        existing_version.auth_config = auth_config
                        existing_version.risk_report = report
                        existing_item.description = description
                        existing_item.user_intro = user_intro
                        existing_item.icon = icon
                        existing_item.updated_at = now
                        server_ids = [
                            row.server_id
                            for row in db.query(McpMarketInstallation)
                            .filter(McpMarketInstallation.slug == slug)
                            .all()
                        ]
                        if server_ids:
                            (
                                db.query(AdminMcpServer)
                                .filter(AdminMcpServer.server_id.in_(server_ids))
                                .update({AdminMcpServer.icon: icon}, synchronize_session=False)
                            )
                        updated = True
            continue
        tools = list(definition.get("tools") or [])
        assessed_level, assessed_report = assess_mcp_risk(tools)
        risk_level = str(definition.get("risk_level") or assessed_level)
        risk_report = dict(assessed_report)
        if risk_level == "high" and not risk_report.get("high_risk_tools"):
            risk_report["high_risk_tools"] = [str(tool.get("name") or "") for tool in tools]
        risk_report.update(
            {
                "requires_confirmation": risk_level == "high",
                "discovery_mode": "per_install",
                "install_notice": str(definition.get("install_notice") or ""),
                "docs_url": str(definition.get("docs_url") or ""),
            }
        )
        version_id = f"mcpver_curated_{slug}"
        item = McpMarketItem(
            slug=slug,
            display_name=str(definition["display_name"]),
            description=str(definition.get("description") or ""),
            user_intro=str(definition.get("user_intro") or ""),
            category=_validate_category(str(definition.get("category") or "通用工具")),
            tags=list(definition.get("tags") or []),
            icon=str(definition.get("icon") or ""),
            publisher_id=None,
            publisher_name=str(definition.get("publisher_name") or "HugAgentOS"),
            source="admin",
            latest_version_id=version_id,
            status="active",
            status_reason=None,
            last_verified_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        db.add(
            McpMarketVersion(
                version_id=version_id,
                slug=slug,
                version=_normalize_version(str(definition.get("version") or "1.0.0")),
                transport=str(definition.get("transport") or "streamable_http"),
                url=str(definition["url"]),
                auth_schema=list(definition.get("auth_schema") or []),
                auth_config=_normalize_auth_config(
                    definition.get("auth_config"),
                    list(definition.get("auth_schema") or []),
                ),
                tools_json=tools,
                tool_hash=tool_snapshot_hash(tools),
                risk_level=risk_level,
                risk_report=risk_report,
                source_server_id=None,
                approved_by="system-curated",
                approved_at=now,
                created_at=now,
            )
        )
        seeded.append(slug)
    if not seeded and not updated:
        return []
    db.commit()
    for slug in seeded:
        ml.set_listing_enabled(db, ml.KIND_MCP, slug, True, updated_by="system-curated")
    return seeded


def _validate_category(category: str) -> str:
    value = (category or "").strip()
    if value not in MCP_MARKET_CATEGORIES:
        raise BadRequestError(message=f"MCP 市场分类必须是：{', '.join(MCP_MARKET_CATEGORIES)}")
    return value


def _normalize_auth_config(
    auth_config: Any,
    auth_schema: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return a backwards-compatible none/token/oauth2 authentication contract."""
    config = dict(auth_config or {}) if isinstance(auth_config, dict) else {}
    normalized: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in config.get("methods") or []:
        if not isinstance(raw, dict):
            continue
        method = dict(raw)
        method_id = str(method.get("id") or method.get("type") or "").strip()
        method_type = str(method.get("type") or "").strip()
        if not method_id or method_id in seen or method_type not in {"none", "token", "oauth2"}:
            continue
        method["id"] = method_id
        method["type"] = method_type
        method["label"] = str(method.get("label") or method_type)
        if method_type == "oauth2":
            method["client_registration"] = str(
                method.get("client_registration") or "dynamic_or_manual"
            )
            method["scopes"] = [str(scope) for scope in (method.get("scopes") or []) if str(scope)]
        normalized.append(method)
        seen.add(method_id)
    if not normalized:
        inferred = "token" if auth_schema else "none"
        normalized = [
            {"id": inferred, "type": inferred, "label": "Token" if auth_schema else "无需认证"}
        ]
    default_method = str(config.get("default_method") or normalized[0]["id"])
    if default_method not in {method["id"] for method in normalized}:
        default_method = str(normalized[0]["id"])
    return {"default_method": default_method, "methods": normalized}


def _credential_free_connection(row: AdminMcpServer) -> tuple[str, List[Dict[str, Any]]]:
    """Strip likely query-string secrets and expose only their install-time schema."""
    parsed = urlparse(row.url or "")
    public_query: List[tuple[str, str]] = []
    auth_schema = auth_schema_from_headers(row.headers)
    known_keys = {str(field.get("key") or "") for field in auth_schema}
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
        looks_secret = any(term in name.lower() for term in _SECRET_QUERY_TERMS)
        credential_key = f"QUERY_{normalized or 'SECRET'}"
        if looks_secret and value:
            if credential_key not in known_keys:
                auth_schema.append(
                    {
                        "key": credential_key,
                        "label": name,
                        "target": "query",
                        "name": name,
                        "required": True,
                        "secret": True,
                    }
                )
                known_keys.add(credential_key)
            continue
        public_query.append((name, value))
    template_url = urlunparse(parsed._replace(query=urlencode(public_query)))
    return template_url, auth_schema


def _normalize_version(version: str) -> str:
    value = (version or "1.0.0").strip()
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,49}", value):
        raise BadRequestError(message="版本号格式无效")
    return value


def _new_slug(db: Session, display_name: str, source_server_id: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    base = (ascii_slug or f"mcp-{source_server_id[-12:].lower()}")[:100]
    slug = base
    suffix = 2
    while (
        db.query(McpMarketItem.slug).filter(McpMarketItem.slug == slug).first()
        or db.query(McpMarketSubmission.slug)
        .filter(
            McpMarketSubmission.slug == slug,
            McpMarketSubmission.status.in_(["pending", "approved"]),
            McpMarketSubmission.deleted_at.is_(None),
        )
        .first()
    ):
        slug = f"{base[:115]}-{suffix}"
        suffix += 1
    return slug


def _source_slug(db: Session, row: AdminMcpServer, owner_user_id: str) -> str:
    previous = (
        db.query(McpMarketSubmission.slug)
        .filter(
            McpMarketSubmission.source_server_id == row.server_id,
            McpMarketSubmission.owner_user_id == owner_user_id,
            McpMarketSubmission.deleted_at.is_(None),
        )
        .order_by(McpMarketSubmission.created_at.desc())
        .first()
    )
    return str(previous[0]) if previous else _new_slug(db, row.display_name, row.server_id)


def _version_for_item(db: Session, item: McpMarketItem) -> McpMarketVersion:
    version = (
        db.query(McpMarketVersion)
        .filter(McpMarketVersion.version_id == item.latest_version_id)
        .first()
    )
    if not version:
        raise ResourceNotFoundError("mcp_market_version", item.latest_version_id)
    return version


def _installed_slug_set(db: Session, owner_user_id: Optional[str]) -> set[str]:
    query = db.query(McpMarketInstallation.slug).filter(
        McpMarketInstallation.status.in_(["active", "suspended"])
    )
    query = (
        query.filter(McpMarketInstallation.owner_user_id.is_(None))
        if owner_user_id is None
        else query.filter(McpMarketInstallation.owner_user_id == owner_user_id)
    )
    return {str(row[0]) for row in query.all()}


def _item_dict(
    item: McpMarketItem,
    version: McpMarketVersion,
    *,
    installed: bool = False,
) -> Dict[str, Any]:
    auth_schema = list(version.auth_schema or [])
    auth_config = _normalize_auth_config(version.auth_config, auth_schema)
    return {
        "slug": item.slug,
        "display_name": item.display_name,
        "description": item.description or "",
        "summary": item.description or "",
        "user_intro": item.user_intro or "",
        "category": item.category,
        "tags": list(item.tags or []),
        "icon": item.icon or "",
        "publisher_name": item.publisher_name or "",
        "source": item.source,
        "version": version.version,
        "version_id": version.version_id,
        "transport": version.transport,
        "url_origin": _safe_origin(version.url),
        "auth_schema": auth_schema,
        "auth_config": auth_config,
        "requires_auth": any(method["type"] != "none" for method in auth_config["methods"]),
        "tools": list(version.tools_json or []),
        "tool_count": len(version.tools_json or []),
        "tool_hash": version.tool_hash,
        "risk_level": version.risk_level,
        "risk_report": dict(version.risk_report or {}),
        "status": item.status,
        "status_reason": item.status_reason or "",
        "last_verified_at": item.last_verified_at.isoformat() if item.last_verified_at else None,
        "installed": installed,
        "deletable": True,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _safe_origin(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url or "")
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def list_market_items(
    db: Session,
    *,
    owner_user_id: Optional[str],
    viewer_user_id: Optional[str],
    include_disabled: bool = False,
) -> Dict[str, Any]:
    query = db.query(McpMarketItem).filter(McpMarketItem.deleted_at.is_(None))
    if not include_disabled:
        query = query.filter(McpMarketItem.status == "active")
    rows = query.order_by(McpMarketItem.updated_at.desc(), McpMarketItem.slug).all()
    installed = _installed_slug_set(db, owner_user_id)
    items = [
        _item_dict(row, _version_for_item(db, row), installed=row.slug in installed) for row in rows
    ]
    items = ml.annotate_and_filter(
        db,
        ml.KIND_MCP,
        items,
        id_key="slug",
        include_disabled=include_disabled,
        viewer_user_id=viewer_user_id,
    )
    categories = [
        category
        for category in MCP_MARKET_CATEGORIES
        if any(i["category"] == category for i in items)
    ]
    return {"items": items, "categories": categories}


def get_market_item(
    db: Session,
    slug: str,
    *,
    owner_user_id: Optional[str],
    viewer_user_id: Optional[str],
    admin: bool = False,
) -> Dict[str, Any]:
    item = db.query(McpMarketItem).filter(McpMarketItem.slug == slug).first()
    if not item or (not admin and item.status != "active"):
        raise ResourceNotFoundError("mcp_market_item", slug)
    if not admin:
        ml.ensure_item_visible(db, ml.KIND_MCP, slug, viewer_user_id, resource="mcp_market_item")
    data = _item_dict(
        item, _version_for_item(db, item), installed=slug in _installed_slug_set(db, owner_user_id)
    )
    state = ml.annotate_and_filter(
        db,
        ml.KIND_MCP,
        [data],
        id_key="slug",
        include_disabled=admin,
        viewer_user_id=viewer_user_id,
    )
    if not state:
        raise ResourceNotFoundError("mcp_market_item", slug)
    return state[0]


async def submit_to_marketplace(
    db: Session,
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

    await validate_remote_mcp_url(row.url, require_https=True)
    ok, error = await probe_mcp_connectivity(row, db)
    if not ok:
        raise BadRequestError(message=f"MCP 上架检测失败：{error}")
    tools = list(row.tools_json or [])
    if not tools:
        raise BadRequestError(message="MCP 未发现任何工具，不能申请上架")
    risk_level, risk_report = assess_mcp_risk(tools)
    template_url, auth_schema = _credential_free_connection(row)
    now = datetime.utcnow()
    submission = McpMarketSubmission(
        submission_id=f"mcpsub_{uuid.uuid4().hex}",
        slug=_source_slug(db, row, owner_user_id),
        source_server_id=row.server_id,
        owner_user_id=owner_user_id,
        submitter_name=submitter_name or "",
        display_name=row.display_name,
        description=(summary or row.description or "").strip(),
        user_intro=row.user_intro,
        category=_validate_category(category),
        tags=list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip())),
        icon=row.icon,
        version=_normalize_version(version),
        transport=row.transport,
        url=template_url,
        auth_schema=auth_schema,
        auth_config=_normalize_auth_config(None, auth_schema),
        tools_json=tools,
        tool_hash=tool_snapshot_hash(tools),
        risk_level=risk_level,
        risk_report=risk_report,
        note=(note or "").strip(),
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return _submission_dict(submission)


def _submission_dict(row: McpMarketSubmission, *, detail: bool = False) -> Dict[str, Any]:
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
        "url_origin": _safe_origin(row.url),
        "auth_schema": list(row.auth_schema or []),
        "auth_config": _normalize_auth_config(row.auth_config, list(row.auth_schema or [])),
        "tool_count": len(row.tools_json or []),
        "tool_hash": row.tool_hash,
        "risk_level": row.risk_level,
        "risk_report": dict(row.risk_report or {}),
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
    return [_submission_dict(row) for row in rows]


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
    row.deleted_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.commit()


def list_submissions(db: Session, status: Optional[str] = None) -> List[Dict[str, Any]]:
    query = db.query(McpMarketSubmission).filter(McpMarketSubmission.deleted_at.is_(None))
    if status:
        query = query.filter(McpMarketSubmission.status == status)
    return [
        _submission_dict(row) for row in query.order_by(McpMarketSubmission.created_at.desc()).all()
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
    return _submission_dict(row, detail=True)


def _publish_snapshot(
    db: Session,
    *,
    slug: str,
    display_name: str,
    description: str,
    user_intro: Optional[str],
    category: str,
    tags: List[str],
    icon: Optional[str],
    publisher_id: Optional[str],
    publisher_name: str,
    source: str,
    version: str,
    transport: str,
    url: str,
    auth_schema: List[Dict[str, Any]],
    auth_config: Optional[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    source_server_id: Optional[str],
    approved_by: Optional[str],
) -> McpMarketItem:
    if (
        db.query(McpMarketVersion)
        .filter(McpMarketVersion.slug == slug, McpMarketVersion.version == version)
        .first()
    ):
        raise BadRequestError(message=f"市场条目 {slug} 已存在版本 {version}，请提升版本号")
    now = datetime.utcnow()
    # Include a soft-deleted row so a later reviewed version can resurrect the
    # same stable marketplace slug without colliding on the primary key.
    item = db.query(McpMarketItem).filter(McpMarketItem.slug == slug).first()
    version_id = f"mcpver_{uuid.uuid4().hex}"
    risk_level, risk_report = assess_mcp_risk(tools)
    if item is None:
        item = McpMarketItem(
            slug=slug,
            display_name=display_name,
            description=description,
            user_intro=user_intro,
            category=_validate_category(category),
            tags=tags,
            icon=icon,
            publisher_id=publisher_id,
            publisher_name=publisher_name,
            source=source,
            latest_version_id=version_id,
            status="active",
            last_verified_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        db.flush()
    else:
        item.deleted_at = None
        item.display_name = display_name
        item.description = description
        item.user_intro = user_intro
        item.category = _validate_category(category)
        item.tags = tags
        item.icon = icon
        item.publisher_id = publisher_id
        item.publisher_name = publisher_name
        item.latest_version_id = version_id
        item.status = "active"
        item.status_reason = None
        item.last_verified_at = now
        item.updated_at = now
    db.add(
        McpMarketVersion(
            version_id=version_id,
            slug=slug,
            version=_normalize_version(version),
            transport=transport,
            url=url,
            auth_schema=auth_schema,
            auth_config=_normalize_auth_config(auth_config, auth_schema),
            tools_json=tools,
            tool_hash=tool_snapshot_hash(tools),
            risk_level=risk_level,
            risk_report=risk_report,
            source_server_id=source_server_id,
            approved_by=approved_by,
            approved_at=now,
            created_at=now,
        )
    )
    return item


async def review_submission(
    db: Session,
    submission_id: str,
    *,
    approve: bool,
    review_note: str = "",
    category: str = "",
    reviewed_by: Optional[str] = None,
) -> Dict[str, Any]:
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
    now = datetime.utcnow()
    if approve:
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
        source_template_url, source_auth_schema = _credential_free_connection(source)
        if source.transport != row.transport or source_template_url.strip() != row.url.strip():
            raise BadRequestError(message="MCP 连接地址或传输方式已变化，请申请人重新提交")
        if source_auth_schema != list(row.auth_schema or []):
            raise BadRequestError(message="MCP 认证参数已变化，请申请人重新提交")
        await validate_remote_mcp_url(source.url or "", require_https=True)
        ok, error = await probe_mcp_connectivity(source, db)
        if not ok:
            db.rollback()
            raise BadRequestError(message=f"MCP 审核复检失败：{error}")
        if tool_snapshot_hash(source.tools_json or []) != row.tool_hash:
            db.rollback()
            raise BadRequestError(message="MCP 工具或参数结构已变化，请申请人重新提交")
        _publish_snapshot(
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
            auth_config=_normalize_auth_config(row.auth_config, list(row.auth_schema or [])),
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
    return _submission_dict(row, detail=True)


async def publish_admin_server(
    db: Session,
    source_server_id: str,
    *,
    category: str,
    version: str,
    summary: str = "",
    tags: Optional[List[str]] = None,
    publisher_name: str = "管理员",
    link_as_installation: bool = True,
) -> Dict[str, Any]:
    row = (
        db.query(AdminMcpServer)
        .filter(
            AdminMcpServer.server_id == source_server_id,
            AdminMcpServer.owner_user_id.is_(None),
        )
        .first()
    )
    if not row:
        raise ResourceNotFoundError("admin_mcp_server", source_server_id)
    if row.transport not in ("streamable_http", "sse") or not row.url:
        raise BadRequestError(message="MCP 市场仅支持远程 HTTP/SSE 服务；stdio 请通过插件市场分发")
    if (row.extra_config or {}).get("market_slug"):
        raise BadRequestError(message="从 MCP 市场安装的实例不能重复发布")
    await validate_remote_mcp_url(
        row.url,
        allow_private_network=True,
        require_https=False,
    )
    ok, error = await probe_mcp_connectivity(row, db)
    if not ok:
        raise BadRequestError(message=f"MCP 发布检测失败：{error}")
    tools = list(row.tools_json or [])
    if not tools:
        raise BadRequestError(message="MCP 未发现任何工具，不能发布")
    existing = (
        db.query(McpMarketItem)
        .join(McpMarketVersion, McpMarketVersion.version_id == McpMarketItem.latest_version_id)
        .filter(McpMarketVersion.source_server_id == source_server_id)
        .first()
    )
    slug = existing.slug if existing else _new_slug(db, row.display_name, source_server_id)
    template_url, auth_schema = _credential_free_connection(row)
    item = _publish_snapshot(
        db,
        slug=slug,
        display_name=row.display_name,
        description=(summary or row.description or "").strip(),
        user_intro=row.user_intro,
        category=category,
        tags=list(dict.fromkeys(str(tag).strip() for tag in (tags or []) if str(tag).strip())),
        icon=row.icon,
        publisher_id=None,
        publisher_name=publisher_name,
        source="admin",
        version=version,
        transport=row.transport,
        url=template_url,
        auth_schema=auth_schema,
        auth_config=_normalize_auth_config(None, auth_schema),
        tools=tools,
        source_server_id=row.server_id,
        approved_by=publisher_name,
    )
    # Existing global MCPs are linked as an installation.  A server created
    # through the marketplace-first flow remains a disabled source template
    # until an administrator explicitly installs it from the market.
    db.flush()
    if link_as_installation:
        installation = (
            db.query(McpMarketInstallation)
            .filter(McpMarketInstallation.server_id == row.server_id)
            .first()
        )
        if installation is None:
            db.add(
                McpMarketInstallation(
                    install_id=f"mcpins_{uuid.uuid4().hex}",
                    slug=item.slug,
                    version_id=item.latest_version_id,
                    server_id=row.server_id,
                    owner_user_id=None,
                    status="active",
                    installed_by="admin",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )
        else:
            installation.slug = item.slug
            installation.version_id = item.latest_version_id
            installation.status = "active"
            installation.updated_at = datetime.utcnow()
    else:
        for installation in (
            db.query(McpMarketInstallation)
            .filter(McpMarketInstallation.server_id == row.server_id)
            .all()
        ):
            db.delete(installation)
        row.is_enabled = False
        row.extra_config = {
            **dict(row.extra_config or {}),
            "market_source_only": True,
            "market_source_slug": item.slug,
        }
    row.headers = encrypt_mcp_headers(decrypt_mcp_headers(row.headers))
    db.commit()
    ml.set_listing_enabled(db, ml.KIND_MCP, item.slug, True, updated_by=publisher_name)
    refresh_mcp_caches()
    return _item_dict(item, _version_for_item(db, item), installed=link_as_installation)


def _credential_storage_key(field: Dict[str, Any]) -> str:
    target = str(field.get("target") or "header")
    name = str(field.get("name") or field.get("key") or "").strip()
    if target == "query":
        return mcp_query_secret_storage_key(name)
    if target == "url":
        return mcp_url_override_storage_key()
    return name


def _materialize_credential_value(field: Dict[str, Any], value: str) -> str:
    prefix = str(field.get("prefix") or "")
    if prefix and not value.lower().startswith(prefix.lower()):
        return f"{prefix}{value}"
    return value


def _installation_secrets(
    version: McpMarketVersion,
    credentials: Dict[str, str],
    existing_headers: Dict[str, str] | None = None,
    *,
    auth_method: str,
    oauth_bundle: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    stored: Dict[str, str] = {}
    missing: List[str] = []
    for field in version.auth_schema or []:
        method_ids = [str(value) for value in (field.get("methods") or []) if str(value)]
        if method_ids and auth_method not in method_ids:
            continue
        key = str(field.get("key") or "").strip()
        storage_key = _credential_storage_key(field)
        if not key or not storage_key:
            continue
        value = str(credentials.get(key) or "").strip()
        if value:
            stored[storage_key] = _materialize_credential_value(field, value)
        elif storage_key in (existing_headers or {}):
            stored[storage_key] = str((existing_headers or {})[storage_key])
        elif field.get("required", True):
            missing.append(str(field.get("label") or key))
    if missing:
        raise BadRequestError(message=f"请填写安装凭据：{', '.join(missing)}")
    oauth_key = mcp_oauth_bundle_storage_key()
    if oauth_bundle:
        stored[oauth_key] = json.dumps(oauth_bundle, ensure_ascii=False, separators=(",", ":"))
    elif auth_method == "oauth2" and oauth_key in (existing_headers or {}):
        stored[oauth_key] = str((existing_headers or {})[oauth_key])
    return stored


async def install_market_item(
    db: Session,
    slug: str,
    *,
    owner_user_id: Optional[str],
    installed_by: Optional[str],
    credentials: Optional[Dict[str, str]] = None,
    auth_method: Optional[str] = None,
    oauth_bundle: Optional[Dict[str, Any]] = None,
    confirm_high_risk: bool = False,
) -> Dict[str, Any]:
    item = (
        db.query(McpMarketItem)
        .filter(
            McpMarketItem.slug == slug,
            McpMarketItem.deleted_at.is_(None),
            McpMarketItem.status == "active",
        )
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    if owner_user_id is not None:
        if slug in ml.get_disabled_ids(db, ml.KIND_MCP):
            raise ResourceNotFoundError("mcp_market_item", slug)
        ml.ensure_item_visible(db, ml.KIND_MCP, slug, owner_user_id, resource="mcp_market_item")
    version = _version_for_item(db, item)
    auth_config = _normalize_auth_config(version.auth_config, list(version.auth_schema or []))
    selected_method = str(auth_method or auth_config["default_method"])
    selected = next(
        (method for method in auth_config["methods"] if method["id"] == selected_method),
        None,
    )
    if selected is None:
        raise BadRequestError(message="不支持所选 MCP 认证方式")
    if selected["type"] == "oauth2" and not oauth_bundle:
        raise BadRequestError(message="请先完成 OAuth 登录")
    if selected["type"] != "oauth2" and oauth_bundle:
        raise BadRequestError(message="当前认证方式不能写入 OAuth 凭据")
    if version.risk_level == "high" and not confirm_high_risk:
        raise BadRequestError(message="该 MCP 包含高风险操作，安装前必须明确确认风险")

    existing = db.query(McpMarketInstallation).filter(McpMarketInstallation.slug == slug)
    existing = (
        existing.filter(McpMarketInstallation.owner_user_id.is_(None))
        if owner_user_id is None
        else existing.filter(McpMarketInstallation.owner_user_id == owner_user_id)
    ).first()
    existing_server = (
        db.query(AdminMcpServer).filter(AdminMcpServer.server_id == existing.server_id).first()
        if existing
        else None
    )
    if existing_server is None and owner_user_id is None and version.source_server_id:
        source_candidate = (
            db.query(AdminMcpServer)
            .filter(
                AdminMcpServer.server_id == version.source_server_id,
                AdminMcpServer.owner_user_id.is_(None),
            )
            .first()
        )
        if source_candidate and (source_candidate.extra_config or {}).get("market_source_only"):
            existing_server = source_candidate
    old_headers = decrypt_mcp_headers(existing_server.headers) if existing_server else {}
    headers = _installation_secrets(
        version,
        dict(credentials or {}),
        old_headers,
        auth_method=selected_method,
        oauth_bundle=oauth_bundle,
    )
    runtime_url, _ = materialize_mcp_http_connection(version.url, headers)
    user_install = owner_user_id is not None
    await validate_remote_mcp_url(
        runtime_url,
        allow_private_network=item.source == "admin" and not user_install,
        require_https=user_install or item.source != "admin",
    )

    now = datetime.utcnow()
    server_id = existing_server.server_id if existing_server else f"mmcp_{uuid.uuid4().hex[:20]}"
    candidate = AdminMcpServer(
        server_id=server_id,
        display_name=item.display_name,
        description=item.description or "",
        user_intro=item.user_intro,
        transport=version.transport,
        command=None,
        args=[],
        url=version.url,
        env_vars={},
        env_inherit=[],
        headers=headers,
        is_stable=False,
        is_enabled=True,
        sort_order=0,
        extra_config={
            **(
                {
                    key: value
                    for key, value in dict(existing_server.extra_config or {}).items()
                    if key not in {"market_source_only", "market_source_slug"}
                }
                if existing_server
                else {}
            ),
            "market_slug": slug,
            "market_version": version.version,
            "risk_level": version.risk_level,
            "risk_report": dict(version.risk_report or {}),
            "auth_method": selected_method,
        },
        tools_json=list(version.tools_json or []),
        icon=item.icon,
        owner_user_id=owner_user_id,
        source_plugin=None,
        created_by=installed_by,
        created_at=existing_server.created_at if existing_server else now,
        updated_at=now,
    )

    ok, error = await probe_mcp_connectivity(candidate, db)
    if not ok:
        raise BadRequestError(message=f"MCP 安装检测失败：{error}")
    discovery_mode = str((version.risk_report or {}).get("discovery_mode") or "reviewed")
    effective_risk, effective_report = assess_mcp_risk(candidate.tools_json or [])
    if discovery_mode == "per_install":
        if effective_risk == "high" and not confirm_high_risk:
            raise BadRequestError(message="该 MCP 实际返回了高风险工具，请确认风险后重新安装")
        candidate.extra_config = {
            **dict(candidate.extra_config or {}),
            "risk_level": effective_risk,
            "risk_report": effective_report,
            "discovery_mode": discovery_mode,
        }
    else:
        actual_hash = tool_snapshot_hash(candidate.tools_json or [])
        if actual_hash != version.tool_hash:
            item.status = "changed"
            item.status_reason = "远程工具或参数结构已变化，等待管理员重新审核"
            item.updated_at = now
            db.commit()
            raise BadRequestError(message="远程 MCP 工具清单已发生变化，已暂停安装并等待重新审核")
        effective_risk = version.risk_level

    server = existing_server or AdminMcpServer(server_id=server_id)
    for field in (
        "display_name",
        "description",
        "user_intro",
        "transport",
        "command",
        "args",
        "url",
        "env_vars",
        "env_inherit",
        "is_stable",
        "is_enabled",
        "sort_order",
        "extra_config",
        "tools_json",
        "icon",
        "owner_user_id",
        "source_plugin",
        "created_by",
        "updated_at",
    ):
        setattr(server, field, getattr(candidate, field))
    server.headers = encrypt_mcp_headers(headers)
    if not existing_server:
        server.created_at = now
        db.add(server)
        db.flush()
    if existing:
        previous_version_id = existing.version_id
        existing.version_id = version.version_id
        existing.status = "active"
        existing.installed_by = installed_by
        existing.updated_at = now
        action = "updated" if previous_version_id != version.version_id else "existing"
    else:
        existing = McpMarketInstallation(
            install_id=f"mcpins_{uuid.uuid4().hex}",
            slug=slug,
            version_id=version.version_id,
            server_id=server.server_id,
            owner_user_id=owner_user_id,
            status="active",
            installed_by=installed_by,
            created_at=now,
            updated_at=now,
        )
        db.add(existing)
        action = "installed"
    db.commit()
    refresh_mcp_caches()
    return {
        "install_id": existing.install_id,
        "server_id": server.server_id,
        "slug": slug,
        "version": version.version,
        "risk_level": effective_risk,
        "action": action,
    }


def set_market_enabled(
    db: Session, slug: str, enabled: bool, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    return ml.set_listing_enabled(db, ml.KIND_MCP, slug, enabled, updated_by=updated_by)


def update_market_item(
    db: Session,
    slug: str,
    *,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    user_intro: Optional[str] = None,
    category: Optional[str] = None,
    tags: Optional[List[str]] = None,
    icon: Optional[str] = None,
) -> Dict[str, Any]:
    """Edit marketplace presentation metadata without mutating its reviewed snapshot.

    Tool schemas, transport, endpoint templates, authentication contracts, and
    version numbers remain immutable in ``McpMarketVersion``.  Presentation
    fields are propagated to existing concrete installations so Config and the
    capability center do not show two names for the same marketplace MCP.
    """
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)

    if display_name is not None:
        normalized_name = display_name.strip()
        if not normalized_name:
            raise BadRequestError(message="MCP 市场名称不能为空")
        item.display_name = normalized_name
    if description is not None:
        item.description = description.strip()
    if user_intro is not None:
        item.user_intro = user_intro.strip() or None
    if category is not None:
        item.category = _validate_category(category)
    if tags is not None:
        item.tags = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    if icon is not None:
        item.icon = icon.strip() or None
    item.updated_at = datetime.utcnow()

    installation_server_ids = [
        str(row[0])
        for row in db.query(McpMarketInstallation.server_id)
        .filter(McpMarketInstallation.slug == slug)
        .all()
    ]
    installations: List[AdminMcpServer] = []
    if installation_server_ids:
        installations = (
            db.query(AdminMcpServer)
            .filter(AdminMcpServer.server_id.in_(installation_server_ids))
            .all()
        )
        for server in installations:
            server.display_name = item.display_name
            server.description = item.description or ""
            server.user_intro = item.user_intro
            server.icon = item.icon
            server.updated_at = item.updated_at

    db.commit()
    refresh_mcp_caches()
    result = get_market_item(
        db,
        slug,
        owner_user_id=None,
        viewer_user_id=None,
        admin=True,
    )
    result["updated_installations"] = len(installations)
    return result


def delete_market_item(
    db: Session, slug: str, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    version = _version_for_item(db, item)
    if version.source_server_id:
        source = (
            db.query(AdminMcpServer)
            .filter(AdminMcpServer.server_id == version.source_server_id)
            .first()
        )
        if source and (source.extra_config or {}).get("market_source_only"):
            db.delete(source)
    item.deleted_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    db.commit()
    ml.set_listing_enabled(db, ml.KIND_MCP, slug, False, updated_by=updated_by)
    return {"slug": slug, "deleted": True}


def set_suspended(
    db: Session,
    slug: str,
    *,
    suspended: bool,
    reason: str = "",
) -> Dict[str, Any]:
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    now = datetime.utcnow()
    item.status = "suspended" if suspended else "active"
    item.status_reason = (reason or "").strip() if suspended else None
    item.updated_at = now
    affected = 0
    installs = db.query(McpMarketInstallation).filter(McpMarketInstallation.slug == slug).all()
    if suspended:
        server_ids = []
        for install in installs:
            install.status = "suspended"
            install.updated_at = now
            server_ids.append(install.server_id)
        if server_ids:
            affected = (
                db.query(AdminMcpServer)
                .filter(AdminMcpServer.server_id.in_(server_ids))
                .update({AdminMcpServer.is_enabled: False}, synchronize_session=False)
            )
    else:
        server_ids = []
        for install in installs:
            if install.status != "suspended":
                continue
            install.status = "active"
            install.updated_at = now
            server_ids.append(install.server_id)
        if server_ids:
            affected = (
                db.query(AdminMcpServer)
                .filter(AdminMcpServer.server_id.in_(server_ids))
                .update({AdminMcpServer.is_enabled: True}, synchronize_session=False)
            )
    db.commit()
    refresh_mcp_caches()
    return {
        "slug": slug,
        "status": item.status,
        "disabled_installations": affected if suspended else 0,
        "restored_installations": affected if not suspended else 0,
    }


async def revalidate_market_item(db: Session, slug: str) -> Dict[str, Any]:
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    version = _version_for_item(db, item)
    if str((version.risk_report or {}).get("discovery_mode") or "") == "per_install":
        # Curated templates have no shared provider credential, so their live
        # tools are intentionally discovered with each installer's credential.
        # Revalidation checks only a fixed public endpoint; private endpoint
        # templates are validated when the user installs them.
        has_endpoint_override = any(
            str(field.get("target") or "") == "url" for field in version.auth_schema or []
        )
        if not has_endpoint_override:
            try:
                await validate_remote_mcp_url(version.url, require_https=True)
                item.status = "active"
                item.status_reason = None
            except BadRequestError as exc:
                item.status = "suspended"
                item.status_reason = f"官方端点安全复检失败：{exc}"
        else:
            item.status = "active"
            item.status_reason = "个人端点将在安装时使用用户凭据验证"
        item.last_verified_at = datetime.utcnow()
        item.updated_at = datetime.utcnow()
        db.commit()
        return _item_dict(item, version, installed=False)
    source = (
        db.query(AdminMcpServer)
        .filter(AdminMcpServer.server_id == version.source_server_id)
        .first()
        if version.source_server_id
        else None
    )
    if not source:
        item.status = "changed"
        item.status_reason = "原始 MCP 连接已不存在，无法继续验证"
        item.last_verified_at = datetime.utcnow()
        item.updated_at = datetime.utcnow()
        db.commit()
        return _item_dict(item, version, installed=False)
    source_template_url, source_auth_schema = _credential_free_connection(source)
    if source.transport != version.transport or source_template_url.strip() != version.url.strip():
        item.status = "changed"
        item.status_reason = "原始 MCP 的连接地址或传输方式已变化，需发布新版本"
        item.last_verified_at = datetime.utcnow()
        item.updated_at = datetime.utcnow()
        db.commit()
        return _item_dict(item, version, installed=False)
    if source_auth_schema != list(version.auth_schema or []):
        item.status = "changed"
        item.status_reason = "原始 MCP 的认证参数已变化，需发布新版本"
        item.last_verified_at = datetime.utcnow()
        item.updated_at = datetime.utcnow()
        db.commit()
        return _item_dict(item, version, installed=False)
    try:
        await validate_remote_mcp_url(
            version.url,
            allow_private_network=item.source == "admin",
            require_https=item.source != "admin",
        )
    except BadRequestError as exc:
        set_suspended(db, slug, suspended=True, reason=f"远程地址安全复检失败：{exc}")
        return _item_dict(item, version, installed=False)
    ok, error = await probe_mcp_connectivity(source, db)
    if not ok:
        item.status = "changed"
        item.status_reason = f"复检连接失败：{error}"
    else:
        actual_hash = tool_snapshot_hash(source.tools_json or [])
        if actual_hash == version.tool_hash:
            item.status = "active"
            item.status_reason = None
        else:
            item.status = "changed"
            item.status_reason = "远程工具或参数结构已变化，需发布新版本并重新审核"
    item.last_verified_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    db.commit()
    return _item_dict(item, version, installed=False)
