"""
Centralized schema registry for the `data` field of the unified response envelope.

Background:
    All /v1/* endpoints in the project return the standard { code, message, data,
    trace_id, timestamp } envelope (see core/infra/responses.py), but the vast majority
    of routes do not declare a response_model, so the data field is empty {} in the
    openapi.json FastAPI generates.

This module provides:
    * DATA_COMPONENTS — reusable JSON-Schema components (entity shapes), injected under
      openapi.components.schemas for reuse via $ref.
    * DATA_SCHEMAS    — (METHOD, PATH) → JSON Schema mapping for the data field.

Integration point:
    The _custom_openapi() hook in api/app.py reads this module; for endpoints matched in
    DATA_SCHEMAS, it replaces the response schema from `$ref ApiResponseEnvelope` to
    `allOf: [ApiResponseEnvelope, {properties: {data: <specific>}}]`,
    keeping the envelope structure while typing the business data.

Maintenance conventions:
    * Path and method must match the actual router declaration (verify: grep openapi.json).
    * If an endpoint's data shape is complex and untyped, leave it out and let the generic envelope be the fallback.
    * Keep module order: components first, registry second; group entries within the registry by business module.
"""

from typing import Any, Dict, Tuple

# ---------------------------------------------------------------------------
# 1. Component schema: reusable entity shapes
# ---------------------------------------------------------------------------

DATA_COMPONENTS: Dict[str, Dict[str, Any]] = {
    # ===== Chat =====
    "ChatSessionItem": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string"},
            "title": {"type": "string"},
            "user_id": {"type": "string"},
            "message_count": {"type": "integer"},
            "pinned": {"type": "boolean"},
            "favorite": {"type": "boolean"},
            "metadata": {"type": "object", "additionalProperties": True},
            "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
        },
    },
    "ChatMessageItem": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string"},
            "chat_id": {"type": "string"},
            "role": {"type": "string", "enum": ["user", "assistant"]},
            "content": {"type": "string"},
            "model": {"type": "string"},
            "tool_calls": {"type": "array"},
            "metadata": {"type": "object", "additionalProperties": True},
            "created_at": {"type": "string", "format": "date-time"},
        },
    },
    "SearchChatResultItem": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string"},
            "title": {"type": "string"},
            "user_id": {"type": "string"},
            "message_count": {"type": "integer"},
            "pinned": {"type": "boolean"},
            "favorite": {"type": "boolean"},
            "metadata": {"type": "object", "additionalProperties": True},
            "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
            "match_type": {"type": "string", "enum": ["title", "content"]},
            "matched_snippet": {"type": "string"},
        },
    },
    # ===== Chat Shares =====
    "ShareMessageItem": {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": ["user", "assistant"]},
            "content": {"type": "string"},
            "is_markdown": {"type": "boolean"},
            "created_at": {"type": "string", "format": "date-time"},
            "plan_data": {"type": "object", "additionalProperties": True},
        },
    },
    "ShareRecordItem": {
        "type": "object",
        "properties": {
            "share_id": {"type": "string"},
            "chat_id": {"type": "string"},
            "origin_message_ts": {"type": "integer"},
            "title": {"type": "string"},
            "preview_url": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
            "expires_at": {"type": "string", "format": "date-time"},
            "expiry_option": {"type": "string", "enum": ["3d", "15d", "3m", "permanent"]},
            "created_by": {"type": "string"},
            "created_by_username": {"type": "string"},
            "status": {"type": "string", "enum": ["valid", "expired"]},
            "view_count": {"type": "integer"},
            "revoked": {"type": "boolean"},
        },
    },
    "ChatSharePayload": {
        "type": "object",
        "properties": {
            "share_id": {"type": "string"},
            "chat_id": {"type": "string"},
            "origin_message_ts": {"type": "integer"},
            "title": {"type": "string"},
            "items": {"type": "array", "items": {"$ref": "#/components/schemas/ShareMessageItem"}},
            "created_by": {"type": "string"},
            "created_by_username": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
            "expires_at": {"type": "string", "format": "date-time"},
            "expiry_option": {"type": "string"},
        },
    },
    # ===== Admin Chat History =====
    "AdminChatSessionItem": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string"},
            "user_id": {"type": "string"},
            "username": {"type": "string"},
            "title": {"type": "string"},
            "message_count": {"type": "integer"},
            "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
            "deleted_at": {"type": "string", "format": "date-time"},
        },
    },
    "AdminChatMessageItem": {
        "type": "object",
        "properties": {
            "message_id": {"type": "string"},
            "role": {"type": "string"},
            "content": {"type": "string"},
            "model": {"type": "string"},
            "tool_calls": {"type": "array"},
            "usage": {"type": "object", "additionalProperties": True},
            "error": {"type": "object", "additionalProperties": True},
            "created_at": {"type": "string", "format": "date-time"},
        },
    },
    "AdminUserItem": {
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
            "username": {"type": "string"},
        },
    },
    # ===== Knowledge Base =====
    "KBChunkPreview": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "content": {"type": "string"},
            "token_count": {"type": "integer"},
            "children_count": {"type": "integer"},
            "children_preview": {"type": "array", "items": {"type": "object"}},
        },
    },
    "KBSpaceItem": {
        "type": "object",
        "properties": {
            "kb_id": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "document_count": {"type": "integer"},
            "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
        },
    },
    "KBDocumentItem": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "desc": {"type": "string"},
            "filename": {"type": "string"},
            "size": {"type": "integer"},
            "mime_type": {"type": "string"},
            "storage_key": {"type": "string"},
            "uploaded_at": {"type": "string", "format": "date-time"},
            "indexing_status": {"type": "string"},
        },
    },
    "KBDocumentDetail": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "desc": {"type": "string"},
            "filename": {"type": "string"},
            "mime_type": {"type": "string"},
            "uploaded_at": {"type": "string", "format": "date-time"},
            "content": {"type": "string"},
        },
    },
    "KBChunkItem": {
        "type": "object",
        "properties": {
            "chunk_id": {"type": "string"},
            "document_id": {"type": "string"},
            "chunk_index": {"type": "integer"},
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "questions": {"type": "array", "items": {"type": "string"}},
        },
    },
    # ===== Agents =====
    "UserAgentItem": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "name": {"type": "string"},
            "avatar": {"type": "string"},
            "description": {"type": "string"},
            "system_prompt": {"type": "string"},
            "welcome_message": {"type": "string"},
            "suggested_questions": {"type": "array", "items": {"type": "string"}},
            "mcp_server_ids": {"type": "array", "items": {"type": "string"}},
            "skill_ids": {"type": "array", "items": {"type": "string"}},
            "kb_ids": {"type": "array", "items": {"type": "string"}},
            "model_provider_id": {"type": "string"},
            "temperature": {"type": "number"},
            "max_tokens": {"type": "integer"},
            "max_iters": {"type": "integer"},
            "timeout": {"type": "integer"},
            "is_enabled": {"type": "boolean"},
            "extra_config": {"type": "object", "additionalProperties": True},
            "created_at": {"type": "string", "format": "date-time"},
            "updated_at": {"type": "string", "format": "date-time"},
        },
    },
    "AgentExportItem": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "system_prompt": {"type": "string"},
            "welcome_message": {"type": "string"},
            "suggested_questions": {"type": "array", "items": {"type": "string"}},
            "mcp_server_ids": {"type": "array", "items": {"type": "string"}},
            "skill_ids": {"type": "array", "items": {"type": "string"}},
            "kb_ids": {"type": "array", "items": {"type": "string"}},
            "model_provider_id": {"type": "string"},
            "temperature": {"type": "number"},
            "max_tokens": {"type": "integer"},
            "max_iters": {"type": "integer"},
            "timeout": {"type": "integer"},
            "is_enabled": {"type": "boolean"},
            "sort_order": {"type": "integer"},
            "extra_config": {"type": "object", "additionalProperties": True},
            "avatar": {"type": "string"},
        },
    },
    "AvailableResources": {
        "type": "object",
        "properties": {
            "mcps": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "skills": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "kb": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "models": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        },
    },
    # ===== Auth / User =====
    "UserAuthInfo": {
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
            "username": {"type": "string"},
            "email": {"type": "string"},
            "avatar_url": {"type": "string"},
            "nickname": {"type": "string"},
            "real_name": {"type": "string"},
            "department": {"type": "string"},
            "expires_at": {"type": "string", "format": "date-time"},
            "sso_token": {"type": "string"},
            "allowed_apps": {"type": "array", "items": {"type": "string"}},
            "lab_enabled": {"type": "boolean"},
        },
    },
    "CurrentUserInfo": {
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
            "user_center_id": {"type": "string"},
            "username": {"type": "string"},
            "email": {"type": "string"},
            "avatar": {"type": "string"},
            "avatar_url": {"type": "string"},
            "nickname": {"type": "string"},
            "real_name": {"type": "string"},
            "phone": {"type": "string"},
            "department": {"type": "string"},
            "auth_source": {"type": "string", "enum": ["local", "external"]},
            "created_at": {"type": "string", "format": "date-time"},
        },
    },
    "UserPreferencesResponse": {
        "type": "object",
        "properties": {
            "default_model": {"type": "string"},
            "language": {"type": "string"},
            "theme": {"type": "string"},
            "enabled_skills": {"type": "array", "items": {"type": "string"}},
            "enabled_mcps": {"type": "array", "items": {"type": "string"}},
        },
    },
    # ===== Memories =====
    "MemorySettingsInfo": {
        "type": "object",
        "properties": {
            "memory_enabled": {"type": "boolean"},
            "memory_write_enabled": {"type": "boolean"},
            "mem0_available": {"type": "boolean"},
            "reranker_enabled": {"type": "boolean"},
            "reranker_available": {"type": "boolean"},
        },
    },
    "MemoryFactItem": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "content": {"type": "string"},
            "layer": {"type": "string"},
            "source": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "confidentiality": {"type": "string"},
            "ttl_days": {"type": "integer"},
            "evidence": {"type": "string"},
        },
    },
    "ProfileMemoryInfo": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "workspace_id": {"type": "string"},
            "content_md": {"type": "string"},
            "length": {"type": "integer"},
            "max_chars": {"type": "integer"},
        },
    },
    "MemoryAuditItem": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "ts": {"type": "string", "format": "date-time"},
            "actor": {"type": "string"},
            "action": {"type": "string"},
            "layer": {"type": "string"},
            "memory_id": {"type": "string"},
            "workspace_id": {"type": "string"},
            "chat_id": {"type": "string"},
            "confidentiality": {"type": "string"},
            "content_hash": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
    "GraphMemoryInfo": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "relations": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "count": {"type": "integer"},
        },
    },
    # ===== Catalog =====
    "CatalogItemBase": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "enabled": {"type": "boolean"},
            "config": {"type": "object", "additionalProperties": True},
            "metadata": {"type": "object", "additionalProperties": True},
        },
    },
    "KBCatalogItem": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "kind": {"type": "string", "enum": ["knowledge_base"]},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "enabled": {"type": "boolean"},
            "version": {"type": "string"},
            "provider": {"type": "string"},
            "visibility": {"type": "string", "enum": ["public", "private"]},
            "is_public": {"type": "boolean"},
            "chunk_method": {"type": "string"},
            "document_count": {"type": "integer"},
            "total_size_bytes": {"type": "integer"},
            "detail": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "system_managed": {"type": "boolean"},
            "pinned": {"type": "boolean"},
            "editable": {"type": "boolean"},
            "deletable": {"type": "boolean"},
            "uploadable": {"type": "boolean"},
        },
    },
    "CatalogData": {
        "type": "object",
        "properties": {
            "skills": {"type": "array", "items": {"$ref": "#/components/schemas/CatalogItemBase"}},
            "agents": {"type": "array", "items": {"$ref": "#/components/schemas/CatalogItemBase"}},
            "mcp": {"type": "array", "items": {"$ref": "#/components/schemas/CatalogItemBase"}},
            "kb": {"type": "array", "items": {"$ref": "#/components/schemas/KBCatalogItem"}},
        },
    },
}


# ---------------------------------------------------------------------------
# 2. Endpoint registry: (METHOD, PATH) → data field schema
# ---------------------------------------------------------------------------


# Shorthand for writing $ref
def _ref(name: str) -> Dict[str, Any]:
    return {"$ref": f"#/components/schemas/{name}"}


DATA_SCHEMAS: Dict[Tuple[str, str], Dict[str, Any]] = {
    # ===== Chat =====
    ("GET", "/v1/chats"): {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": _ref("ChatSessionItem")},
            "page": {"type": "integer"},
            "page_size": {"type": "integer"},
            "total": {"type": "integer"},
        },
    },
    ("POST", "/v1/chats"): _ref("ChatSessionItem"),
    ("GET", "/v1/chats/search"): {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": _ref("SearchChatResultItem")},
            "total": {"type": "integer"},
        },
    },
    ("GET", "/v1/chats/{chat_id}"): _ref("ChatSessionItem"),
    ("PATCH", "/v1/chats/{chat_id}"): _ref("ChatSessionItem"),
    ("DELETE", "/v1/chats/{chat_id}"): {"type": "null"},
    ("GET", "/v1/chats/{chat_id}/messages"): {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": _ref("ChatMessageItem")},
            "page": {"type": "integer"},
            "page_size": {"type": "integer"},
            "total": {"type": "integer"},
        },
    },
    ("GET", "/v1/chats/{chat_id}/messages/{message_id}/followups"): {
        "type": "object",
        "properties": {
            "follow_up_questions": {"type": "array", "items": {"type": "string"}},
        },
    },
    ("POST", "/v1/chats/send"): {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string"},
            "response": {"type": "string"},
            "timestamp": {"type": "string", "format": "date-time"},
            "is_markdown": {"type": "boolean"},
            "route": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "artifacts": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    },
    ("POST", "/v1/chats/stream"): {
        "type": "object",
        "additionalProperties": True,
        "description": "SSE 流式响应；事件类型包含 thinking / content / tool_call / tool_result / meta / error",
    },
    ("POST", "/v1/chats/{chat_id}/regenerate"): {
        "type": "object",
        "additionalProperties": True,
        "description": "SSE 流式响应；事件结构同 /v1/chats/stream",
    },
    ("POST", "/v1/chats/{chat_id}/edit"): {
        "type": "object",
        "additionalProperties": True,
        "description": "SSE 流式响应；事件结构同 /v1/chats/stream",
    },
    ("POST", "/v1/chats/messages/{message_id}/feedback"): {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "feedback_id": {"type": "string"},
            "rating": {"type": "string", "enum": ["like", "dislike"]},
        },
    },
    # ===== Chat Shares =====
    ("POST", "/v1/chat-shares"): {
        "type": "object",
        "properties": {
            "share_id": {"type": "string"},
            "preview_url": {"type": "string"},
            "expires_at": {"type": "string", "format": "date-time"},
            "expiry_option": {"type": "string"},
        },
    },
    ("GET", "/v1/chat-shares"): {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": _ref("ShareRecordItem")},
        },
    },
    ("GET", "/v1/chat-shares/{share_id}"): _ref("ChatSharePayload"),
    ("POST", "/v1/chat-shares/{share_id}/revoke"): {
        "type": "object",
        "properties": {
            "share_id": {"type": "string"},
            "status": {"type": "string"},
        },
    },
    ("POST", "/v1/chat-shares/{share_id}/restore"): {
        "type": "object",
        "properties": {
            "share_id": {"type": "string"},
            "status": {"type": "string"},
        },
    },
    # ===== Admin Chat History =====
    ("GET", "/v1/admin/chat-history/sessions"): {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": _ref("AdminChatSessionItem")},
            "page": {"type": "integer"},
            "page_size": {"type": "integer"},
            "total": {"type": "integer"},
        },
    },
    ("GET", "/v1/admin/chat-history/sessions/{chat_id}/messages"): {
        "type": "array",
        "items": _ref("AdminChatMessageItem"),
    },
    ("GET", "/v1/admin/chat-history/users"): {
        "type": "array",
        "items": _ref("AdminUserItem"),
    },
    # ===== Knowledge Base =====
    ("POST", "/v1/catalog/kb/preview-chunks"): {
        "type": "object",
        "properties": {
            "total_chunks": {"type": "integer"},
            "total_children": {"type": "integer"},
            "chunks": {"type": "array", "items": _ref("KBChunkPreview")},
        },
    },
    ("POST", "/v1/catalog/kb"): _ref("KBSpaceItem"),
    ("POST", "/v1/catalog/kb/polish-description"): {
        "type": "object",
        "properties": {"description": {"type": "string"}},
    },
    ("PATCH", "/v1/catalog/kb/{kb_id}"): _ref("KBSpaceItem"),
    ("DELETE", "/v1/catalog/kb/{kb_id}"): {"type": "null"},
    ("POST", "/v1/catalog/kb/{kb_id}/documents"): {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "kb_id": {"type": "string"},
            "title": {"type": "string"},
            "filename": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "mime_type": {"type": "string"},
            "storage_key": {"type": "string"},
            "checksum": {"type": "string"},
            "indexing_status": {"type": "string"},
        },
    },
    ("GET", "/v1/catalog/kb/{kb_id}/documents"): {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": _ref("KBDocumentItem")},
            "page": {"type": "integer"},
            "page_size": {"type": "integer"},
            "total": {"type": "integer"},
        },
    },
    ("GET", "/v1/catalog/kb/{kb_id}/documents/{document_id}"): _ref("KBDocumentDetail"),
    ("DELETE", "/v1/catalog/kb/{kb_id}/documents/{document_id}"): {"type": "null"},
    ("POST", "/v1/catalog/kb/{kb_id}/documents/{document_id}/reindex"): {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "indexing_status": {"type": "string"},
        },
    },
    ("GET", "/v1/catalog/kb/{kb_id}/chunks"): {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": _ref("KBChunkItem")},
            "page": {"type": "integer"},
            "page_size": {"type": "integer"},
            "total": {"type": "integer"},
        },
    },
    ("PATCH", "/v1/catalog/kb/{kb_id}/chunks/{chunk_id}"): {
        "type": "object",
        "properties": {
            "chunk_id": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "questions": {"type": "array", "items": {"type": "string"}},
        },
    },
    # ===== Agents =====
    ("GET", "/v1/agents"): {"type": "array", "items": _ref("UserAgentItem")},
    ("GET", "/v1/agents/available-resources"): _ref("AvailableResources"),
    ("GET", "/v1/agents/{agent_id}"): _ref("UserAgentItem"),
    ("POST", "/v1/agents"): _ref("UserAgentItem"),
    ("PUT", "/v1/agents/{agent_id}"): _ref("UserAgentItem"),
    ("DELETE", "/v1/agents/{agent_id}"): {
        "type": "object",
        "properties": {"deleted": {"type": "boolean"}},
    },
    # ===== Admin Agents =====
    ("GET", "/v1/admin/agents"): {"type": "array", "items": _ref("UserAgentItem")},
    ("POST", "/v1/admin/agents"): _ref("UserAgentItem"),
    ("GET", "/v1/admin/agents/export"): {"type": "array", "items": _ref("AgentExportItem")},
    ("POST", "/v1/admin/agents/import"): {
        "type": "object",
        "properties": {
            "created": {"type": "integer"},
            "updated": {"type": "integer"},
            "message": {"type": "string"},
        },
    },
    ("GET", "/v1/admin/agents/{agent_id}"): _ref("UserAgentItem"),
    ("PUT", "/v1/admin/agents/{agent_id}"): _ref("UserAgentItem"),
    ("PUT", "/v1/admin/agents/{agent_id}/toggle"): _ref("UserAgentItem"),
    ("DELETE", "/v1/admin/agents/{agent_id}"): {
        "type": "object",
        "properties": {"deleted": {"type": "boolean"}},
    },
    # ===== Auth =====
    ("POST", "/v1/auth/ticket/exchange"): _ref("UserAuthInfo"),
    ("GET", "/v1/auth/session/check"): _ref("UserAuthInfo"),
    ("POST", "/v1/auth/logout"): {
        "type": "object",
        "properties": {"login_url": {"type": "string"}},
    },
    # ===== Current user =====
    ("GET", "/v1/me"): _ref("CurrentUserInfo"),
    ("PATCH", "/v1/me"): _ref("CurrentUserInfo"),
    # ===== User preferences =====
    ("GET", "/v1/users/{user_id}/preferences"): _ref("UserPreferencesResponse"),
    ("PUT", "/v1/users/{user_id}/preferences"): {"type": "null"},
    # ===== Memories =====
    ("GET", "/v1/memories/settings"): _ref("MemorySettingsInfo"),
    ("PATCH", "/v1/memories/settings"): {
        "type": "object",
        "additionalProperties": True,
        "description": "返回 MemorySettingsInfo 的子集（仅包含本次更新的字段）",
    },
    ("GET", "/v1/memories"): {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "items": {"type": "array", "items": _ref("MemoryFactItem")},
            "count": {"type": "integer"},
        },
    },
    ("DELETE", "/v1/memories"): {
        "type": "object",
        "properties": {"message": {"type": "string"}},
    },
    ("DELETE", "/v1/memories/{memory_id}"): {
        "type": "object",
        "properties": {"deleted": {"type": "string"}},
    },
    ("GET", "/v1/memories/profile"): _ref("ProfileMemoryInfo"),
    ("GET", "/v1/memories/audit"): {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "items": {"type": "array", "items": _ref("MemoryAuditItem")},
            "count": {"type": "integer"},
        },
    },
    ("GET", "/v1/memories/graph"): _ref("GraphMemoryInfo"),
    # ===== Catalog =====
    ("GET", "/v1/catalog"): _ref("CatalogData"),
    ("PATCH", "/v1/catalog/{kind}/{id}"): {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "id": {"type": "string"},
            "enabled": {"type": "boolean"},
            "config": {"type": "object", "additionalProperties": True},
        },
    },
}
