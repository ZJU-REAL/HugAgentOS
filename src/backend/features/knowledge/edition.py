"""Community knowledge-base grant policy: resources are owner-only."""

from __future__ import annotations

from sqlalchemy.orm import Session


def initial_visibility_grants(db: Session, user_id: str, visibility: str) -> list[tuple[str, str]]:
    return []


__all__ = ["initial_visibility_grants"]


def grant_initial_access(db, kb_id, grants, *, granted_by):
    if grants:
        raise ValueError("Community knowledge bases do not support shared grants")


def __getattr__(name):
    if name in ('KBLevel', 'filter_accessible_kb_ids', 'get_accessible_local_kb_ids', 'get_accessible_local_kb_levels', 'get_dataset_levels', 'has_kb_permission', 'is_shared_visibility', 'level_to_caps', 'resolve_local_kb_level'):
        import features.knowledge.personal_permissions as provider
        return getattr(provider, name)
    if name in ('get_allowed_collection_ids', 'get_document_detail', 'get_provider_cache_identity', 'get_provider_name', 'is_enabled', 'list_collections', 'list_documents', 'runtime_request_context', 'supports_wiki', 'wiki_module'):
        import features.knowledge.disabled_provider as provider
        return getattr(provider, name)
    if name in ('DatasetRetrievalTimeoutError', 'DatasetRetrievalUnavailableError', 'MAX_RETRIEVE_TOKENS', 'RETRIEVE_MAX_CONCURRENCY', 'RETRIEVE_REQUEST_TIMEOUT_SECONDS', 'RETRIEVE_TOTAL_TIMEOUT_SECONDS', 'list_external_datasets', 'retrieve_dataset_content', 'retrieve_dataset_content_async'):
        import features.knowledge.disabled_retrieval as provider
        return getattr(provider, name)
    if name in ('CreateKBSpaceRequest', 'IndexingConfig', 'PolishKBDescriptionRequest', 'ReindexRequest', 'UpdateChunkRequest', 'UpdateKBSpaceRequest'):
        import features.knowledge.personal_request_models as provider
        return getattr(provider, name)
    raise AttributeError(name)
