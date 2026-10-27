"""mcp revalidation responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
from typing import Any, Dict, Optional
from core.db.models import AdminMcpServer
from features.marketplace.models import McpMarketItem
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.capabilities.mcp.management import probe_mcp_connectivity, tool_snapshot_hash, validate_remote_mcp_url
from core.db.threads import run_in_session
from sqlalchemy.orm import Session
from features.marketplace import mcp_administration
from features.marketplace import mcp_listing
from features.marketplace import mcp_validation
from features.marketplace import mcp_versions


def _listing(db: Session, slug: str):
    item = (
        db.query(McpMarketItem)
        .filter(McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None))
        .first()
    )
    if not item:
        raise ResourceNotFoundError("mcp_market_item", slug)
    return item, mcp_versions.version_for_item(db, item)


def _settle(db: Session, item, version, status: str, reason: Optional[str]) -> Dict[str, Any]:
    item.status = status
    item.status_reason = reason
    item.last_verified_at = utc_now()
    item.updated_at = utc_now()
    db.commit()
    return mcp_listing.item_dict(db, item, version, installed=False)


def _plan(db: Session, slug: str) -> Dict[str, Any]:
    """Decide what has to be checked remotely; settles the cases that need no network."""
    item, version = _listing(db, slug)
    if str((version.listing_notice or {}).get("discovery_mode") or "") == "per_install":
        # Curated templates have no shared provider credential, so their live
        # tools are intentionally discovered with each installer's credential.
        # Revalidation checks only a fixed public endpoint; private endpoint
        # templates are validated when the user installs them.
        has_endpoint_override = any(
            str(field.get("target") or "") == "url" for field in version.auth_schema or []
        )
        if not has_endpoint_override:
            return {"kind": "endpoint", "url": version.url}
        return {"kind": "done", "result": _settle(db, item, version, "active", "个人端点将在安装时使用用户凭据验证")}
    source = (
        db.query(AdminMcpServer)
        .filter(AdminMcpServer.server_id == version.source_server_id)
        .first()
        if version.source_server_id
        else None
    )
    if source:
        source_template_url, source_auth_schema = mcp_validation.credential_free_connection(source)
        if (
            source.transport != version.transport
            or source_template_url.strip() != version.url.strip()
        ):
            return {"kind": "done", "result": _settle(
                db, item, version, "changed", "原始 MCP 的连接地址或传输方式已变化，需发布新版本"
            )}
        auth_policy = mcp_validation.normalize_auth_config(version.auth_config, list(version.auth_schema or []))
        if auth_policy["credential_mode"] == "auto" and source_auth_schema != list(
            version.auth_schema or []
        ):
            return {"kind": "done", "result": _settle(
                db, item, version, "changed", "原始 MCP 的认证参数已变化，需发布新版本"
            )}
        db.expunge(source)
    return {
        "kind": "probe",
        "url": version.url,
        "allow_private_network": item.source == "admin"
        or mcp_validation.self_service_private_network_allowed(),
        "target": source or mcp_versions.version_probe_candidate(item, version),
        "from_source": source is not None,
    }


def _record_endpoint(db: Session, slug: str, error: Optional[str]) -> Dict[str, Any]:
    item, version = _listing(db, slug)
    if error is None:
        return _settle(db, item, version, "active", None)
    return _settle(db, item, version, "suspended", f"官方端点安全复检失败：{error}")


def _record_unsafe(db: Session, slug: str, error: str) -> Dict[str, Any]:
    mcp_administration.set_suspended(db, slug, suspended=True, reason=f"远程地址安全复检失败：{error}")
    item, version = _listing(db, slug)
    return mcp_listing.item_dict(db, item, version, installed=False)


def _record_probe(
    db: Session, slug: str, ok: bool, error: str, discovered_tools, from_source: bool, accept_changes: bool
) -> Dict[str, Any]:
    item, version = _listing(db, slug)
    if not ok:
        prefix = "复检连接失败" if from_source else "原始 MCP 连接已不存在，版本端点复检失败"
        return _settle(db, item, version, "changed", f"{prefix}：{error}")
    if not discovered_tools:
        return _settle(db, item, version, "changed", "复检连接成功，但远程 MCP 未返回任何工具，暂不接受新快照")
    if tool_snapshot_hash(discovered_tools) == version.tool_hash:
        return _settle(db, item, version, "active", None)
    if accept_changes and item.source == "admin":
        previous_version = version.version
        accepted = mcp_versions.accept_revalidated_snapshot(db, item, version, discovered_tools)
        result = mcp_listing.item_dict(db, item, accepted, installed=False)
        result["snapshot_accepted"] = True
        result["previous_version"] = previous_version
        return result
    return _settle(db, item, version, "changed", "远程工具或参数结构已变化，需发布新版本并重新审核")


async def revalidate_market_item(slug: str, *, accept_changes: bool = False) -> Dict[str, Any]:
    """Re-check a listing against its live endpoint and record the verdict.

    Reading the listing, the remote checks and recording the result are three
    separate steps; no session waits on the remote server.
    """
    plan = await run_in_session(_plan, slug)
    if plan["kind"] == "done":
        return plan["result"]
    if plan["kind"] == "endpoint":
        try:
            await validate_remote_mcp_url(plan["url"], require_https=False)
            error = None
        except BadRequestError as exc:
            error = str(exc)
        return await run_in_session(_record_endpoint, slug, error)
    try:
        await validate_remote_mcp_url(
            plan["url"],
            allow_private_network=plan["allow_private_network"],
            require_https=False,
        )
    except BadRequestError as exc:
        return await run_in_session(_record_unsafe, slug, str(exc))
    target = plan["target"]
    ok, error = await probe_mcp_connectivity(target, validate=plan["from_source"])
    return await run_in_session(
        _record_probe, slug, ok, error, list(target.tools_json or []), plan["from_source"], accept_changes
    )
