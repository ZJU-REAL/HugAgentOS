"""Community edition has no externally managed knowledge provider."""


def is_enabled() -> bool:
    return False


def get_provider_name() -> str:
    return ""


def get_provider_cache_identity(provider_name: str | None = None) -> str:
    return f"{provider_name or 'ce'}:disabled"


class CollectionDirectoryUnavailable(RuntimeError):
    pass


def collection_snapshot() -> list:
    return []


def get_collections(*args, **kwargs) -> list:
    return []


async def get_collections_async(*args, **kwargs) -> list:
    return []


def warm_collections() -> None:
    return None


def list_documents(*args, **kwargs) -> list:
    return []


def get_document_detail(*args, **kwargs):
    return None


def open_original_document(*args, **kwargs):
    return None


def get_allowed_collection_ids(*args, **kwargs):
    return None


def runtime_request_context(enabled_kb_ids: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    return {}, {}


def supports_wiki() -> bool:
    """社区版没有外接知识后端，因此这一路不提供 Wiki。

    注意这**不代表社区版没有 Wiki**：自建知识库勾选 Wiki 索引模式后，页面由本地
    管线生成，走 ``features.knowledge.wiki.local_provider``。要问「某个库有没有 Wiki」应当用
    ``features.knowledge.wiki_router.supports_wiki(kb_id)``，它会把两条来源都算上。
    """
    return False


def wiki_module():
    return None


__all__ = [
    "CollectionDirectoryUnavailable",
    "collection_snapshot",
    "get_allowed_collection_ids",
    "get_collections",
    "get_collections_async",
    "get_document_detail",
    "open_original_document",
    "get_provider_cache_identity",
    "get_provider_name",
    "is_enabled",
    "list_documents",
    "runtime_request_context",
    "supports_wiki",
    "warm_collections",
    "wiki_module",
]
