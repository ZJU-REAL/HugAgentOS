"""Dify Knowledge Base client for dataset management."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import re
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


def _normalize_timestamp(value: Any) -> Optional[str]:
    """Normalize Dify timestamp values to ISO8601 strings."""
    if value is None or value == "":
        return None

    numeric_value: Optional[float] = None
    if isinstance(value, (int, float)):
        numeric_value = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            numeric_value = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                return None
    else:
        return None

    if numeric_value >= 1e15:
        numeric_value /= 1_000_000
    elif numeric_value >= 1e12:
        numeric_value /= 1_000

    try:
        return datetime.fromtimestamp(numeric_value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _resolve_documents_total(
    *,
    data: Dict[str, Any],
    raw_docs: List[Dict[str, Any]],
    dataset_id: str,
    base_url: str,
    auth_token: str,
    page: int,
    limit: int,
    keyword: str,
) -> int:
    """Resolve actual documents total, probing more pages when Dify omits it."""
    total_raw = data.get("total")
    try:
        total = int(total_raw)
        if total >= 0:
            return total
    except (TypeError, ValueError):
        pass

    has_more = bool(data.get("has_more"))
    if not has_more:
        return len(raw_docs)

    total = (max(page, 1) - 1) * limit + len(raw_docs)
    next_page = max(page, 1) + 1

    while True:
        params: Dict[str, Any] = {"page": next_page, "limit": limit}
        if keyword:
            params["keyword"] = keyword
        try:
            resp = requests.get(
                f"{base_url}/datasets/{dataset_id}/documents",
                headers={"Authorization": f"Bearer {auth_token}"},
                params=params,
                timeout=3,
            )
            resp.raise_for_status()
            next_payload = resp.json()
        except Exception as exc:
            logger.warning(
                "Failed to probe Dify documents total dataset=%s page=%s: %s",
                dataset_id,
                next_page,
                exc,
            )
            break

        next_docs = next_payload.get("data", [])
        if not isinstance(next_docs, list) or not next_docs:
            break

        total += len(next_docs)
        if not next_payload.get("has_more"):
            break
        next_page += 1

    return total


def _resolve_dify_config() -> tuple[str, str]:
    # DB-first: try SystemConfigService, fall back to env
    try:
        from core.services.system_config import SystemConfigService
        svc = SystemConfigService.get_instance()
        base_url = (svc.get("knowledge_base.url") or "").strip().rstrip("/")
        auth_token = (svc.get("knowledge_base.api_key") or "").strip()
        if base_url and auth_token:
            return base_url, auth_token
    except Exception:
        pass
    base_url = (
        os.getenv("DIFY_URL") or os.getenv("DIFY_BASE_URL") or ""
    ).strip().rstrip("/")
    auth_token = (
        os.getenv("DIFY_API_KEY") or os.getenv("DIFY_AUTH_TOKEN") or ""
    ).strip()
    return base_url, auth_token


def get_allowed_dataset_ids() -> set[str]:
    """
    Optional allowlist of Dify dataset IDs.

    DB-first via SystemConfigService, falls back to env DIFY_ALLOWED_DATASET_IDS.
    """
    raw = ""
    try:
        from core.services.system_config import SystemConfigService
        raw = (SystemConfigService.get_instance().get("knowledge_base.allowed_dataset_ids") or "").strip()
    except Exception:
        pass
    if not raw:
        raw = (os.getenv("DIFY_ALLOWED_DATASET_IDS") or "").strip()
    if not raw:
        return set()
    parts = [x.strip() for x in re.split(r"[,\n;]+", raw) if x.strip()]
    return set(parts)


def is_dataset_allowed(dataset_id: str) -> bool:
    """Check whether dataset_id is allowed by env allowlist."""
    target = (dataset_id or "").strip()
    if not target:
        return False
    allowlist = get_allowed_dataset_ids()
    if not allowlist:
        return True
    return target in allowlist


def is_dify_enabled() -> bool:
    """
    Determine whether Dify-backed knowledge base should be used.

    Priority:
    1. DB config knowledge_base.provider
    2. Explicit KNOWLEDGE_BASE env setting (must be "dify")
    3. Fallback to presence of Dify URL + API key
    """
    try:
        from core.services.system_config import SystemConfigService
        provider = (SystemConfigService.get_instance().get("knowledge_base.provider") or "").strip().lower()
        if provider:
            return provider == "dify"
    except Exception:
        pass
    kb_backend = (os.getenv("KNOWLEDGE_BASE") or "").strip().lower()
    if kb_backend:
        return kb_backend == "dify"

    base_url, auth_token = _resolve_dify_config()
    return bool(base_url and auth_token)


def _format_doc_desc(doc: Dict[str, Any]) -> str:
    """Build a human-readable description for a Dify document."""
    parts: List[str] = []
    status = doc.get("indexing_status", "")
    if status:
        status_map = {
            "completed": "已完成",
            "indexing": "索引中",
            "waiting": "等待中",
            "error": "错误",
            "paused": "已暂停",
        }
        parts.append(f"状态：{status_map.get(status, status)}")
    word_count = doc.get("word_count") or doc.get("tokens", 0)
    if word_count:
        parts.append(f"{word_count:,} 词")
    src = doc.get("data_source_type", "")
    if src:
        src_map = {
            "upload_file": "上传文件",
            "web_site": "网页",
            "notion_import": "Notion",
        }
        parts.append(src_map.get(src, src))
    return "  |  ".join(parts) if parts else "文档"


def _dataset_to_kb_item(ds: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a Dify dataset object into a frontend KBItem-compatible dict."""
    tags: List[str] = []
    if ds.get("indexing_technique"):
        tags.append(ds["indexing_technique"])
    if ds.get("embedding_model_provider"):
        tags.append(ds["embedding_model_provider"])

    doc_count: int = ds.get("document_count", 0)
    word_count: int = ds.get("word_count", 0)

    detail = (
        f"### {ds.get('name', '知识库')}\n\n"
        f"{ds.get('description') or '暂无简介'}\n"
    )

    return {
        "id": ds["id"],
        "kind": "knowledge_base",
        "name": ds.get("name", ds["id"]),
        "description": ds.get("description") or "无简介",
        "desc": ds.get("description") or "无简介",
        "enabled": True,
        "version": "dify",
        "tags": tags,
        "detail": detail,
        "provider": ds.get("embedding_model_provider", "Dify"),
        "document_count": doc_count,
        "word_count": word_count,
    }


def list_datasets(page: int = 1, limit: int = 100, keyword: str = "", timeout=3) -> List[Dict[str, Any]]:
    """Fetch all datasets from Dify and return as KBItem-compatible list.

    Args:
        timeout: HTTP timeout in seconds. Can be a float or a
                 (connect_timeout, read_timeout) tuple.
    """
    base_url, auth_token = _resolve_dify_config()
    if not base_url or not auth_token:
        logger.warning("Dify config missing: DIFY_URL and DIFY_API_KEY required")
        return []

    params: Dict[str, Any] = {"page": page, "limit": limit}
    if keyword:
        params["keyword"] = keyword

    try:
        # Use a session with retries disabled when using short timeouts,
        # to avoid blocking on unreachable Dify instances.
        session = requests.Session()
        if isinstance(timeout, tuple) or (isinstance(timeout, (int, float)) and timeout < 5):
            from requests.adapters import HTTPAdapter
            session.mount("http://", HTTPAdapter(max_retries=0))
            session.mount("https://", HTTPAdapter(max_retries=0))
        resp = session.get(
            f"{base_url}/datasets",
            headers={"Authorization": f"Bearer {auth_token}"},
            params=params,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        datasets = data.get("data", [])
        items = [_dataset_to_kb_item(ds) for ds in datasets]
        allowlist = get_allowed_dataset_ids()
        if not allowlist:
            return items
        return [item for item in items if str(item.get("id", "")).strip() in allowlist]
    except Exception as exc:
        logger.error("Failed to fetch Dify datasets: %s", exc)
        return []


def get_dataset(dataset_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single dataset detail from Dify."""
    if not is_dataset_allowed(dataset_id):
        return None

    base_url, auth_token = _resolve_dify_config()
    if not base_url or not auth_token:
        return None

    try:
        resp = requests.get(
            f"{base_url}/datasets/{dataset_id}",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=3,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("Failed to fetch Dify dataset %s: %s", dataset_id, exc)
        return None


def list_documents(
    dataset_id: str,
    page: int = 1,
    limit: int = 20,
    keyword: str = "",
) -> Dict[str, Any]:
    """Fetch documents for a Dify dataset and convert to frontend-friendly format."""
    base_url, auth_token = _resolve_dify_config()
    empty: Dict[str, Any] = {
        "items": [],
        "total": 0,
        "page": page,
        "page_size": limit,
        "has_more": False,
    }
    if not is_dataset_allowed(dataset_id):
        return empty

    if not base_url or not auth_token:
        return empty

    params: Dict[str, Any] = {"page": page, "limit": limit}
    if keyword:
        params["keyword"] = keyword

    try:
        resp = requests.get(
            f"{base_url}/datasets/{dataset_id}/documents",
            headers={"Authorization": f"Bearer {auth_token}"},
            params=params,
            timeout=3,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch Dify documents for dataset %s: %s", dataset_id, exc)
        return empty

    raw_docs = data.get("data", [])
    items = [
        {
            "id": doc.get("id", ""),
            "title": doc.get("name", doc.get("id", "")),
            "desc": _format_doc_desc(doc),
            "word_count": doc.get("word_count", 0),
            "indexing_status": doc.get("indexing_status", ""),
            "enabled": doc.get("enabled", True),
            "data_source_type": doc.get("data_source_type", ""),
            "created_at": _normalize_timestamp(doc.get("created_at")),
        }
        for doc in raw_docs
    ]

    total = _resolve_documents_total(
        data=data,
        raw_docs=raw_docs if isinstance(raw_docs, list) else [],
        dataset_id=dataset_id,
        base_url=base_url,
        auth_token=auth_token,
        page=page,
        limit=limit,
        keyword=keyword,
    )

    dataset_detail = get_dataset(dataset_id)
    if isinstance(dataset_detail, dict):
        detail_total = dataset_detail.get("document_count")
        try:
            detail_total_int = int(detail_total)
            if detail_total_int > total:
                total = detail_total_int
        except (TypeError, ValueError):
            pass

    return {
        "items": items,
        "total": total,
        "page": data.get("page", page),
        "page_size": data.get("limit", limit),
        "has_more": data.get("has_more", False),
    }


def _request_json(url: str, auth_token: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Perform GET request and return JSON dict."""
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {auth_token}"},
        params=params,
        timeout=3,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _first_non_empty_str(*values: Any) -> str:
    """Return the first non-empty string value."""
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return ""


def get_document_detail(dataset_id: str, document_id: str) -> Dict[str, Any]:
    """
    Fetch Dify document detail and aggregate segment content for frontend modal display.

    Dify list API does not include full content. Content is assembled from the
    document segments endpoint.
    """
    base_url, auth_token = _resolve_dify_config()
    empty: Dict[str, Any] = {
        "id": document_id,
        "title": document_id,
        "desc": "文档详情暂不可用",
        "content": "",
    }
    if not is_dataset_allowed(dataset_id):
        return empty

    if not base_url or not auth_token:
        return empty

    detail: Dict[str, Any] = {}
    try:
        detail = _request_json(
            f"{base_url}/datasets/{dataset_id}/documents/{document_id}",
            auth_token=auth_token,
        )
    except Exception as exc:
        logger.warning(
            "Failed to fetch Dify document detail dataset=%s document=%s: %s",
            dataset_id,
            document_id,
            exc,
        )

    segment_texts: List[str] = []
    page = 1
    limit = 100
    max_segments = 500

    while len(segment_texts) < max_segments:
        try:
            seg_payload = _request_json(
                f"{base_url}/datasets/{dataset_id}/documents/{document_id}/segments",
                auth_token=auth_token,
                params={"page": page, "limit": limit},
            )
        except Exception as exc:
            logger.warning(
                "Failed to fetch Dify document segments dataset=%s document=%s page=%s: %s",
                dataset_id,
                document_id,
                page,
                exc,
            )
            break

        raw_segments = seg_payload.get("data", [])
        if not isinstance(raw_segments, list):
            raw_segments = []

        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            text = seg.get("content") or seg.get("answer") or seg.get("segment")
            if isinstance(text, str):
                normalized = text.strip()
                if normalized:
                    segment_texts.append(normalized)
                    if len(segment_texts) >= max_segments:
                        break

        has_more = bool(seg_payload.get("has_more", False))
        if not has_more:
            break
        page += 1

    content = "\n\n".join(segment_texts).strip()
    if content:
        try:
            from core.services.system_config import SystemConfigService
            max_chars = int(SystemConfigService.get_instance().get("knowledge_base.detail_max_chars", "50000"))
        except Exception:
            max_chars = int(os.getenv("KB_DETAIL_CONTENT_MAX_CHARS", "50000"))
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "\n\n...（内容过长，已截断）"

    # Dify detail endpoint may wrap document fields under "data".
    detail_data = detail.get("data") if isinstance(detail.get("data"), dict) else detail

    title = (
        _first_non_empty_str(
            detail_data.get("name") if isinstance(detail_data, dict) else None,
            detail_data.get("title") if isinstance(detail_data, dict) else None,
            detail.get("name"),
            detail.get("title"),
        )
        or (detail_data.get("id") if isinstance(detail_data, dict) else "")
        or detail.get("id")
        or document_id
    )
    desc_source = detail_data if isinstance(detail_data, dict) else detail
    desc = _format_doc_desc(desc_source)
    if not desc or desc == "文档":
        desc = "文档详情"

    return {
        "id": document_id,
        "title": title,
        "desc": desc,
        "content": content,
        "segment_count": len(segment_texts),
    }
