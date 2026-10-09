"""Import actual enterprise records without copying source credentials into applications."""

from __future__ import annotations

import httpx
from core.infra.time import utc_now
from core.services.application_schema import (
    ColumnDefinition,
    IndustryImport,
    RecordBatch,
    TableDefinition,
)
from fastapi import HTTPException


def fetch_enterprises(keyword: str, limit: int) -> list[dict]:
    from mcp_servers.ai_chain_information_mcp.impl_company import _resolve_company_config

    try:
        url, token = _resolve_company_config()
        response = httpx.get(
            url + "/toModel/company/search",
            params={"keyword": keyword, "topNum": limit},
            headers={"Authorization": token},
            timeout=20,
        )
        response.raise_for_status()
        envelope = response.json()
    except (httpx.HTTPError, ValueError, RuntimeError):
        raise HTTPException(502, "Industry knowledge center is unavailable")
    if (
        not isinstance(envelope, dict)
        or not isinstance(envelope.get("header"), dict)
        or envelope["header"].get("code") != 200
        or not isinstance(envelope.get("body"), list)
        or any(not isinstance(item, dict) for item in envelope["body"])
    ):
        raise HTTPException(502, "Industry knowledge center returned an invalid response")
    return envelope["body"][:limit]


def import_enterprises(service, db, app_id: str, owner: str, payload: dict) -> dict:
    from core.config.settings import settings
    from core.plugins.management.queries import list_installed

    if settings.edition.edition != "ee":
        raise HTTPException(403, "Industry knowledge center requires the enterprise edition")
    plugin = next(
        (
            item
            for item in list_installed(db, owner, include_global=True)
            if item["slug"] == "industry-knowledge-center" and item.get("callable")
        ),
        None,
    )
    if plugin is None:
        raise HTTPException(403, "Industry knowledge center plugin is unavailable")
    definition = IndustryImport.model_validate(payload)
    service.get(app_id, owner)
    source = fetch_enterprises(definition.keyword, definition.limit)
    if not source:
        return {"items": [], "source": "industry-knowledge-center", "count": 0}
    existing = service.get(app_id, owner)
    rows, seen = [], set()
    for item in source:
        company_id = str(item.get("企业id") or "").strip()
        name = str(item.get("企业名称") or "").strip()
        if not company_id or not name:
            raise HTTPException(502, "Source enterprise identifier or name is missing")
        if company_id in seen:
            continue
        seen.add(company_id)
        if (
            "enterprises" in existing["tables"]
            and service.query(
                app_id, owner, "enterprises", filters={"company_id": company_id}, limit=1
            )["total"]
        ):
            continue
        rows.append(
            {
                "company_id": company_id,
                "name": name,
                "legal_representative": str(item.get("法定代表人", "")),
                "address": str(item.get("地址", "")),
                "status": str(item.get("企业状态", "")),
                "source": "industry-knowledge-center",
                "imported_at": utc_now().isoformat(),
            }
        )
    service.define_table(
        app_id,
        owner,
        TableDefinition(
            name="enterprises",
            columns=[
                ColumnDefinition(name="company_id", required=True, unique=True, indexed=True),
                ColumnDefinition(name="name", required=True, indexed=True),
                ColumnDefinition(name="legal_representative"),
                ColumnDefinition(name="address"),
                ColumnDefinition(name="status"),
                ColumnDefinition(name="source", required=True),
                ColumnDefinition(name="imported_at", required=True),
            ],
        ),
    )
    if not rows:
        return {"items": [], "source": "industry-knowledge-center", "count": 0}
    result = service.insert(app_id, owner, "enterprises", RecordBatch(rows=rows))
    return {**result, "source": "industry-knowledge-center", "count": len(rows)}
