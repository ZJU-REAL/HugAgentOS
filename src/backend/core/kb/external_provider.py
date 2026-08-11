"""Community edition has no externally managed knowledge provider."""


def is_enabled() -> bool:
    return False


def get_provider_name() -> str:
    return ""


def list_collections(*args, **kwargs) -> list:
    return []


def list_documents(*args, **kwargs) -> list:
    return []


def get_document_detail(*args, **kwargs):
    return None


def get_allowed_collection_ids(*args, **kwargs):
    return None


def runtime_request_context(enabled_kb_ids: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    return {}, {}


def supports_wiki() -> bool:
    """LLM Wiki / 概念图谱依赖外接知识后端，社区版整体不提供。"""
    return False


def wiki_module():
    return None


__all__ = [
    "get_allowed_collection_ids",
    "get_document_detail",
    "get_provider_name",
    "is_enabled",
    "list_collections",
    "list_documents",
    "runtime_request_context",
    "supports_wiki",
    "wiki_module",
]
